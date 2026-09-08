# 家庭监控视频理解：双 3090 × Qwen3.8-27B 本地多模态部署最佳实践

> 版本：中文 v1（2026-09-04）
> 素材来源：ops 仓库（`~/research/ops`）2026-08-18 ~ 2026-09-04 的 20+ 个 opencode 会话记录、
> `UPGRADE_P2P_RTX3090.md`、`.video_research/REPORT_video_preprocessing.md`、`AGENTS.md`（运维现状），
> 以及 video_dairy 仓库 ADR 0009 与当前代码实现。
> 文中所有数字均为实测或日志实读值，非理论估算。

---

## 0. 摘要

video_dairy 是一个离线家庭监控视频分析系统：NAS 里的摄像头录像（2560×1440）被切成 60 秒的
sub-chunk，每块以 raw mp4 base64 连同 `num_frames=120` 发给本地多模态大模型，模型输出结构化
JSON 事件。整个链路要同时满足三个矛盾需求：

- **分辨率**——要认人、认脸，不能糊；
- **时长**——单块 1 分钟（生产切块）甚至更长的录像要覆盖全；
- **成本与延迟**——完全离线、不积压、token 成本可控。

为此我们走了完整的硬件+软件路线：

1. **硬件**：双 RTX 3090（24G×2）从 WSL2 基线迁到原生 Ubuntu，打 aikitoria 驱动补丁开启
   PCIe P2P，刷 ReBAR VBIOS 把两卡 BAR1 从 256 MiB 扩到 32 GB，锁 350W 功耗消除板级保护降频。
2. **部署**：vLLM TP=2 + Qwen3.8-27B INT4，`gpu-memory-utilization=0.9`、FP8 KV cache、
   262144 上下文、`--mm-encoder-tp-mode weights`，最终文本吞吐单流 76.7 tok/s、8 并发 434.7 tok/s。
3. **视频参数**：吃透"100M 像素预算 → 视觉 token 恒 ≈48.8k"的不变量，用 60 秒 sub-chunk +
   120 帧采样取代默认 32 帧均匀采样（消除 4~5 秒瞬态事件的采样混叠），关键帧方案经 A/B 验证后
   作为研究结论保留、生产锁定 raw_mp4 单一路径。

全文按"硬件 → 部署 → 视频 → 客户端"四层展开，每层都给出**最终配置**、**为什么是这个值**、
**试过但放弃的方案**和**坑清单**。

---

## 1. 背景：video_dairy 对 LLM 的工作负载画像

```
视频目录 → VideoSource → VideoFile → VideoSession → EventRecord → DailySummary
                                                    → Chat / MCP / Webhook
```

与 LLM 直接相关的链路：

1. 扫描、去重、Session 合并与封口（纯 CPU，不涉及模型）；
2. **Session 分析**：`SEALED` 的 Session 按 600 秒切块（`ANALYZER_SEGMENT_SECONDS`），
   每块再按 60 秒切 sub-chunk（`ANALYZER_LLM_CHUNK_SECONDS`，默认 60），**每个 sub-chunk 一次
   视觉模型调用**；
3. 家庭日报、问答、MCP 基于结构化事件做二次 LLM 调用（纯文本，成本低，本文不展开）。

一次视觉调用的负载画像（这是后面所有调参的约束来源）：

| 维度 | 值 | 来源 |
|---|---|---|
| 输入分辨率 | 2560×1440（小米摄像头，约 20 fps） | 生产录像实测 |
| 单 sub-chunk 时长 | 60 s | 项目常量 |
| 视觉 token | ≈ 48.8k（100M 像素预算 / 2048，不变量） | §4.1 推导 |
| 输出 | 结构化 JSON 事件（`temperature=0`、`json_object`、`max_tokens=8192`） | `sub_chunk_runner.py` |
| 并发 | vision worker `concurrency=1`（`analysis_hot/analysis_full`） | 部署配置 |
| 调用频率 | 每 60s 素材 1 次调用（1 分钟 = 1 次）；10 分钟素材 = 10 次 | 推导 |
| 延迟要求 | 批量离线（NAS 积压素材回补），产品明确接受延迟；
  concurrency=1 下 1 分钟素材 ≈ 60~80s LLM 时间（实时比 ~1.0~1.3×） | 产品决策 |

关键点：**每 48k 视觉 token 的信息量**决定了识别质量，而 48k 又是显存/耗时硬约束下的最大值。
整个调优过程本质上就是"在 48k token 不变量的前提下，把这 48k 花得最值"。

---

## 2. 硬件基础：双 3090 升级全程

目标机：原生 Ubuntu 22.04（），双 RTX 3090（24G×2），，
双卡经 PCIe Host Bridge（PHB）直连 CPU，无 NVLink（买不起），Gen4 x8/x8 分线。两卡身份：

| 卡 | PCI | subsystem | 型号 | 特点 |
|---|---|---|---|---|
| GPU0 | 0e:00.0 | `1043:87AF` | ASUS ROG-STRIX-3090-O24G | 出厂 OC 390W、双 BIOS |
| GPU1 | 0f:00.0 | `10de:1454` | Dell OEM 3090（PG132，改过外观与 VBIOS） | 单 BIOS、350W |

### 2.1 WSL2 基线：为什么必须迁走（只说结论）

最初的部署在 Windows 桌面的 WSL2 里（同样是双 3090），基线问题一句话：**WSL2 的 GPU 走
dxgkrnl 虚拟化层，拿不到宿主内核的控制权**，直接后果有四个：

- `expandable_segments:True` 崩溃（CUDA VMM 多 handle 与 NCCL/CustomAllreduce 的 IPC 单
  handle 冲突，vllm#43923 长期 open）——只能 `False`，显存碎片化无解；
- `nvidia-smi topo` 直接不可用（dxgkrnl 不暴露拓扑）——SGLang HiCache 的 NUMA 探测因此崩溃；
- **无法打驱动补丁**（没有宿主内核模块编译入口）→ PCIe P2P 永远开不了；
- WSL2 内存默认只有 8 GB 上限，模型加载后主机内存直接为负。

性能基线：WSL2 上 TP 模式实测比 PP 慢 25%（TP+MTP 单流 31 tok/s vs PP 44.4 tok/s）——因为
没有 P2P，卡间通信只能走 显存→主机内存→显存 的 SHM 路径（实测单卡 PCIe 有效带宽 ~12.5 GB/s）。
整卡吞吐 50~80 tok/s 量级，调研结论是迁移原生 Ubuntu 预期提升 30~50%，且消除上述全部
WSL2 特有问题。08-18 期间还并行试过 SGLang：单流/6 并发全面慢于 vLLM（vLLM 50/240 tok/s），
TP 同样被 PCIe 拖死，**引擎选择维持 vLLM**。

**基线教训**：消费级双卡做推理，"能不能 P2P"比"引擎选哪个"影响大一个量级。

### 2.2 PP vs TP：P2P 为什么是命门

双 3090 无 NVLink，卡间只有 PCIe。两种并行模式每步的卡间流量完全不同：

- **PP=2**：每张卡放一半层，每 step 只传一次 hidden states（小、低频）；
- **TP=2**：每张卡放全部层的切片，**每层后都要 all-reduce**（大、每层一次）。

没有 P2P 时 all-reduce 走 SHM（经主机内存），decode 被 ~12.5 GB/s 的带宽锁死，TP 比 PP 还慢。
开启 BAR1 P2P 后，卡间 DMA 直连（patch 实测 P2P 延迟 15.23µs → 1.01µs），vLLM 的
CustomAllreduce 快速路径才真正可用。

**性能之外还有一条硬约束**：视觉编码器的权重只支持 TP 切分（`--mm-encoder-tp-mode weights`，
见 §3.2）。PP=2 下 ViT 只能整块放在一张卡上，100M 预算下单个视频的视觉激活 ~6.6 GiB
全落一卡，必然 OOM——**本项目负载下 TP 是唯一可行的并行模式，P2P 决定的是 TP 快不快**。
整个升级项目的意义因此是：先让 TP 变得比 PP 快，其余参数调优才有平台。

### 2.3 驱动补丁：aikitoria P2P（消费卡没有 MAILBOXP2P 硬件，用 BAR1 绕行）

原理：企业卡 P2P 走 MAILBOXP2P 硬件，消费 3090 没有。aikitoria 补丁（源自 geohot 的
tinygrad 工作）改 6 个内核源文件，把 peer 传输重路由到对端 BAR1（PCIe MMIO 窗口）：
`p2pOverride=0x11`、`forceP2PType=BAR1P2P`，GMMU 的 PEER aperture 重映射为
`SYS_NONCOH`。

**实际执行版本组合**：

| 组件 | 版本 | 来源 |
|---|---|---|
| KMD（内核模块） | 610.57.04（patched） | aikitoria `610.57.04-p2p-v2`（commit `803113dc`，v2 修复 GA102 的 BAR1 P2P dispatch） |
| UMD（用户态） | 610.57.04 | NVIDIA 官方 `.run --no-kernel-modules` + GSP firmware |
| 内核 | 6.8.0-138-generic | apt，**`apt-mark hold` 锁定** |

关键步骤与踩坑：

1. **UMD/KMD 必须同版本**。原计划 595.71.05-p2p，实际 apt 里只有 595.84 且 aikitoria
   不支持；"apt 595.84 用户态 + 595.71.05 内核模块"会出现 `Driver/library version mismatch`。
   最终整体切 610.57.04。
2. **GRUB**：`amd_iommu=on iommu=pt`（IOMMU 直通，否则 DMA 走页表翻译 P2P 必失败；
   代价是设备隔离弱化，不可用于不可信负载）。
3. **ACS 必须关**，但偏移量要按板子查：X570 Taichi 的 ACS capability 在 `0x2a0`，
   ACSCtl 在 **`0x2a6`**。
   systemd `disable-acs.service` 开机 `setpci` 持久化。
4. **patched 模块会被 DKMS/apt 覆盖**：装完必须 `dkms remove nvidia/*` 确认补丁模块独占
   `/lib/modules/.../nvidia*.ko`；**内核一升级补丁就失效**，必须重新编译，所以内核 hold。
5. **回退路径**：删 patched 模块 → apt 装回原驱动 → 还原 GRUB → 删 ACS service →
   重启后 `topo -p2p r` 应回到 `CNS`。

验证：

```
$ nvidia-smi topo -p2p r
        GPU0    GPU1
  GPU0   X       OK
  GPU1   OK      X
```

### 2.4 刷 ReBAR VBIOS：BAR1 256 MiB → 32 GB（含一起事故）

P2P 补丁依赖 BAR1 作为传输窗口。出厂 3090 的 BAR1 只有 256 MiB，aperture remap 能工作但
单卡 DMA 带宽被映射窗口卡死。刷 ReBAR VBIOS 后 BAR1 扩到 32 GB，P2P 才拿到满带宽。

**前置 BIOS 设置**（ASRock X570 Taichi）：CSM 关闭（ReBAR 要纯 UEFI）、Above 4G Decoding
开、Re-Size BAR Support/C.A.M. 开；驱动侧 `options nvidia NVreg_EnableResizableBar=1`。

**两卡分别刷**，过程很不顺：

- **GPU0（ASUS，双 BIOS，低风险）**：顺利刷入 ReBAR 版，BAR1 32 GB 生效。
- **GPU1（Dell OEM，单 BIOS，高风险）**：最初被误判成 NVIDIA FE，下载的 FE ReBAR ROM
  被 nvflash 以 board ID mismatch 拒绝。用户现场确认是 Dell RTX 3090（PG132，改过外观、
  刷过 VBIOS）后，按 subsystem `10de:1454` + 350W + "Applied ReBAR update" 三条件匹配到
  **Manli Gallardo ROM（`94.02.42.80.20`，TechPowerUp 267013）**，刷入成功，BAR1 32 GB。

**事故：黑屏 + SSH 失联**。刷 VBIOS 前为了让 `rmmod nvidia` 能写 EEPROM，撤销了
`/usr/bin/nvidia-modprobe` 的 SUID 位（防它自动把 nvidia 模块拉回来），**刷完忘了恢复**。
结果 GDM 以非 root 启动时无法创建 `/dev/nvidia-modeset` → 开机只有 Ubuntu 启动界面，
进不了系统，X 日志 `Validated MetaModes: NULL`。恢复路径：`Ctrl+Alt+F3` 进 TTY（BIOS 里
关 CSM 开了 Above 4G 后系统本身能进），`chmod u+s /usr/bin/nvidia-modprobe`，再加一个
`nvidia-modeset-device.service`（`Before=gdm`，开机 `mknod -m 666 /dev/nvidia-modeset` 兜底，
幂等）。单 BIOS 卡刷写没有安全网，全程依赖本地控制台操作（不 SSH）+ 备份 ROM（`nvflash -b`）。

最终状态：

```
GPU0: 94.02.42.00.A9   (ASUS,  subsystem 1043:87AF)  BAR1 32GB
GPU1: 94.02.42.80.20   (Dell,  subsystem 10de:1454)  BAR1 32GB  (Manli Gallardo ROM)
nvidia-smi topo -p2p r → OK
```

### 2.5 硬件层验证清单（每次重启/内核升级后跑一遍）

```bash
cat /proc/driver/nvidia/version          # 610.57.04 Open Kernel Module
nvidia-smi topo -p2p r                   # OK / OK，CNS = 补丁失效
nvidia-smi topo -m                       # PHB 拓扑不变
nvidia-smi -q | grep -A3 "BAR1 Memory"   # 双卡 32 GB
nvidia-smi -pl -i 0                      # 350W 是否在
docker logs qwen3.5-4bit | grep -i "custom.*all.*reduce"  # Registering N cuda graph (N>0)
```

---

## 3. vLLM 部署：Qwen3.8-27B INT4 的双 3090 参数调优

模型：`Qwen3.8-27b-int4`（INT4 量化，权重 ~10.1 GiB/卡）。镜像 `vllm/vllm-openai`，
v0.27.1 起步，08-28 升 v0.28.0（含 encoder cache 驱逐竞态修复 PR #52482/#53451）。

### 3.1 最终配置（09-04 现状）

```yaml
# ~/models/qwen/docker-compose.yaml（ubuntu 机）
services:
  vllm:
    image: vllm/vllm-openai:latest        # v0.28.0
    container_name: qwen3.5-4bit
    ipc: host                              # NCCL 共享内存必须
    shm_size: 12gb
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 2                     # 注意：count 与 device 二选一
              capabilities: [gpu]
    volumes:
      - ./Qwen3.8-27b-int4:/model:ro
      - ./vllm-cache:/root/.cache/vllm     # 持久化 torch.compile 缓存，显著缩短启动时间
    ports: ["8000:8000"]
    environment:
      - TZ=Asia/Shanghai
      - PYTORCH_CUDA_ALLOC_CONF=expandable_segments:False   # 硬约束，见 §3.3
      - NCCL_SHM_DISABLE=0
      - VLLM_SKIP_P2P_CHECK=1
    command:
      - /model
      - --served-model-name qwen3.5-9b
      - --trust-remote-code
      - --enable-auto-tool-choice
      - --tool-call-parser qwen3_coder
      - --reasoning-parser qwen3
      - --tensor-parallel-size 2           # P2P 打通后才快
      - --gpu-memory-utilization 0.9
      - --max-model-len 262144
      - --kv-cache-dtype fp8
      - --dtype float16
      - --max-num-seqs 6
      - --enable-prefix-caching
      - --mm-encoder-tp-mode weights       # 视觉激活均摊两卡，100M 稳定前提
      - --mm-processor-cache-type shm
      - --mm-shm-cache-max-object-size-mb 2048
      # MTP 已注释（08-31）；v0.28.0 重启用参考：
      # - --speculative-config {"method":"mtp","num_speculative_tokens":4}
    restart: "no"
```

模型侧配置（`video_preprocessor_config.json`）：`size.longest_edge = 100000000`（100M，见 §4）。

### 3.2 每个参数的来龙去脉

**为什么 TP=2 而不是 PP=2。** WSL2 时代只能 PP（无 P2P，TP 慢 25%）。P2P 打通后 TP 的
每层 all-reduce 走 BAR1 直连 + CustomAllreduce，成为默认最快路径。08-23 实测 TP=2+MTP
通过 CustomAllreduce 注册阶段——但因当时 Dell 卡 BAR1 还是 256 MB（P2P 不对称），
CUDA graph 捕获卡死；两卡都刷到 32 GB 后 TP=2 全链路打通。PP 从此只是回退选项。
另一个原因（见 §2.2）：视觉编码器的权重只有 TP 切分一条路，PP 下 ViT 整块落在单卡，
100M 预算的视觉激活会直接 OOM。

**`gpu-memory-utilization 0.9`（当前稳定值）。** 这个值不是"KV cache 占显存的比例"，而是
vLLM 为"权重 + 激活 + KV cache + CUDA graph"整体划的显存上限比例。v0.21+ 有 CUDA graph
memory profiling：日志会提示"当前 0.88 等效无 profiling 时的 0.8611，要维持同样 KV 请提到
0.8989"这类换算——**改 util 时必须对照日志里的等效值**，不能拍脑袋。调优路径是
0.85 → 0.88 → 0.9 逐档贴极限，最终稳定在 0.9，每次都用启动日志里的
`Available KV cache memory` 与 `Actual usage` 分解核对余量。KV cache 不手动限制，交给
vLLM 按 util 自动分配。

**`--kv-cache-dtype fp8`。** 3090（Ampere）没有硬件 FP8 算力，但 vLLM 支持 FP8 **存储**
KV cache——纯显存节省，decode 时反量化到 FP16 计算，精度损失可忽略。262144 上下文的 KV
占用直接减半，这是 24G 卡能吃到 256k 上下文的头号功臣。

**`--max-model-len 262144`。** 即模型原生上下文上限 256k（262144 = 2^18），不再做 YARN
扩展（08-24/08-31 两轮调研：INT4 下 YARN 2x 到 512k 理论上可行，但长上下文性能与遗忘
风险不划算；DeepSeek 的 1M 是另一套技术路线）。256k 位置中由 ~48k 视觉 token + 文本
共同占用，对 60s sub-chunk 的场景绰绰有余。

**`--max-num-seqs 6`。** 视觉请求单块 ≈48k prompt token，是文本请求的 10 倍+。6 并发是
"文本吞吐 331.8 tok/s 仍健康 + 视觉请求不饿死 KV"的平衡点。文本性能曲线（TP=2、MTP 关、
GPU0 锁 350W）：

| 并发 | 1 | 2 | 4 | 6 | 8 |
|---|---|---|---|---|---|
| tok/s | 76.7 | 122.7 | 268.3 | 331.8 | 434.7 |

**`--mm-encoder-tp-mode weights`（视觉参数调优的核心发现）。** 两种模式：

- `data`：两卡各处理**不同**的图像/视频；单视频请求时全部视觉激活砸在 GPU0
  （实测 100M 预算下 ~6.6 GiB 全落 GPU0），GPU1 闲到只有 4 GB 占用；
- `weights`：视觉编码器按权重切分，**同一个视频两卡共同处理**，激活 ~2.9 GiB/卡均摊。

100M 预算在 v0.27.1 + 24G 卡上贴着显存极限（GPU0 余量一度只有 ~150 MiB），`data` 模式
下视觉请求并发 >1 必 OOM（64M 档也 OOM 过）。切 `weights` 后两卡显存均衡、单视频稳定，
**100M 从"不可用"变"生产可用"**。08-23 通过日志中两卡显存分布 + vLLM 源码确认的结论。

**`--mm-processor-cache-type shm`（2048 MiB）。** 处理器输出（resize 后的帧张量）走共享
内存缓存，避免多请求重复计算。默认上限 128 MiB，1080p 视频序列化后 134 MiB
（140,084,023 B）直接触发 `mm_input ... too large to cache` 告警——调到 2048。

**`--enable-prefix-caching`。** 系统提示 + 视觉模板部分跨请求复用，文本并发下命中率高。

**`--dtype float16`。** INT4 权重 + FP16 计算（模型默认 bf16，部署显式指定 float16 覆盖）。
08-19 在 400K 上下文/YARN 分析中对比过 fp16/bf16 的 KV 占用与 prefill 成本，最终维持
float16。

**`cudagraph_mm_encoder`：不开。** 实测 CUDA Graph 对多模态编码器的捕获总占用 ~8.1 GiB，
直接挤压 KV cache，引擎启动即崩（`No available memory for the cache blocks`）。

### 3.3 环境变量硬约束（改一个就崩的那几个）

| 变量 | 值 | 原因 |
|---|---|---|
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:False` | `True` 走 CUDA VMM 多 handle，与 CustomAllreduce/NCCL 的 CUDA IPC 单 handle 要求冲突（vllm#42609，修复 #43923 长期 open）。WSL2 上 `True` 直接 OOM 崩溃；原生 Ubuntu 上同样不能开 |
| `VLLM_SKIP_P2P_CHECK` | `1` | vLLM 的 P2P 探测对 patched 驱动误报，信任驱动（vLLM PR #8911） |
| `NCCL_SHM_DISABLE` | `0` | 允许 SHM 回退（纯 decode 小流量场景仍有用） |
| **不要** `NCCL_P2P_DISABLE=1` | — | 禁 P2P = TP 性能大跌 |
| **不要** `--disable-custom-all-reduce` | — | P2P 已通，CustomAllreduce 在小 all-reduce payload 上 +5~15% |



---

## 4. 视频参数：把 48k 视觉 token 花得最值

这是本项目与"普通 LLM 部署"差异最大的部分。

### 4.1 核心不变量：100M 像素预算 → token 恒定

`video_preprocessor_config.json` 的 `size.longest_edge`（当前 `100000000` = 100M）是
**整段视频的全局像素预算（t×h×w）**，不是单帧边长。processor 的 `smart_resize` 按
`beta = sqrt(T·H·W / max_pixels)` 缩放所有帧，于是：

```
视觉 token = (T/2)·(H/32)·(W/32) = 预算 / 2048 ≈ 48.8k
```

**与帧数/分辨率如何分配无关**——改帧数、改分辨率都不改变总 token（也就不改变显存占用和
耗时）。唯一能改变信息量的，是"预算花在哪几帧上"。

2560×1440 下 100M 预算的 N↔单帧分辨率对照（token 恒 ≈48k）：

| N 帧 | 单帧分辨率 | 视觉 tokens |
|---|---|---|
| 32（vLLM 默认） | 2336×1312 | 47,888 |
| 48 | 1920×1080 | ~48,960 |
| 64 | 1664×928 | 48,256 |
| 96 | 1344×768 | 48,384 |
| 128 | 1152×640 | 46,080 |
| 256 | 832×448 | 46,592 |

`longest_edge` 档位实测（24G 卡，v0.27.1）：

| 档位 | 结果 |
|---|---|
| 24M（~416px） | 能跑，但色块化，认不出目标（监控场景不可用） |
| 64M | 稳定 |
| 67M | **OOM，不稳定**（历史多次 down） |
| 100M | **生产档位**。v0.27.1 下贴着极限（GPU0 余量 ~150 MiB），`weights` 模式是稳定前提；v0.28.0 下余量改善 |
| 300M（1440px） | token 暴涨 ~7 万，不可并发，弃用 |

### 4.2 默认 32 帧采样的问题：采样混叠

vLLM 默认从视频均匀采 32 帧（`VideoMediaIO.num_frames=32`）。5 分钟 chunk 下 = 7.5 秒一帧，
两个实测问题（239s 生产 chunk，2fps 采样分析 482 帧）：

1. **重复帧浪费预算**：画面绝大部分时间静止（pHash 帧距 p50=0），32 帧里大量帧几乎相同；
2. **采样混叠漏事件**：7.5 秒间隔 → **4~5 秒的瞬态事件整段漏掉**。实测：一只橘猫
   01:43–01:48 快速跑过客厅，32 帧配置完全没看到。

A/B 实测（同一 chunk、同一问题、同一 100M 预算）：

| 变体 | 帧 | 单帧分辨率 | prompt_tokens | 耗时 | 橘猫（4s 瞬态） |
|---|---|---|---|---|---|
| 32 均匀（当时现状） | 32 | 2336×1312 | 48,114 | 53.3s | ✗ 漏 |
| 64 关键帧（变化点 top34 ∪ 8s 周期） | 64 | 1664×928 | 48,642 | 60.7s | ✓ 01:43-01:48 |
| 136 关键帧（全变化点） | 136 | ~1120×630 | 48,335 | 77.4s | ✓（无实质增益） |

结论：三种配置 token 占用几乎相同（预算不变 ✓）；N=64 相对 N=32 时间覆盖 2×、抓到瞬态
事件、单帧细节保留 78%；N=136 边际收益为负。**5 分钟 chunk 的最优帧数 ≈64，而非 32。**

### 4.3 生产决策：raw_mp4 + `num_frames=120`

研究阶段（`.video_research/`）完整实现了客户端关键帧管线：ffmpeg 单遍 2fps 解码 →
MAD/pHash 变化点检测（`mad>1.0` 或 `phd>6`）∪ 8 秒周期帧 → top-64 关键帧存 1080p JPEG
（q85-88）→ 请求体携带 `media_io_kwargs: {fps, total_num_frames, frames_indices(升序),
num_frames: -1}`，让每帧带真实时间位置（模型报的时间戳与烧录字幕对位）。NAS（N5105，4 核
软解）成本约 25~30s/5 分钟 chunk，占管线 ~10%，worker 不积压。

**但生产最终锁定了 raw_mp4 单一路径**：

- 分析粒度改为 **60 秒 sub-chunk**：60s 素材约 1200 个源帧（~20fps），`num_frames=120`
  让 vLLM 端均匀采样恰好**整段全覆盖**（每 0.5s 一帧，4~5 秒的瞬态事件必然被覆盖）——
  关键帧方案要解决的"漏事件"问题，在 60s 粒度下被直接消解；
- 120 帧落在 100M 预算内对应单帧 ~1200×675（128 帧档为 1152×640），细节识别可行
  （人物/活动识别足够，人脸级细节是权衡掉的）；
- 客户端零 ffmpeg 解码、零 cv2/numpy 依赖，payload 构建就是读文件 + base64，vLLM 端
  零改动；
- `RAW_MP4_NUM_FRAMES = 120` 固化为代码常量，
  `video_preprocess_mode` 非 `raw_mp4` 的值在 422 层直接拒绝，关键帧代码/配置面全部删除。

**两个请求级参数的坑（vLLM 端）**：

- **`num_frames` 必须显式传**：不传则被默认 32 帧上限截断（64/120 帧都白给）；
- **`frames_indices` 必须升序**且与帧列表顺序一致（顺序 = 时间顺序）；`num_frames: -1`
  表示"原样使用给出的全部帧"。

### 4.4 为什么 sub-chunk = 60 秒

- **采样目标固定、时长反推**：`num_frames=120` 是固定的产品决策。要在 0.5s/帧
  的时间分辨率下（研究实测：这是抓住 4~5 秒瞬态事件同时不浪费预算的甜点位）全覆盖，
  sub-chunk 必须 ≤ 120×0.5s = 60s；取上限 60s，同时让**每单位素材的调用次数最少**
  （每次调用有固定的 ~48k prefill 成本，30s 粒度 = 双倍调用 + 双倍固定开销）；
- **耗时**：单 sub-chunk 调用 60~80s（prefill 为主）。1 分钟素材 = 1 次调用，
  实时比 ~1.0~1.3×——对 NAS 积压素材的批量回补场景，产品明确"接受延迟"，
  concurrency=1 可消化；若要求连续 24/7 素材实时跟上，才需要提并发（受 §3.2
  视觉 OOM 约束，需配合 weights 模式 + 客户端限流）；
- **断点粒度**：checkpoint 按 sub-chunk 持久化（指纹 = prompt + video sha256 + offset +
  文件列表），失败重跑只补未成功的 sub-chunk，60s 粒度让重试成本可控；
- **时间定位**：事件回写 `base_offset_seconds = sub_chunk.start_offset_seconds`（绝对
  session 时间），60s 粒度让事件时间戳精度天然达到 1 秒级烧录字幕可对齐。

### 4.5 更长时长的策略（研究结论，备用）

核心结论：**去重后 token 成本与"事件数"成正比，而不是与视频时长成正比**。

| chunk 时长 | 实测事件率 | 建议 N | 单帧分辨率 |
|---|---|---|---|
| 5 分钟（研究期生产切块） | ~135 变化点/239s | 64 | 1664×928 |
| 10 分钟 | ~300 变化点 | 128 | 1152×640 |
| 30 分钟+ | ~900 变化点 | 单阶段不够 | — |

30 分钟以上建议两阶段（每阶段都是一次标准 48k 调用）：粗扫（N=128，只问"哪些时间窗口
有事件"）→ 对每个事件窗口做细看（标准 5 分钟级调用）。成本 = (1 + k) × 48k，k = 事件
窗口数。安静场景（变化点 <40）可降 N=48（FHD 单帧），预算不变、少花帧数多花细节。

### 4.6 请求体最佳实践（video_dairy 现状）

```json
{
  "model": "qwen3.5-9b",
  "messages": [
    {"role": "system", "content": "<系统提示>"},
    {"role": "user", "content": [
      {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,..."}},
      {"type": "text", "text": "<用户提示>"}
    ]}
  ],
  "temperature": 0,
  "max_tokens": 8192,
  "response_format": {"type": "json_object"},
  "chat_template_kwargs": {"enable_thinking": false},
  "media_io_kwargs": {"video": {"num_frames": 120}}
}
```

要点：

- **`chat_template_kwargs` 放请求体顶层**（wire 格式），不是 OpenAI client 的
  `extra_body` 嵌套——放错位置 `enable_thinking` 不生效（项目里 `openai_client.py`
  对 qwen 系模型自动注入）；
- **`temperature=0` + `json_object`**：事件抽取是结构化任务，要确定性，不要创造性；
  通用对话场景（如问答链路的二次调用）才用 0.8；
- **`max_tokens=8192` 服务端不设输出上限**：输出长度用模型自身上限 + 请求级 max_tokens
  控制，不在 vLLM 侧加 `--max-output-tokens` 之类的全局限制（会影响其他客户端）；
- **可选 `mm_processor_kwargs: {"max_pixels": N}`**：请求级覆盖像素预算（如 64M），
  默认走模型配置的 100M，本项目不加。

---

## 5. 客户端（video_dairy）侧的 LLM 调用工程实践

硬件和引擎之上，应用层的实践决定了"同样的模型、同样的卡，结果差几倍"。

1. **两级切块，一次一调**：600s chunk（`ANALYZER_SEGMENT_SECONDS`）→ 60s sub-chunk
   （`ANALYZER_LLM_CHUNK_SECONDS`）。跨文件片段先 `ffmpeg concat` 成单 mp4 再 base64
   （`build_chunk_video_data_url`），保证时间轴连续、`base_offset_seconds` 语义清晰。
2. **参数固化不配置化**：`RAW_MP4_NUM_FRAMES=120` 是常量。凡属产品决策的参数，
   用代码常量 + 回归测试（`test_analyzer_raw_mp4_payload.py` 断言 payload 前缀、
   num_frames 值、schema 422 拒绝 keyframe）锁定，而不是藏在可漂移的配置里。
3. **断点续跑 + 指纹围栏**：每个 sub-chunk 的 LLM 调用结果写入
   `SessionAnalysisCheckpoint`（session + analysis_run + sub_chunk 唯一键，记录输入指纹、
   状态、事件载荷、token 用量、错误）。输入指纹（prompt 双段 + video sha256 + offset +
   文件列表）变化即失效——**改 prompt 或改视频不会误用旧结果**。中途失败 Session 进
   `PARTIAL`，重跑从第一个非 success 断点继续，已成功的分片绝不重复计费。
   `analysis_run_id`（文件 path+size+mtime 的 sha256）作为 fence，拒收迟到 worker 的写入。
4. **Token 用量全链路记账**：每次调用的 prompt/completion/total 归入 `LLMUsageLog`，
   按 session + checkpoint 归属——成本可审计、可归因到具体素材。
5. **并发=1 是设计不是限制**：vision worker 单独队列（`analysis_hot/analysis_full`，
   `concurrency=1`）。依据：批量回补场景下 1 分钟素材 ≈ 1 次调用 ≈ 60~80s，
   concurrency=1 足够消化典型素材量（产品接受延迟）；而视觉请求的并发恰恰是 OOM 源
   （§3.2 weights 模式）。**应用侧限速比服务端限并发更可靠**——服务端
   `max-num-seqs=6` 留给文本链路，视觉流量由客户端队列串行。
6. **Provider 无关**：LLM 走 OpenAI 兼容协议（`LLMProvider` 表 + `OpenAIClient`），
   本地 vLLM、云端、MiniCPM 均可接入；视觉能力探测（`probe_vision`）与工具调用探测
   （`probe_tool_calling`）独立于文本链路。视频预处理模式在 schema 层锁定 `raw_mp4`
   （422 拒绝其余值），杜绝"配置能写但运行时不生效"的隐形状态。
7. **失败留证**：LLM 原始响应文本在 parser 之前捕获，解析失败时原始文本进失败日志——
   排障时能看到模型到底吐了什么（parser bug vs 模型 bug 一眼分清）。
8. **重试策略分层**：HTTP 层指数退避重试可重试状态码与连接异常；Celery 层只对 PG
   死锁（`40P01`/`40001`）自动重试 3 次，其余交给 task_maintenance 的超时恢复/lease 机制。
   LLM 调用失败不盲目重试（成本考虑），靠断点续跑在下次调度补齐。
9. **可观测性闭环**：vLLM `/metrics` + Grafana（TTFT/ITL/E2E、KV、prefix 命中率）+
   项目侧 `/metrics`（outbox 延迟、任务恢复计数、checkpoint 进度）+ 结构化 JSON 日志
   （correlation_id 贯穿 API→outbox→worker）。**每个调参决策都必须能指到一块看板数据。**

---

## 6. 坑清单（按发生顺序）

| # | 现象 | 根因 | 解法 |
|---|---|---|---|
| 1 | WSL2 上 `expandable_segments:True` OOM 崩溃 | dxgkrnl VMM 分配路径与 NCCL IPC 单 handle 冲突（vllm#43923） | 迁原生 Ubuntu；全局 `False` |
| 2 | WSL2 上 TP 比 PP 慢 25% | 无 P2P，all-reduce 走 SHM（~12.5 GB/s） | 迁原生 + 驱动补丁开 P2P |
| 3 | SGLang HiCache 启动崩溃 | `nvidia-smi topo` 在 WSL2 不可用 | 弃 SGLang |
| 4 | `Driver/library version mismatch` | apt 595.84 用户态 + 595.71.05 内核模块混用 | UMD/KMD 整体同版本（610.57.04） |
| 5 | `topo -p2p r` 显示 CNS | DKMS 标准模块覆盖了 patched 模块；或内核升级后未重编 | `dkms remove` + 重编；内核 `apt-mark hold` |
| 6 | P2P 带宽上不去 | ACS 未关；且偏移量按板子查（X570 是 0x2a6 不是 0x116） | systemd setpci 持久化 |
| 7 | 刷 VBIOS 后黑屏、SSH 失联 | 刷写前撤了 `nvidia-modprobe` SUID 忘了恢复，GDM 建不了 `/dev/nvidia-modeset` | F3 TTY 恢复 SUID + `nvidia-modeset-device.service` 兜底 |
| 8 | Dell 卡 ReBAR ROM 被拒 | 误判为 NVIDIA FE，board ID mismatch | 按 subsystem+功耗+ReBAR 标注匹配 Manli Gallardo ROM |
| 9 | TP=2 卡在 CUDA graph 捕获 | P2P 不对称（32GB + 256MB），`cudaErrorMapBufferObjectFailed` | 两卡都刷到 32GB |
| 10 | 吞吐比预期低 20% | ASUS 卡 390W 出厂限幅超板级供电，`hw_power_brake_slowdown` 压时钟到 ~1400MHz | `-pl 350` + 开机持久化 service |
| 11 | `mm_input ... too large to cache` | 1080p 视频序列化 134 MiB > 默认 128 MiB shm 缓存上限 | `--mm-shm-cache-max-object-size-mb 2048` |
| 12 | 视觉请求并发 OOM（64M 也 OOM） | `data` 模式单视频激活全砸 GPU0 | `--mm-encoder-tp-mode weights` |
| 13 | 100M 档启动即崩 | 曾误开 `cudagraph_mm_encoder`（8.1G 挤压 KV） | 关闭该参数 |
| 14 | 120 帧被截成 32 帧 | 未传 `num_frames`，vLLM 默认上限生效 | 显式传 `num_frames`（120 / -1） |
| 15 | 4~5 秒瞬态事件漏检 | 32 帧均匀采样的采样混叠 | sub-chunk 60s + `num_frames=120` 全覆盖 |
| 16 | MTP+视觉组合不稳定 | v0.27.1 encoder cache 驱逐竞态 | v0.27.1 禁用 MTP；v0.28.0 已修复，重启用参考已记录 |
| 17 | 输出前巨量思考、prefix 命中归零 | 官方模板默认 `reasoning_effort: xhigh` 注入 | 替换 chat template（默认 medium）+ `--chat-template`/`--default-chat-template-kwargs` |
| 18 | 客户端 400 bad request | opencode 以 anthropic 格式打 OpenAI 端点 | 客户端侧改配置（服务端不动） |

---

## 7. 复现清单

想在自己的双卡（消费级 24G×2）机器上复现这套配置：

**硬件前提**：双卡同 PCIe 根复合体（`nvidia-smi topo -m` 显示 PHB/PIX；SYS 则 P2P 无意义）、
主板 BIOS 支持 ReBAR（CSM 关、Above 4G、Re-Size BAR 开）、Gen3 x8 以上链路。

**顺序**（每步验证后再进下一步）：

1. 原生 Linux（别在 WSL2 里做，补丁打不了）；
2. 驱动补丁（aikitoria p2p 分支，UMD/KMD 同版本，iommu=pt，关 ACS，hold 内核）→
   `topo -p2p r` 出 OK；
3. 刷 ReBAR VBIOS（**两卡都要刷**；先备份、本地控制台操作、单 BIOS 卡准备 CH341A 兜底）→
   双卡 BAR1 32GB；
4. 功耗检查：`nvidia-smi -q -d PERFORMANCE` 看 throttle，OC 卡按实测供电能力限幅并持久化；
5. vLLM compose（§3.1），环境变量硬约束一个不能少（§3.3）；
6. 验证：CustomAllreduce 注册日志、双卡显存均衡（单视频请求下）、`/metrics` 吞吐与
   WSL2 基线对比（预期 +30~50%）；
7. 模型侧 `longest_edge=100M` + `weights` 模式，用真实素材 A/B 验证细节识别与显存余量；
8. 客户端按 §4.3/§5 接入（60s sub-chunk、`num_frames=120`、断点续跑、并发=1）。

**成本量级**（双 3090，本文配置）：文本吞吐 76.7~434.7 tok/s（1~8 并发）；视频分析
1 分钟素材 = 1 次调用 ≈ 60~80s（~48k 视觉 token/次），10 分钟素材 ≈ 10~13 分钟 LLM
时间（concurrency=1，实时比 ~1.0~1.3×），批量回补场景可接受。

---

## 8. 后续方向

- **MTP 重启用**（v0.28.0 竞态已修）：3 vs 4 个 speculative tokens 对比（位置接受率
  0.73/0.61/0.46/0.38，第 4 位边际已低），注意 cudagraph PIECEWISE 降级与
  `max_num_scheduled_tokens=2048` 对 prefill 的影响；
- **MoE 替代候选**：Qwen3.6-35B-A3B（双 3090 AWQ-INT4 TP=2 显存/吞吐推演已完成，
  08-27 调研）——若 27B 稠密模型吞吐成为瓶颈，MoE 是主要替代路线；
- **更大视觉模型**：DeepSeek-V4-Flash-Vision 双 3090 部署案例调研（09-01）；
- **引擎侧 EVS**（`--video-pruning-rate` + `--video-pruning-method evs`）：只减 ViT 之后
  的 LLM 侧计算，不动 100M 预算/细节/显存，未来提吞吐可评估；
- **关键帧管线复活**：ADR 0009 明确删除是产品决策；若未来要 30 分钟+ 长视频两阶段策略，
  需重新引入 opencv/numpy 与整条管线（走新 ADR），研究脚本在 ops 仓库 `.video_research/`
  可复现。

---
