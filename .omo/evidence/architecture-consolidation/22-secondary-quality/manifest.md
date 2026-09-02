# Todo 22 - Consolidate secondary modules, DTOs and error contracts

Wave 6 converges secondary complexity: it fixes the one mandatory
pre-existing integration failure (daily-summaries generate-all) and splits
five secondary modules / error contracts so each concern is
independently testable, without changing any API response contract.

## Mandatory pre-existing fix (required first)

`tests/integration/test_daily_summaries_http.py::test_generate_all_daily_summaries_route_hits_post_handler`
was failing because the endpoint's `orchestrator` became a FastAPI
dependency (a local variable) while the test monkeypatches
`daily_summaries._pipeline_orchestrator.dispatch_generate_daily_summary`
(a module-level path that no longer existed).

Fix: `src/api/v1/endpoints/daily_summaries.py` now owns a module-level
`_pipeline_orchestrator` built from the composition root's dispatcher and a
`get_orchestrator()` FastAPI dependency (typed
`Orchestrator = Annotated[PipelineOrchestrator, Depends(get_orchestrator)]`)
that returns it. Endpoints keep `orchestrator: Orchestrator`, so direct unit
calls with a mock (existing `test_daily_summaries_endpoint.py`) still work,
while the HTTP + monkeypatch path resolves to the same patchable object.

Result: `test_daily_summaries_http.py` passes (1 passed).

## Scope delivered

1. **QA agent / legacy separation** - `src/application/qa/` now ships
   `legacy.py` (`LegacyQAStrategy`) and `agent_strategy.py`
   (`AgentQAStrategy`); `QAService.answer` resolves provider + gateway and
   dispatches to the strategy selected by `gateway.supports_tool_calling`.
   The `QAService(db, llm_factory=...)` / `.answer(QARequest)` contract and
   the `QAResult` payload are unchanged.

2. **Dashboard query/presenter separation** - `src/services/dashboard.py`
   became a package: `queries.py` (DB gathering) + `presenter.py`
   (`DashboardPresenter`, pure response builders) + `__init__.py`
   (`get_dashboard_overview` and thin `_build_*` wrappers kept for the
   existing unit tests' import surface). No known N+1 was present in the
   touched queries (important-events already joins its source).

3. **Home Profile vision use case** - verified the endpoint already funnels
   through `generate_entity_appearance_use_case` (post-Todo 6); no stray
   direct call remains. No change required.

4. **Provider mutation service** - new `src/application/llm_providers/`
   use cases (`create/update/delete/set_default/enable/disable/test`) return
   typed `ProviderMutationResult`; the endpoint only maps params -> command
   and result -> `BaseResponse`. The C901 on `update_provider` is removed.
   `reset_other_default_providers` moved from `src/api/common.py` to
   `src/services/provider_selector.py` (keeps application off `src.api.*`).

5. **Public get/pagination** - `src/api/common.py` keeps only the typed
   `paginate(...)` helper. Dead `get_or_404` removed. List GET endpoints
   (`video_sources`, `tasks` logs, `llm_providers`, `daily_summaries`) now
   share `paginate`.

6. **Centralized business error enum** - `src/api/error_status.py` now
   exposes `BusinessCode(IntEnum)` with an enum-derived HTTP mapping. Codes
   4000/4001/4002/4004/4011/4290/4291/5000/5001/5002 keep their exact HTTP
   statuses; 4003/4005/5003 stay unmapped (HTTP 200), unchanged. Middleware
   and `status_for_response_code` contract intact.

7. **Cross-boundary JSON DTOs** - response models remain fully typed
   (`BaseResponse[T]`, `PaginatedResponse[T]`); `paginate` is generic over
   the item schema, so no bare `Any` reaches the JSON boundary. Endpoint
   `-> Any` returns are governed by typed `response_model`.

8. **Dead helpers / N+1 / broad exceptions** - dead `get_or_404` removed;
   no N+1 found in touched queries; broad `except Exception` in
   `_parse_intent` / `_generate_answer` preserved as-is (it is the
   pre-existing fallback contract for LLM failures).

## Files

- `src/api/error_status.py` - `BusinessCode` enum + mapping
- `src/api/common.py` - typed `paginate` only
- `src/api/v1/endpoints/daily_summaries.py` - orchestrator seam + paginate
- `src/api/v1/endpoints/llm_providers.py` - thin mapping via use cases
- `src/api/v1/endpoints/tasks.py` - paginate
- `src/api/v1/endpoints/video_sources.py` - paginate
- `src/application/qa/service.py`, `legacy.py`, `agent_strategy.py`
- `src/application/llm_providers/` - mutation use cases
- `src/services/dashboard/` - queries / presenter / `__init__`
- `src/services/provider_selector.py` - `reset_other_default_providers`
- `tests/unit/test_secondary_convergence.py` - 14 new unit tests

## Verification (all green)

- `ruff check .` - passed
- `ruff format --check src tests` - passed (374 files)
- `mypy src` - passed (269 source files)
- `pytest tests/unit -q` - **637 passed**
- `pytest -m postgres -q` - **96 passed**
- `pytest tests/integration/test_daily_summaries_http.py -q` - **1 passed**
- `pytest tests/architecture -q` - 9 passed
- `alembic heads` - `20260902_0021` (single head, no migration added)

## Known pre-existing failure (out of scope)

`test_mcp_streamable_http.py::test_mcp_ask_home_monitor` fails on HEAD
(confirmed via `git stash`): the test's mocked `OpenAIClient.chat_completion`
signature omits `extra_body`, which the QA gateway always forwards. Not
`postgres`-marked, outside the alarmed gates, and predates this Todo. See
`notepads/.../issues.md`.

## Commit

Single commit: `refactor: consolidate application contracts and endpoint logic`
