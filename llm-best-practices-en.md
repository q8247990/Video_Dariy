# Home Surveillance Video Understanding: Best Practices for Dual RTX 3090 × Qwen3.8-27B Local Multimodal Deployment

> Version: English v1 (2026-09-04)
> Source material: 20+ opencode session logs from the ops repository (`~/research/ops`) between
> 2026-08-18 and 2026-09-04, `UPGRADE_P2P_RTX3090.md`, `.video_research/REPORT_video_preprocessing.md`,
> `AGENTS.md` (operations status), plus video_dairy ADR 0009 and the current code implementation.
> Every figure cited below is measured or read from production logs, not theoretical estimates.

---

## 0. Summary

video_dairy is an offline home surveillance video analysis system. Camera footage (2560×1440) on the
NAS is sliced into 60-second sub-chunks; each chunk is sent to a local multimodal LLM as raw mp4
base64 with `num_frames=120`, and the model emits structured JSON events. The pipeline must satisfy
three competing demands simultaneously:

- **Resolution** — must be sharp enough to recognize people and faces;
- **Duration** — every minute of footage (or longer) in a single production chunk must be covered;
- **Cost and latency** — fully offline, no backlog, controllable token cost.

We took a complete hardware + software path to get there:

1. **Hardware**: Migrated from WSL2 to native Ubuntu on dual RTX 3090 (24G×2); applied the aikitoria
   driver patch to enable PCIe P2P; flashed the ReBAR VBIOS to expand each card's BAR1 from 256 MiB
   to 32 GB; locked power at 350 W to eliminate board-level protection throttling.
2. **Deployment**: vLLM TP=2 + Qwen3.8-27B INT4, `gpu-memory-utilization=0.9`, FP8 KV cache,
   262144 context, `--mm-encoder-tp-mode weights`. Final text throughput: 76.7 tok/s single-stream,
   434.7 tok/s at 8 concurrent streams.
3. **Video parameters**: Internalized the invariant "100M pixel budget → ~48.8k vision tokens
   constant" and replaced the default 32-frame uniform sampling with 60-second sub-chunks + 120
   frames (eliminating 4–5 second transient-event aliasing). The keyframe pipeline was kept as a
   research conclusion after A/B validation; production locked onto the raw_mp4 single path.

This document is organized into four layers — Hardware, Deployment, Video, Client — and each layer
provides the **final configuration**, **why that value**, **alternatives tried and dropped**, and a
**pitfall list**.

---

## 1. Background: video_dairy's Workload Profile Against the LLM

```
Video directory → VideoSource → VideoFile → VideoSession → EventRecord → DailySummary
                                                    → Chat / MCP / Webhook
```

The stages that interact with the LLM directly:

1. Scanning, deduplication, session merging and sealing (CPU-only, no model);
2. **Session analysis**: A `SEALED` session is split by file — **every video file maps to exactly
   one sub-chunk and exactly one vision-model call** (no merging, no splitting; the historical
   two-level chunking was removed);
3. Daily summary, Q&A, and MCP make secondary LLM calls on top of the structured events (text-only,
   low cost — out of scope here).

Workload profile of a single vision call (this is the constraint source for every later tuning):

| Dimension | Value | Source |
|---|---|---|
| Input resolution | 2560×1440 (Xiaomi camera, ~20 fps) | Production recording measurements |
| Sub-chunk duration | 60 s | Project constant |
| Vision tokens | ~48.8k (100M pixel budget / 2048, an invariant) | §4.1 derivation |
| Output | Structured JSON event (`temperature=0`, `json_object`, `max_tokens=8192`) | `sub_chunk_runner.py` |
| Concurrency | Vision worker `concurrency=1` (`analysis_hot` / `analysis_full`) | Deployment config |
| Call frequency | 1 call per 60 s of footage (1 min = 1 call; 10 min = 10 calls) | Derived |
| Latency requirement | Batch offline (backfilling NAS backlog); product explicitly accepts latency; at concurrency=1, 1 minute of footage ≈ 60–80 s LLM time (realtime ratio ~1.0–1.3×) | Product decision |

Key point: **The information density of every 48k vision tokens** determines recognition quality,
and 48k is in turn the maximum the VRAM/latency budget allows. The whole tuning exercise is
fundamentally about "spending that 48k most effectively under the 48k-token invariant."

---

## 2. Hardware Foundation: The Dual 3090 Upgrade in Full

Target machine: native Ubuntu 22.04, dual RTX 3090 (24G×2). Both cards are connected directly to
the CPU through separate PCIe Host Bridges (PHBs); no NVLink (unaffordable); Gen4 x8/x8 split.
Card identities:

| Card | PCI | Subsystem | Model | Notes |
|---|---|---|---|---|
| GPU0 | 0e:00.0 | `1043:87AF` | ASUS ROG-STRIX-3090-O24G | Factory OC 390 W, dual BIOS |
| GPU1 | 0f:00.0 | `10de:1454` | Dell OEM 3090 (PG132, modified shell and VBIOS) | Single BIOS, 350 W |

### 2.1 WSL2 Baseline: Why We Had to Leave (Conclusion Only)

The first deployment lived inside WSL2 on a Windows desktop (same dual 3090s). The baseline problem
in one sentence: **WSL2's GPU path goes through the dxgkrnl virtualization layer, which means no
host-kernel control**, with four direct consequences:

- `expandable_segments:True` crashes (CUDA VMM multi-handle vs NCCL/CustomAllreduce's IPC
  single-handle conflict, vllm#43923 long-standing open) — only `False` works, so VRAM
  fragmentation has no remedy;
- `nvidia-smi topo` is unavailable (dxgkrnl does not expose topology) — SGLang HiCache NUMA
  detection crashes as a result;
- **Driver patches cannot be applied** (no host-kernel module compile entry point) → PCIe P2P can
  never be enabled;
- WSL2's default 8 GB host-memory cap makes host memory go negative after the model loads.

Performance baseline: WSL2 TP measured 25% slower than PP (TP+MTP single-stream 31 tok/s vs
PP 44.4 tok/s) — because without P2P, inter-card communication falls back to the
VRAM→host memory→VRAM SHM path (measured effective per-card PCIe bandwidth ~12.5 GB/s). Whole-card
throughput lands in the 50–80 tok/s range; the research conclusion was that migrating to native
Ubuntu was expected to deliver 30–50% gains and eliminate every WSL2-specific issue above. During
08-18 we also ran SGLang in parallel: single-stream and 6-concurrent numbers were uniformly worse
than vLLM (vLLM 50/240 tok/s), and TP was killed by PCIe the same way. **Engine choice stayed on
vLLM.**

**Baseline takeaway**: on consumer-grade dual cards for inference, "can it do P2P" matters an
order of magnitude more than "which engine to pick."

### 2.2 PP vs TP: Why P2P Is the Linchpin

Two 3090s without NVLink communicate only over PCIe. The two parallel modes move a very different
amount of data per step:

- **PP=2**: Each card holds half the layers, exchanging hidden states once per step (small,
  infrequent);
- **TP=2**: Each card holds slices of all layers, **performing all-reduce after every layer**
  (large, every layer).

Without P2P, all-reduce falls back to SHM (through host memory), and decode is capped at the
~12.5 GB/s bandwidth — so TP ends up slower than PP. With BAR1 P2P enabled, inter-card DMA is
direct (the patch reduced P2P latency from 15.23 µs to 1.01 µs in our measurements), and vLLM's
CustomAllreduce fast path finally works.

**There is a hard constraint beyond performance**: the vision encoder's weights only support TP
sharding (`--mm-encoder-tp-mode weights`, see §3.2). Under PP=2, the ViT must sit whole on one
card; at the 100M budget the per-video vision activations (~6.6 GiB) all land on that one card and
OOM. **For this workload, TP is the only viable parallel mode; P2P decides how fast TP runs.** The
whole upgrade project therefore means: first make TP faster than PP, and only then do the other
parameter tunings have a platform.

### 2.3 Driver Patch: aikitoria P2P (Consumer Cards Lack MAILBOXP2P, Routed via BAR1)

Principle: enterprise cards P2P over MAILBOXP2P hardware; the consumer 3090 has none. The
aikitoria patch (originating from geohot's tinygrad work) modifies 6 kernel source files to reroute
peer transfers to the opposite card's BAR1 (PCIe MMIO window): `p2pOverride=0x11`,
`forceP2PType=BAR1P2P`, and GMMU's PEER aperture is remapped to `SYS_NONCOH`.

**Actual installed version combination**:

| Component | Version | Source |
|---|---|---|
| KMD (kernel module) | 610.57.04 (patched) | aikitoria `610.57.04-p2p-v2` (commit `803113dc`, v2 fixes BAR1 P2P dispatch on GA102) |
| UMD (user-space) | 610.57.04 | NVIDIA official `.run --no-kernel-modules` + GSP firmware |
| Kernel | 6.8.0-138-generic | apt, **held with `apt-mark hold`** |

Key steps and pitfalls:

1. **UMD/KMD must match versions**. The original plan was 595.71.05-p2p, but only 595.84 is in
   apt and aikitoria does not support it; "apt 595.84 user-space + 595.71.05 kernel module" hits
   `Driver/library version mismatch`. The final answer was 610.57.04 end-to-end.
2. **GRUB**: `amd_iommu=on iommu=pt` (IOMMU passthrough — otherwise DMA goes through page-table
   translation and P2P fails by definition; the price is weaker device isolation, so this is not
   safe for untrusted workloads).
3. **ACS must be disabled**, but the offset is board-specific: on X570 Taichi the ACS capability
   sits at `0x2a0`, with ACSCtl at **`0x2a6`**. A systemd `disable-acs.service` runs `setpci` at
   boot to persist this.
4. **Patched modules can be overwritten by DKMS/apt**: after installation, `dkms remove nvidia/*`
   must confirm the patched module exclusively owns `/lib/modules/.../nvidia*.ko`; **every kernel
   upgrade invalidates the patch and forces a rebuild — that is why the kernel is held**.
5. **Rollback path**: remove the patched module → apt-install the original driver → restore
   GRUB → remove the ACS service → after reboot `topo -p2p r` should return to `CNS`.

Verification:

```
$ nvidia-smi topo -p2p r
        GPU0    GPU1
  GPU0   X       OK
  GPU1   OK      X
```

### 2.4 Flashing the ReBAR VBIOS: BAR1 256 MiB → 32 GB (Including an Incident)

The P2P patch depends on BAR1 as the transport window. Out of the box, 3090 BAR1 is only 256 MiB;
aperture remapping still works, but per-card DMA bandwidth is capped by the mapped window. After
flashing the ReBAR VBIOS, BAR1 expands to 32 GB and P2P gets full bandwidth.

**Required BIOS settings** (ASRock X570 Taichi): CSM off (ReBAR needs pure UEFI), Above 4G
Decoding on, Re-Size BAR Support / C.A.M. on; driver-side `options nvidia NVreg_EnableResizableBar=1`.

**The two cards are flashed separately**, and the process was bumpy:

- **GPU0 (ASUS, dual BIOS, low risk)**: Smoothly flashed the ReBAR version; BAR1 32 GB took
  effect.
- **GPU1 (Dell OEM, single BIOS, high risk)**: Initially misidentified as an NVIDIA FE; the
  downloaded FE ReBAR ROM was rejected by nvflash with a board ID mismatch. After the user
  confirmed on-site that it is a Dell RTX 3090 (PG132, modified shell, VBIOS-flashed), the match
  was made by three conditions — subsystem `10de:1454` + 350 W + "Applied ReBAR update" — and the
  **Manli Gallardo ROM (`94.02.42.80.20`, TechPowerUp 267013)** was flashed successfully, giving
  BAR1 32 GB.

**Incident: black screen + lost SSH**. Before flashing, to let `rmmod nvidia` write the EEPROM, the
SUID bit on `/usr/bin/nvidia-modprobe` was removed (so it would not pull the nvidia module back
in). **The bit was not restored after flashing**. The result: GDM starts as non-root and cannot
create `/dev/nvidia-modeset` → only the Ubuntu splash appears at boot, the system does not come
up, and the X log reads `Validated MetaModes: NULL`. Recovery: `Ctrl+Alt+F3` to a TTY (after
disabling CSM and enabling Above 4G in the BIOS the system itself can still reach a TTY),
`chmod u+s /usr/bin/nvidia-modprobe`, plus an extra `nvidia-modeset-device.service` (`Before=gdm`,
runs `mknod -m 666 /dev/nvidia-modeset` at boot, idempotent). Single-BIOS card flashing has no
safety net; the whole process depends on a local console (not SSH) plus a backed-up ROM
(`nvflash -b`).

Final state:

```
GPU0: 94.02.42.00.A9   (ASUS,  subsystem 1043:87AF)  BAR1 32 GB
GPU1: 94.02.42.80.20   (Dell,  subsystem 10de:1454)  BAR1 32 GB  (Manli Gallardo ROM)
nvidia-smi topo -p2p r → OK
```

### 2.5 Hardware-Layer Verification Checklist (Run After Every Reboot / Kernel Upgrade)

```bash
cat /proc/driver/nvidia/version          # 610.57.04 Open Kernel Module
nvidia-smi topo -p2p r                   # OK / OK; CNS means the patch is gone
nvidia-smi topo -m                       # PHB topology unchanged
nvidia-smi -q | grep -A3 "BAR1 Memory"   # both cards 32 GB
nvidia-smi -pl -i 0                      # 350 W locked in
docker logs qwen3.5-4bit | grep -i "custom.*all.*reduce"  # Registering N cuda graph (N>0)
```

---

## 3. vLLM Deployment: Tuning Qwen3.8-27B INT4 on Dual 3090

Model: `Qwen3.8-27b-int4` (INT4 quantized, ~10.1 GiB weights per card). Image
`vllm/vllm-openai`, starting at v0.27.1 and bumped to v0.28.0 on 08-28 (which includes the encoder
cache eviction race fix from PR #52482 / #53451).

### 3.1 Final Configuration (As of 09-04)

```yaml
# ~/models/qwen/docker-compose.yaml (Ubuntu host)
services:
  vllm:
    image: vllm/vllm-openai:latest        # v0.28.0
    container_name: qwen3.5-4bit
    ipc: host                              # NCCL shared memory required
    shm_size: 12gb
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 2                     # note: count and device are mutually exclusive
              capabilities: [gpu]
    volumes:
      - ./Qwen3.8-27b-int4:/model:ro
      - ./vllm-cache:/root/.cache/vllm     # persist torch.compile cache, cuts startup time
    ports: ["8000:8000"]
    environment:
      - TZ=Asia/Shanghai
      - PYTORCH_CUDA_ALLOC_CONF=expandable_segments:False   # hard constraint, see §3.3
      - NCCL_SHM_DISABLE=0
      - VLLM_SKIP_P2P_CHECK=1
    command:
      - /model
      - --served-model-name qwen3.5-9b
      - --trust-remote-code
      - --enable-auto-tool-choice
      - --tool-call-parser qwen3_coder
      - --reasoning-parser qwen3
      - --tensor-parallel-size 2           # only fast after P2P is wired up
      - --gpu-memory-utilization 0.9
      - --max-model-len 262144
      - --kv-cache-dtype fp8
      - --dtype float16
      - --max-num-seqs 6
      - --enable-prefix-caching
      - --mm-encoder-tp-mode weights       # vision activations balanced across both cards, the 100M prerequisite
      - --mm-processor-cache-type shm
      - --mm-shm-cache-max-object-size-mb 2048
      # MTP commented out (08-31); reference for re-enabling on v0.28.0:
      # - --speculative-config {"method":"mtp","num_speculative_tokens":4}
    restart: "no"
```

Model-side config (`video_preprocessor_config.json`): `size.longest_edge = 100000000` (100M, see
§4).

### 3.2 Where Every Parameter Comes From

**Why TP=2, not PP=2**. In the WSL2 era we could only run PP (no P2P, TP was 25% slower). With
P2P wired up, TP's per-layer all-reduce runs directly over BAR1 + CustomAllreduce and becomes the
default fastest path. On 08-23, TP=2+MTP was measured to pass the CustomAllreduce registration
phase — but at that time the Dell card's BAR1 was still 256 MB (asymmetric P2P), so the CUDA
graph capture hung; once both cards were flashed to 32 GB the TP=2 path cleared end-to-end. PP
became a fallback option only. The other reason (see §2.2): the vision encoder weights can only be
TP-sharded, so under PP the ViT sits whole on one card and the 100M budget's vision activations
OOM.

**`gpu-memory-utilization 0.9` (the current stable value)**. This number is not "the fraction of
VRAM that KV cache can take"; it is the upper bound vLLM draws for the *combined* pool of
"weights + activations + KV cache + CUDA graph". Since v0.21 there is a CUDA graph memory
profiling path: the log will print hints like "current 0.88 is equivalent to a no-profiling 0.8611;
to keep the same KV budget, raise to 0.8989". **When changing util you must read the equivalent
value from the log, not guess.** The tuning path went 0.85 → 0.88 → 0.9, hugging the limit at each
step, and finally stabilized at 0.9; at every step the startup log's `Available KV cache memory`
and `Actual usage` are broken down to confirm the headroom. KV cache is not capped manually —
vLLM allocates it automatically off the util.

**`--kv-cache-dtype fp8`**. The 3090 (Ampere) has no hardware FP8 compute, but vLLM supports FP8
*storage* for the KV cache — pure VRAM savings, dequantized back to FP16 at decode time, with
negligible precision loss. KV occupancy for the 262144 context is cut in half; this is the single
biggest reason a 24G card can serve a 256k context.

**`--max-model-len 262144`**. The model's native context ceiling is 256k (262144 = 2^18); no YARN
extension (08-24 / 08-31 rounds of research: YARN 2× to 512k is theoretically possible on INT4,
but the long-context performance and forgetting-risk trade-off is not worth it; DeepSeek's 1M is
a different technical path). Of the 256k slot, ~48k vision tokens share the space with text —
plenty for the 60 s sub-chunk use case.

**`--max-num-seqs 6`**. A single vision request eats ~48k prompt tokens, an order of magnitude
more than a text request. 6 concurrent streams is the balance point where "text throughput stays
healthy at 331.8 tok/s" and "vision requests do not starve the KV budget". Text performance
curve (TP=2, MTP off, GPU0 power-locked to 350 W):

| Concurrency | 1 | 2 | 4 | 6 | 8 |
|---|---|---|---|---|---|
| tok/s | 76.7 | 122.7 | 268.3 | 331.8 | 434.7 |

**`--mm-encoder-tp-mode weights` (the key finding for vision tuning)**. Two modes exist:

- `data`: the two cards process *different* images / videos; for a single-video request all
  vision activations land on GPU0 (measured ~6.6 GiB at 100M budget), GPU1 idles at only 4 GB
  occupied;
- `weights`: the vision encoder is sharded by weight, and *the same video is processed across
  both cards*; activations are ~2.9 GiB per card, balanced.

At 100M budget, v0.27.1 on 24G cards sits right at the limit (GPU0 once had only ~150 MiB of
headroom). Under `data`, vision requests with concurrency > 1 always OOM (it OOM-ed at the 64M
budget too). Switching to `weights` balances VRAM and stabilizes single-video requests —
**100M goes from "unusable" to "production-ready"**. The conclusion was confirmed on 08-23 by
reading the per-card VRAM distribution in the logs together with the vLLM source.

**`--mm-processor-cache-type shm` (2048 MiB)**. Processor output (resized frame tensors) is cached
in shared memory to avoid recomputing across requests. The default ceiling is 128 MiB; a 1080p
video serializes to 134 MiB (140,084,023 B) and trips the `mm_input ... too large to cache`
warning — bumped to 2048.

**`--enable-prefix-caching`**. The system prompt and vision template portion are reused across
requests, so the hit rate stays high under text concurrency.

**`--dtype float16`**. INT4 weights with FP16 compute (the model defaults to bf16; the deployment
explicitly specifies float16 to override). The fp16 vs bf16 KV-occupancy and prefill cost was
compared in 08-19 during a 400K context / YARN analysis; float16 held.

**`cudagraph_mm_encoder`: do not enable**. The CUDA Graph capture of the multimodal encoder
occupies ~8.1 GiB and crushes the KV budget; the engine crashes at startup with
`No available memory for the cache blocks`.

### 3.3 Hard Environment-Variable Constraints (Any One Missing → Crash)

| Variable | Value | Why |
|---|---|---|
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:False` | `True` uses CUDA VMM multi-handle, which conflicts with the single-handle CUDA IPC requirement of CustomAllreduce / NCCL (vllm#42609, fix #43923 long-standing open). On WSL2 `True` OOM-crashes; on native Ubuntu it is just as unusable |
| `VLLM_SKIP_P2P_CHECK` | `1` | vLLM's P2P detection mis-reports against the patched driver; trust the driver (vLLM PR #8911) |
| `NCCL_SHM_DISABLE` | `0` | allow SHM fallback (still useful for small decode flows) |
| **Do not** set `NCCL_P2P_DISABLE=1` | — | disabling P2P = TP performance collapses |
| **Do not** use `--disable-custom-all-reduce` | — | P2P is on; CustomAllreduce gains 5–15% on small all-reduce payloads |



---

## 4. Video Parameters: Spending 48k Vision Tokens Most Effectively

This is the section that diverges most from "a normal LLM deployment".

### 4.1 Core Invariant: 100M Pixel Budget → Constant Token Count

`size.longest_edge` in `video_preprocessor_config.json` (currently `100000000` = 100M) is the
**global pixel budget for the entire video (T·H·W)**, not a per-frame edge length. The
processor's `smart_resize` scales every frame by `beta = sqrt(T·H·W / max_pixels)`, so:

```
vision tokens = (T/2)·(H/32)·(W/32) = budget / 2048 ≈ 48.8k
```

**This is independent of how frames and resolution are split** — changing frame count or
resolution does not change the total token count (and therefore does not change VRAM occupancy
or runtime). The only thing that changes the amount of information is "which frames the budget
buys".

N ↔ per-frame resolution trade-off at 100M budget under 2560×1440 (tokens stay ~48k):

| N frames | Per-frame resolution | Vision tokens |
|---|---|---|
| 32 (vLLM default) | 2336×1312 | 47,888 |
| 48 | 1920×1080 | ~48,960 |
| 64 | 1664×928 | 48,256 |
| 96 | 1344×768 | 48,384 |
| 128 | 1152×640 | 46,080 |
| 256 | 832×448 | 46,592 |

Measured `longest_edge` levels (24G card, v0.27.1):

| Level | Result |
|---|---|
| 24M (~416 px) | Runs, but heavily color-blocked, no recognizable subjects (unusable for surveillance) |
| 64M | Stable |
| 67M | **OOM, unstable** (history of multiple downtimes) |
| 100M | **Production level**. v0.27.1 sits at the edge (GPU0 headroom ~150 MiB); the `weights` mode is the stability prerequisite; v0.28.0 improved headroom |
| 300M (1440 px) | tokens balloon to ~70k, no concurrency possible, dropped |

### 4.2 The Default 32-Frame Sampling Problem: Aliasing

vLLM defaults to 32 uniformly sampled frames (`VideoMediaIO.num_frames=32`). On a 5-minute chunk
that is one frame every 7.5 seconds; on a 239 s production chunk analyzed at 2 fps over 482
frames, we observed two problems:

1. **Duplicated frames waste the budget**. The scene is mostly static (pHash frame-distance
   p50=0); many of the 32 frames are nearly identical;
2. **Aliasing misses events**. A 7.5-second interval means **a 4–5 second transient event is
   missed entirely**. Real example: an orange tabby cat ran through the living room between
   01:43–01:48, and the 32-frame config did not see it at all.

A/B measurements (same chunk, same question, same 100M budget):

| Variant | Frames | Per-frame resolution | prompt_tokens | Latency | Tabby (4 s transient) |
|---|---|---|---|---|---|
| 32 uniform (status quo) | 32 | 2336×1312 | 48,114 | 53.3 s | ✗ missed |
| 64 keyframes (change-point top34 ∪ 8 s period) | 64 | 1664×928 | 48,642 | 60.7 s | ✓ 01:43–01:48 |
| 136 keyframes (all change points) | 136 | ~1120×630 | 48,335 | 77.4 s | ✓ (no real gain) |

Conclusion: token occupancy is nearly identical across all three (the budget invariant holds);
N=64 doubles the time coverage vs N=32, captures the transient, and keeps 78% of per-frame
detail; N=136 has negative marginal returns. **The optimal frame count for a 5-minute chunk is
~64, not 32.**

### 4.3 Production Decision: raw_mp4 + `num_frames=120`

During research (`.video_research/`) we built the full client-side keyframe pipeline: single-pass
ffmpeg decode at 2 fps → MAD / pHash change-point detection (`mad>1.0` or `phd>6`) ∪ 8-second
period frames → top-64 keyframes stored as 1080p JPEG (q85–88) → the request body carries
`media_io_kwargs: {fps, total_num_frames, frames_indices (ascending), num_frames: -1}` so every
frame carries its real time position (the model's reported timestamps line up with on-screen
captions). On the NAS (N5105, 4-core soft decode) this costs ~25–30 s per 5-minute chunk, about
10% of pipeline time; the worker never falls behind.

**But production ultimately locked onto the raw_mp4 single path**:

- Analysis granularity was changed to **60-second sub-chunks**: 60 s of footage contains ~1200
  source frames (~20 fps); `num_frames=120` lets vLLM's uniform sampling cover the *entire*
  segment (one frame per 0.5 s, so a 4–5 second transient is necessarily included) — the
  "missed events" problem the keyframe path set out to solve simply dissolves at 60 s
  granularity;
- 120 frames under the 100M budget correspond to ~1200×675 per frame (the 128-frame band is
  1152×640); detail recognition is workable (person / activity recognition is fine; face-level
  detail is the deliberate trade-off);
- Zero ffmpeg decode and zero cv2 / numpy dependencies on the client; payload construction is
  read-file + base64, and vLLM is unchanged;
- `RAW_MP4_NUM_FRAMES = 120` is a code constant, and any `video_preprocess_mode` value other
  than `raw_mp4` is rejected at the 422 layer. All keyframe code / config surface is removed.

**Two request-level gotchas (vLLM-side)**:

- **`num_frames` must be passed explicitly**: if omitted, the default 32-frame cap kicks in
  (64 / 120 frame requests are silently truncated);
- **`frames_indices` must be sorted ascending** and consistent with the frame list order (the
  order is the temporal order); `num_frames: -1` means "use exactly the frames you were given".

### 4.4 Why sub-chunk = 60 Seconds

- **Sampling target is fixed, duration derived**: `num_frames=120` is a fixed product decision. To
  get full coverage at a 0.5 s/frame temporal resolution (research-validated as the sweet spot for
  catching 4–5 second transient events without wasting budget), the sub-chunk must be
  ≤ 120 × 0.5 s = 60 s; we take the upper bound 60 s, which also **minimizes calls per unit of
  footage** (each call carries a fixed ~48k prefill cost; 30 s granularity means double the
  calls and double the fixed overhead);
- **Latency**: a single sub-chunk call takes 60–80 s (prefill-dominated). 1 minute of footage =
  1 call, realtime ratio ~1.0–1.3× — fine for batch backfill of NAS backlog, where the product
  explicitly "accepts latency", and concurrency=1 can absorb it. Only if 24/7 real-time coverage
  is required does concurrency need to be raised (subject to the §3.2 vision OOM constraint, in
  combination with weights mode + client-side rate limiting);
- **Checkpoint granularity**: checkpoints are persisted per sub-chunk (fingerprint = prompt +
  video sha256 + offset + file list); on failure only un-successful sub-chunks are retried, and
  the 60 s granularity keeps retry cost manageable;
- **Time alignment**: events are written back with `base_offset_seconds =
  sub_chunk.start_offset_seconds` (absolute session time); at 60 s granularity, event timestamps
  naturally achieve the 1-second precision that matches on-screen captions.

### 4.5 Strategy for Longer Durations (Research Conclusions, Reserved)

Core finding: **after dedup, the token cost is proportional to the number of events, not to the
video duration**.

| Chunk duration | Measured event rate | Suggested N | Per-frame resolution |
|---|---|---|---|
| 5 minutes (research-stage production chunk) | ~135 change points / 239 s | 64 | 1664×928 |
| 10 minutes | ~300 change points | 128 | 1152×640 |
| 30 minutes+ | ~900 change points | one stage is not enough | — |

For 30 minutes and above we recommend a two-stage approach (each stage is a standard 48k call):
coarse scan (N=128, just asking "which time windows contain events") → for each event window do
a fine-grained pass (a standard 5-minute-tier call). Cost = (1 + k) × 48k, where k is the number
of event windows. Quiet scenes (<40 change points) can drop to N=48 (FHD per-frame); the budget
is unchanged, more frames traded for more detail.

### 4.6 Request-Body Best Practices (Current video_dairy State)

```json
{
  "model": "qwen3.5-9b",
  "messages": [
    {"role": "system", "content": "<system prompt>"},
    {"role": "user", "content": [
      {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,..."}},
      {"type": "text", "text": "<user prompt>"}
    ]}
  ],
  "temperature": 0,
  "max_tokens": 8192,
  "response_format": {"type": "json_object"},
  "chat_template_kwargs": {"enable_thinking": false},
  "media_io_kwargs": {"video": {"num_frames": 120}}
}
```

Key points:

- **`chat_template_kwargs` belongs at the request body's top level** (wire format), not nested
  in the OpenAI client's `extra_body` — wrong location means `enable_thinking` is silently
  ignored (the project auto-injects this for Qwen-family models in `openai_client.py`);
- **`temperature=0` + `json_object`**: event extraction is a structured task, so we want
  determinism, not creativity; the 0.8 setting is reserved for general dialogue (such as
  secondary calls in the Q&A chain);
- **`max_tokens=8192` does not act as a server-side output cap**: output length is controlled by
  the model's own ceiling combined with the per-request `max_tokens`. We do *not* add a global
  `--max-output-tokens` style cap on the vLLM side (it would affect other clients);
- **Optional `mm_processor_kwargs: {"max_pixels": N}`**: per-request override of the pixel
  budget (e.g., 64M); the default follows the model's 100M. We do not add it in this project.

---

## 5. Client-Side (video_dairy) LLM Engineering Practices

Above hardware and engine, the application layer is what decides "same model, same cards, results
that differ by several times".

1. **One file, one call**: each video file in a session is its own sub-chunk, read and
   base64-encoded directly (`build_video_data_url`) with no cross-file concat.
   `base_offset_seconds` is the file's start offset inside the session. The real per-file duration
   is probed with `ffprobe` at ingest.
2. **Hard-coded parameters, not configurable**: `RAW_MP4_NUM_FRAMES = 120` is a constant. Any
   parameter that encodes a product decision lives as a code constant + regression test
   (`test_analyzer_raw_mp4_payload.py` asserts the payload prefix, the `num_frames` value, and
   that the 422 layer rejects the `keyframe` mode) — not in drifting config.
3. **Checkpoint resume + fingerprint fence**: every sub-chunk's LLM result is written to
   `SessionAnalysisCheckpoint` (unique work key session + analysis_run + sub_chunk, storing input
   fingerprint, state, event payload, token usage, error). An input-fingerprint change (prompt
   two segments + video sha256 + offset + file list) invalidates the entry — **changing the
   prompt or the video cannot accidentally reuse a stale result**. On mid-run failure the
   session enters `PARTIAL`; rerun resumes from the first non-success checkpoint and never
   re-charges a successful chunk. `analysis_run_id` (sha256 of file path + size + mtime)
   serves as a fence, rejecting late-arriving worker writes.
4. **Token usage end-to-end bookkeeping**: every call's prompt / completion / total is recorded
   in `LLMUsageLog`, attributed per session + checkpoint — costs are auditable and traceable to
   specific footage.
5. **Concurrency = 1 is a design choice, not a limit**: the vision worker has its own queue
   (`analysis_hot` / `analysis_full`, `concurrency=1`). Rationale: under the batch-backfill
   scenario, 1 minute of footage ≈ 1 call ≈ 60–80 s, so `concurrency=1` is enough to absorb
   typical volumes (the product accepts latency); meanwhile vision-request concurrency is
   exactly the OOM source (the §3.2 weights mode). **Application-side rate limiting is more
   reliable than server-side concurrency caps** — the server's `max-num-seqs=6` is left to the
   text path, with vision traffic serialized by the client queue.
6. **Provider-agnostic**: the LLM goes through the OpenAI-compatible protocol (the
   `LLMProvider` table + `OpenAIClient`); local vLLM, cloud, MiniCPM can all be plugged in.
   Vision-capability probing (`probe_vision`) and tool-calling probing (`probe_tool_calling`)
   are independent of the text path. The video-preprocess mode is locked to `raw_mp4` at the
   schema layer (422 rejects other values), eliminating the invisible "configurable but
   ineffective" state.
7. **Failure forensics**: the LLM's raw response text is captured *before* the parser runs, and
   the raw text is logged on parse failure — when troubleshooting you can see exactly what the
   model emitted (parser bug vs model bug is obvious at a glance).
8. **Layered retry policy**: HTTP-layer exponential backoff retries only retryable status codes
   and connection errors; the Celery layer only auto-retries PG deadlocks (`40P01` / `40001`)
   three times. Everything else is left to task_maintenance's timeout-recovery / lease
   machinery. LLM call failures are not retried blindly (cost consideration); instead the
   checkpoint-resume path picks them up on the next dispatch.
9. **Closed-loop observability**: vLLM `/metrics` + Grafana (TTFT / ITL / E2E, KV, prefix hit
   rate) + the project's `/metrics` (outbox latency, task-recovery counters, checkpoint
   progress) + structured JSON logs (correlation_id traverses API → outbox → worker). **Every
   tuning decision must be traceable to a dashboard panel.**

---

## 6. Pitfall List (In Order of Occurrence)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | `expandable_segments:True` OOMs on WSL2 | dxgkrnl VMM allocation path conflicts with NCCL IPC's single-handle requirement (vllm#43923) | Move to native Ubuntu; set `False` globally |
| 2 | TP 25% slower than PP on WSL2 | No P2P; all-reduce falls back to SHM (~12.5 GB/s) | Move to native + driver patch to enable P2P |
| 3 | SGLang HiCache crashes at startup | `nvidia-smi topo` unavailable on WSL2 | Drop SGLang |
| 4 | `Driver/library version mismatch` | Mixing apt 595.84 user-space + 595.71.05 kernel module | Keep UMD / KMD on the same version (610.57.04) |
| 5 | `topo -p2p r` shows CNS | DKMS stock module overwrote the patched module; or kernel upgrade without rebuild | `dkms remove` + rebuild; `apt-mark hold` the kernel |
| 6 | P2P bandwidth too low | ACS not disabled; *and* the offset is board-specific (X570 is 0x2a6, not 0x116) | systemd `setpci` to persist |
| 7 | Black screen and lost SSH after VBIOS flash | Removed `nvidia-modprobe` SUID before flashing and forgot to restore; GDM cannot create `/dev/nvidia-modeset` | TTY (F3) to restore SUID + `nvidia-modeset-device.service` as a safety net |
| 8 | Dell card ReBAR ROM rejected | Misidentified as NVIDIA FE, board ID mismatch | Match Manli Gallardo ROM by subsystem + power + ReBAR label |
| 9 | TP=2 hangs at CUDA graph capture | Asymmetric P2P (32 GB + 256 MB), `cudaErrorMapBufferObjectFailed` | Flash both cards to 32 GB |
| 10 | Throughput 20% below expectation | ASUS card 390 W factory cap exceeds board power delivery, `hw_power_brake_slowdown` throttles clocks to ~1400 MHz | `-pl 350` + systemd service to persist at boot |
| 11 | `mm_input ... too large to cache` | 1080p video serializes to 134 MiB > default 128 MiB shm cache ceiling | `--mm-shm-cache-max-object-size-mb 2048` |
| 12 | Vision request concurrency OOMs (also at 64M) | `data` mode: single-video activations all land on GPU0 | `--mm-encoder-tp-mode weights` |
| 13 | 100M tier crashes at startup | Accidentally enabled `cudagraph_mm_encoder` (8.1 GiB crushes KV) | Disable the flag |
| 14 | 120 frames truncated to 32 | `num_frames` not passed, vLLM default cap kicks in | Pass `num_frames` explicitly (120 / -1) |
| 15 | 4–5 second transient events missed | Aliasing from 32-frame uniform sampling | sub-chunk 60 s + `num_frames=120` for full coverage |
| 16 | MTP + vision combination unstable | v0.27.1 encoder cache eviction race | Disable MTP on v0.27.1; v0.28.0 fixed it, re-enable reference documented |
| 17 | Huge pre-output thinking, prefix-cache hit rate drops to zero | The official template injects `reasoning_effort: xhigh` by default | Replace the chat template (default medium) + `--chat-template` / `--default-chat-template-kwargs` |
| 18 | Client gets 400 bad request | opencode sends Anthropic-format payloads to the OpenAI endpoint | Fix on the client side (server unchanged) |

---

## 7. Reproduction Checklist

To reproduce this configuration on your own consumer dual-card (24G×2) box:

**Hardware prerequisites**: both cards under the same PCIe root complex (`nvidia-smi topo -m`
shows PHB / PIX; SYS means P2P is moot), motherboard BIOS supports ReBAR (CSM off, Above 4G on,
Re-Size BAR on), Gen3 x8+ link or better.

**Order** (verify at every step before moving on):

1. Native Linux (do not run inside WSL2 — patches cannot be applied);
2. Driver patch (aikitoria p2p branch; UMD / KMD same version; `iommu=pt`; disable ACS; hold
   the kernel) → `topo -p2p r` returns OK;
3. Flash the ReBAR VBIOS (**both cards**, no exceptions; back up first, operate from a local
   console, single-BIOS cards need a CH341A as a safety net) → both cards BAR1 32 GB;
4. Power check: `nvidia-smi -q -d PERFORMANCE` for throttling, and on OC cards cap power to the
   actually measured delivery capability and persist at boot;
5. vLLM compose (§3.1); every hard environment-variable constraint in §3.3 must be present;
6. Verify: CustomAllreduce registration log, balanced VRAM across both cards (under a
   single-video request), `/metrics` throughput compared to the WSL2 baseline (expect
   +30–50%);
7. Model-side `longest_edge=100M` + `weights` mode; use real footage to A/B test detail
   recognition and VRAM headroom;
8. Client side: follow §4.3 / §5 (60 s sub-chunks, `num_frames=120`, checkpoint resume,
   concurrency=1).

**Order-of-magnitude cost** (dual 3090, this configuration): text throughput 76.7–434.7 tok/s
(1–8 concurrent); video analysis 1 minute of footage = 1 call ≈ 60–80 s (~48k vision tokens per
call), 10 minutes of footage ≈ 10–13 minutes of LLM time (concurrency=1, realtime ratio
~1.0–1.3×) — acceptable for batch-backfill scenarios.

---

## 8. Future Directions

- **Re-enable MTP** (the v0.28.0 race is fixed): compare 3 vs 4 speculative tokens (position
  acceptance rates 0.73 / 0.61 / 0.46 / 0.38, marginal at the 4th); watch the cudagraph PIECEWISE
  degradation and the `max_num_scheduled_tokens=2048` impact on prefill;
- **MoE alternative**: Qwen3.6-35B-A3B (dual 3090 AWQ-INT4 TP=2 VRAM / throughput modeling
  completed, 08-27 research) — if 27B dense throughput becomes the bottleneck, MoE is the main
  alternative path;
- **Larger vision models**: investigate the DeepSeek-V4-Flash-Vision dual-3090 case (09-01);
- **Engine-side EVS** (`--video-pruning-rate` + `--video-pruning-method evs`): only trims the
  post-ViT LLM-side compute, leaving the 100M budget / detail / VRAM untouched — worth
  evaluating for future throughput gains;
- **Keyframe pipeline resurrection**: ADR 0009's removal is a product decision. If 30-minute+
  long-form two-stage strategy is ever needed again, opencv / numpy and the entire pipeline
  will have to be re-introduced (under a new ADR); the research scripts in the ops repo's
  `.video_research/` are reproducible.
