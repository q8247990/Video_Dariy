# Todo 7 — Task Layer Dependency Injection

## Goal

Wire every Celery task module (`src/tasks/{session_build,analyzer,summarizer,task_maintenance}.py`) and the legacy `src/services/task_retry.py` to the composition-root `Container` exposed by Todo 5. No task module or the retry service may import `src.infrastructure.*` directly; ports only.

## Changes

| File | Change |
|---|---|
| `src/tasks/_container.py` | **new** — module-level production `Container` holder (`get_container` / `set_container_for_tests` / `reset_container_for_tests`), mirroring `src.api.deps._container` and `src/mcp/tools.py`. |
| `src/tasks/session_build.py` | Replace `CeleryTaskDispatcher()` with `get_container().dispatcher`. |
| `src/tasks/task_maintenance.py` | Same swap in `_dispatch_hot_builds` and the lease-expiry requeue path. |
| `src/tasks/summarizer.py` | `_get_pipeline_orchestrator()` wraps `get_container().dispatcher`; daily-summary client uses `get_container().llm_factory.build(...)`. |
| `src/tasks/analyzer.py` | `_build_provider_client()` consumes `get_container().llm_factory`. |
| `src/application/tasks/use_case_retry.py` | **new** — `retry_task()` + `RetryResult` relocated from `src/services/task_retry.py`. Now depends on `TaskDispatcherPort` instead of `PipelineOrchestrator`. |
| `src/application/tasks/use_case_stop_retry.py` | `retry_task_log_use_case` invokes `retry_task(db, row, container.dispatcher)` directly; no `PipelineOrchestrator` import remains. |
| `src/application/tasks/__init__.py` | Re-export `RetryResult` / `retry_task`. |
| `src/services/task_retry.py` | **deleted** (was a services->application violation). |
| `tests/unit/test_tasks_di.py` | **new** — 5 tests proving `session_build`, `task_maintenance`, the container holder, and the `LLMGatewayFactoryPort` binding all route through the composition root with no Redis / broker touch-points. |
| `tests/unit/test_task_maintenance.py` | Switched `monkeypatch.setattr("src.tasks.task_maintenance.CeleryTaskDispatcher", ...)` to `monkeypatch.setattr("src.tasks.task_maintenance.get_container", ...)`. New `_bind_fake_dispatcher` helper. |
| `tests/unit/test_task_retry_service.py` -> `test_task_retry_use_case.py` | Renamed and re-imported `retry_task` / `RetryResult` from `src.application.tasks.use_case_retry`. |
| `tests/architecture/test_dependency_boundaries.py` | Removed 5 `Todo 7` rows + 2 `task_retry.py` rows from `KNOWN_VIOLATIONS`. |

## Import diff

### Removed (`src/tasks/*`)

```diff
- src/tasks/analyzer.py:29    from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
- src/tasks/session_build.py:19 from src.infrastructure.tasks.celery_dispatcher import CeleryTaskDispatcher
- src/tasks/summarizer.py:28  from src.infrastructure.llm.openai_gateway import OpenAICompatGatewayFactory
- src/tasks/summarizer.py:29  from src.infrastructure.tasks.celery_dispatcher import CeleryTaskDispatcher
- src/tasks/task_maintenance.py:22 from src.infrastructure.tasks.celery_dispatcher import CeleryTaskDispatcher
```

### Removed (`src/services/task_retry.py`)

```diff
- src/services/task_retry.py:6  from src.application.pipeline.commands import (AnalyzeSessionCommand, GenerateDailySummaryCommand, SessionBuildCommand)
- src/services/task_retry.py:7  from src.application.pipeline.orchestrator import PipelineOrchestrator
```

### Added

```diff
+ src/tasks/_container.py                 (new module)
+ src/application/tasks/use_case_retry.py (new module)
+ src/tasks/{analyzer,session_build,summarizer,task_maintenance}.py
+     from src.tasks._container import get_container
+ src/application/tasks/use_case_stop_retry.py
+     from src.application.tasks.use_case_retry import retry_task
```

## KNOWN_VIOLATIONS state

Before Todo 7: 10 rows (5 Todo 7 + 4 Todo 5 + 1 Todo 8).
After Todo 7: 3 rows, none owned by Todo 7.

| Owner | Remaining rows |
|---|---|
| Todo 5 | `src/services/prompt_builder/v2/qa_answer.py` -> `src.application.qa.schemas`; `src/services/llm_provider_tester.py` -> `src.infrastructure.llm.openai_gateway` |
| Todo 7 | **0** |
| Todo 8 | `src/core/i18n/__init__.py` -> `src.db.session` |

Verification:

```text
$ grep -rn "from src\.infrastructure\|import src\.infrastructure" src/tasks
# (no output)

$ python3 -m pytest tests/architecture/test_dependency_boundaries.py -q
7 passed in 0.10s
```

## Validation

| Check | Command | Result |
|---|---|---|
| Unit + architecture tests | `pytest tests/unit tests/architecture -q` | **358 passed** |
| Lint | `ruff check .` | clean |
| Types | `mypy src` | no issues found in 189 source files |
| Format | `ruff format --check src tests` | 267 files already formatted; 1 known exception (`tests/unit/test_keyframe_extractor.py`) |
| Architecture | `pytest tests/architecture -q` | 7 passed |
| DI smoke | `pytest tests/unit/test_tasks_di.py -v` | 5 passed |

### Key test evidence

```
tests/unit/test_tasks_di.py::test_session_build_dispatches_via_container PASSED
tests/unit/test_tasks_di.py::test_task_maintenance_dispatch_uses_container PASSED
tests/unit/test_tasks_di.py::test_container_helpers_round_trip PASSED
tests/unit/test_tasks_di.py::test_dispatcher_port_satisfies_protocol PASSED
tests/unit/test_tasks_di.py::test_llm_factory_via_container_uses_fake PASSED

tests/unit/test_task_retry_use_case.py  5 passed  (was test_task_retry_service.py)
tests/unit/test_task_maintenance.py     all dispatch assertions via fake
tests/unit/test_application_use_cases.py::test_retry_task_log_use_case_dispatches_via_container PASSED
tests/architecture/test_dependency_boundaries.py::test_no_undeclared_violations PASSED
```

## Behavioural invariants verified

* `_dispatch_analysis_for_sealed` still walks `sealed_sessions` and builds `AnalyzeSessionCommand(session_id, priority)` for every entry - now via the container's dispatcher port.
* `_dispatch_hot_builds` still calls `dispatch_session_build(SessionBuildCommand(source_id, scan_mode="hot"))` per enabled source - same call signature, same `dispatcher` argument.
* `analyze_session_task` still builds its provider client with `api_base_url / api_key / model_name / timeout_seconds` from the DB row - the LLM factory slot on the container now provides the gateway instead of `OpenAICompatGatewayFactory()`.
* `generate_daily_summary_task` still resolves `GenerateDailySummaryCommand(target_date_str)` and `SendWebhookCommand` via `PipelineOrchestrator` whose dispatcher is `get_container().dispatcher`. Deduped/queued webhook command is unchanged.
* `retry_task` still respects active-dedupe collisions (`4004`), state transitions for analysis retry (`SEALED` from FAILED/PARTIAL/SUCCESS), and queue/priority selection from `detail_json`.

## Out of scope (deliberate)

* `src/services/llm_provider_tester.py` is still a `Todo 5` violation; not touched in this commit.
* `src/core/i18n/__init__.py` remains a `Todo 8` violation.
* `src/tasks/webhook.py` already used no `src.infrastructure.*` symbols - no edit needed.
* `frontend/test-results/` left untouched.