"""Architecture dependency boundary tests (Todo 2 — Wave 0 gate).

This test module is the AST-driven, version-locked characterization of the
target layered architecture described in ``.omo/plans/architecture-consolidation.md``
(Wave 0, Todo 2). It exists **before** any structural cleanup and must stay
green at every commit; it is the only guard that prevents new
adapter-binding regressions from sneaking in.

Layer rules enforced here:

==========================  ========================  =================================
Source layer                Forbidden target module    Owner (per plan)
==========================  ========================  =================================
``src/api/**``              ``src.infrastructure.*``   Todo 6 (API/MCP use cases)
``src/mcp/**``              ``src.infrastructure.*``   Todo 6 (API/MCP use cases)
``src/tasks/**``            ``src.infrastructure.*``   Todo 7 (Celery task DI)
``src/services/**``         ``src.application.*``      Todo 5 (composition root)
``src/core/**``             ``src.db.session``         Todo 8 (boundary enforcement)
==========================  ========================  =================================

Anything not in ``KNOWN_VIOLATIONS`` is a regression and fails the build. New
violations must be added to the table **together with** the Todo that owns the
removal; the table is the contract for Todo 8 to land with zero rows.

Detection is AST-only (no grep/text matching) to dodge comment/docstring
false positives, and walks every ``Import``/``ImportFrom`` node in the file —
including lazy imports inside functions and conditional branches — so a
``from src.infrastructure.foo import bar`` hidden behind ``if settings.FOO:``
cannot hide.
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Architecture rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundaryRule:
    """A single layer-boundary rule.

    Attributes:
        name: Stable identifier used in test ids and error messages.
        source_root: Filesystem root that the source layer scans.
        source_module_prefix: Dotted prefix the source file must live under
            (matched as ``str(path).replace(os.sep, '.')``).
        target_module_prefix: Dotted prefix that is **forbidden** in imports
            from files in the source layer.
        owner_todo: Which Wave 1 todo is responsible for clearing violations
            of this rule. Required, non-empty, must be one of ``Todo 5`` /
            ``Todo 6`` / ``Todo 7`` / ``Todo 8``.
        description: Human-readable description for test failure messages.
    """

    name: str
    source_root: str
    source_module_prefix: str
    target_module_prefix: str
    owner_todo: str
    description: str


RULES: tuple[BoundaryRule, ...] = (
    BoundaryRule(
        name="api_no_infrastructure_import",
        source_root="src/api",
        source_module_prefix="src.api",
        target_module_prefix="src.infrastructure",
        owner_todo="Todo 6",
        description=(
            "API endpoints must route through application use cases; "
            "binding Celery or LLM adapters directly from src/api is forbidden."
        ),
    ),
    BoundaryRule(
        name="mcp_no_infrastructure_import",
        source_root="src/mcp",
        source_module_prefix="src.mcp",
        target_module_prefix="src.infrastructure",
        owner_todo="Todo 6",
        description=(
            "MCP server and tools must call application services; "
            "binding Celery or LLM adapters directly from src/mcp is forbidden."
        ),
    ),
    BoundaryRule(
        name="tasks_no_infrastructure_import",
        source_root="src/tasks",
        source_module_prefix="src.tasks",
        target_module_prefix="src.infrastructure",
        owner_todo="Todo 7",
        description=(
            "Celery task modules must obtain dispatcher / LLM gateway via "
            "the composition root (TaskDispatcherPort / LLMGatewayFactoryPort), "
            "not by importing src.infrastructure.* directly."
        ),
    ),
    BoundaryRule(
        name="services_no_application_import",
        source_root="src/services",
        source_module_prefix="src.services",
        target_module_prefix="src.application",
        owner_todo="Todo 5",
        description=(
            "Services are pure business rules; they must not depend on "
            "application-layer use cases / orchestrators / schemas."
        ),
    ),
    BoundaryRule(
        name="services_no_infrastructure_import",
        source_root="src/services",
        source_module_prefix="src.services",
        target_module_prefix="src.infrastructure",
        owner_todo="Todo 5",
        description=(
            "Adapter binding lives in src/application/bootstrap.py only; "
            "services must not import src.infrastructure.* directly."
        ),
    ),
    BoundaryRule(
        name="core_no_db_session_import",
        source_root="src/core",
        source_module_prefix="src.core",
        target_module_prefix="src.db.session",
        owner_todo="Todo 8",
        description=(
            "src/core must not create or hold ORM Sessions; "
            "i18n DB lookups belong in an application/system-config provider."
        ),
    ),
)


VALID_OWNER_TODOS = frozenset({"Todo 5", "Todo 6", "Todo 7", "Todo 8"})


# ---------------------------------------------------------------------------
# Known violations — explicit, time-bounded exceptions.
#
# Each row is (source_path, target_module, owner_todo, reason). Every entry
# MUST be backed by a real, currently-existing import (see
# ``test_known_violations_resolve_to_real_code``). New rows are forbidden
# outside this baseline contract: see ``test_no_undeclared_violations`` and
# ``test_known_violations_have_valid_owner_todo``.
# ---------------------------------------------------------------------------


KNOWN_VIOLATIONS: tuple[tuple[str, str, str, str], ...] = (
    # ---- api → infrastructure (Todo 6) ----------------------------------
    # Cleared under Todo 6.
    # ---- mcp → infrastructure (Todo 6) ----------------------------------
    # Cleared under Todo 6.
    # ---- tasks → infrastructure (Todo 7) --------------------------------
    (
        "src/tasks/analyzer.py",
        "src.infrastructure.llm.openai_gateway",
        "Todo 7",
        "analyzer directly instantiates OpenAICompatGatewayFactory; replaced "
        "by LLMGatewayFactoryPort binding from composition root under Todo 7.",
    ),
    (
        "src/tasks/session_build.py",
        "src.infrastructure.tasks.celery_dispatcher",
        "Todo 7",
        "session_build directly constructs CeleryTaskDispatcher; replaced "
        "by TaskDispatcherPort binding from composition root under Todo 7.",
    ),
    (
        "src/tasks/summarizer.py",
        "src.infrastructure.llm.openai_gateway",
        "Todo 7",
        "summarizer directly instantiates OpenAICompatGatewayFactory; replaced "
        "by LLMGatewayFactoryPort binding from composition root under Todo 7.",
    ),
    (
        "src/tasks/summarizer.py",
        "src.infrastructure.tasks.celery_dispatcher",
        "Todo 7",
        "summarizer directly constructs CeleryTaskDispatcher; replaced "
        "by TaskDispatcherPort binding from composition root under Todo 7.",
    ),
    (
        "src/tasks/task_maintenance.py",
        "src.infrastructure.tasks.celery_dispatcher",
        "Todo 7",
        "task_maintenance directly constructs CeleryTaskDispatcher; replaced "
        "by TaskDispatcherPort binding from composition root under Todo 7.",
    ),
    # ---- services → application (Todo 5) -------------------------------
    (
        "src/services/task_retry.py",
        "src.application.pipeline.commands",
        "Todo 5",
        "task_retry imports application pipeline commands; will be replaced "
        "by port-driven composition under Todo 5.",
    ),
    (
        "src/services/task_retry.py",
        "src.application.pipeline.orchestrator",
        "Todo 5",
        "task_retry imports application PipelineOrchestrator; service "
        "depends on use-case layer, removed under Todo 5.",
    ),
    (
        "src/services/prompt_builder/v2/qa_answer.py",
        "src.application.qa.schemas",
        "Todo 5",
        "qa_answer prompt builder imports application QA schemas; moved to pure DTOs under Todo 5.",
    ),
    # ---- services → infrastructure (Todo 5 — adapter binding rule) -----
    (
        "src/services/llm_provider_tester.py",
        "src.infrastructure.llm.openai_gateway",
        "Todo 5",
        "llm_provider_tester instantiates OpenAICompatGatewayFactory directly; "
        "binding moved into composition root under Todo 5.",
    ),
    # ---- core → db.session (Todo 8) ------------------------------------
    (
        "src/core/i18n/__init__.py",
        "src.db.session",
        "Todo 8",
        "i18n.get_system_default_locale lazily opens a SQLAlchemy Session; "
        "DB lookup moves to an application/system-config provider under Todo 8.",
    ),
)


# ---------------------------------------------------------------------------
# AST scanning helpers
# ---------------------------------------------------------------------------


def _iter_python_files(root: Path) -> Iterator[Path]:
    """Yield every regular ``.py`` file under ``root`` (excluding caches)."""

    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.name.startswith("."):
            continue
        yield path


def _path_to_module(path: Path, *, root: Path) -> str:
    """Best-effort dotted module path for a file under ``root``.

    Used only for diagnostic messages and to match the rule's
    ``source_module_prefix``. AST node modules already give us the absolute
    target dotted path; this helper just round-trips the source file path.
    """

    rel = path.relative_to(root)
    parts = list(rel.parts)
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _iter_import_modules(path: Path) -> Iterator[tuple[str, int]]:
    """Walk every import statement in ``path`` and yield (module, lineno).

    Uses ``ast.walk`` so that **lazy** imports inside functions and
    conditional branches are not silently missed. Comments and string
    literals are ignored because AST nodes represent real import statements
    only.
    """

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"failed to read {path}: {exc}") from exc
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise RuntimeError(f"failed to parse {path} at line {exc.lineno}: {exc.msg}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                # ``from . import x`` — relative; only the level matters and
                # the target cannot reach ``src.infrastructure`` etc., so skip.
                continue
            yield node.module, node.lineno


def _detect_violations() -> list[tuple[str, str, int, str]]:
    """Return all (source_path, target_module, lineno, owner_todo) tuples.

    The fourth element is the rule's ``owner_todo`` so downstream tests can
    report which Wave 1 task is responsible for clearing the violation.
    """

    out: list[tuple[str, str, int, str]] = []
    for rule in RULES:
        root = PROJECT_ROOT / rule.source_root
        if not root.exists():
            continue
        for path in _iter_python_files(root):
            module = _path_to_module(path, root=PROJECT_ROOT)
            if not module.startswith(rule.source_module_prefix):
                # Defensive: should never trigger because we already rglob
                # from ``rule.source_root``, but keep the guard so the rule
                # stays composable if extended in the future.
                continue
            for imported, lineno in _iter_import_modules(path):
                if imported.startswith(rule.target_module_prefix):
                    rel = str(path.relative_to(PROJECT_ROOT))
                    out.append((rel, imported, lineno, rule.owner_todo))
    out.sort()
    return out


# ---------------------------------------------------------------------------
# Per-rule structural tests
# ---------------------------------------------------------------------------


def _declared_pairs() -> set[tuple[str, str]]:
    return {(path, module) for path, module, _, _ in KNOWN_VIOLATIONS}


def test_known_violations_have_valid_owner_todo() -> None:
    """Every entry must be tagged with one of Todo 5/6/7/8."""

    offenders: list[str] = []
    for path, module, owner, _reason in KNOWN_VIOLATIONS:
        if owner not in VALID_OWNER_TODOS:
            offenders.append(
                f"{path}:{module} -> owner={owner!r} (must be one of {sorted(VALID_OWNER_TODOS)})"
            )
    assert not offenders, (
        "KNOWN_VIOLATIONS contains rows with invalid owner_todo; "
        "fix the test data first:\n  - " + "\n  - ".join(offenders)
    )


def test_known_violations_resolve_to_real_code() -> None:
    """Each declared violation must point to a real (path, module) pair.

    Either:
      * a literal ``from <module> import ...`` line exists in the file at the
        reported import location, OR
      * the rule has a separate verification step (e.g. TYPE_CHECKING) that
        is otherwise exercised at runtime. For this test we only require that
        ``ast.parse`` succeeds on the file and that the module name appears
        somewhere among its import statements — enough to detect typos in the
        table without forcing us to mirror line numbers exactly when code is
        refactored.
    """

    missing: list[str] = []
    for path, module, _owner, _reason in KNOWN_VIOLATIONS:
        full = PROJECT_ROOT / path
        if not full.exists():
            missing.append(f"{path}:{module} -> file not found on disk")
            continue
        try:
            imports = {m for m, _ln in _iter_import_modules(full)}
        except RuntimeError as exc:
            missing.append(f"{path}:{module} -> {exc}")
            continue
        if module not in imports:
            missing.append(
                f"{path}:{module} -> module not imported by file (file imports: {sorted(imports)})"
            )
    assert not missing, (
        "KNOWN_VIOLATIONS references (path, module) pairs that no longer "
        "exist in source. Remove the dead rows or fix the table:\n  - " + "\n  - ".join(missing)
    )


def test_no_undeclared_violations() -> None:
    """Every detected violation must already be in ``KNOWN_VIOLATIONS``.

    This is the load-bearing guard: a new ``from src.infrastructure...`` in
    ``src/api`` (or any other forbidden pair) will fail this test until the
    caller explicitly registers it as a known exception **with** an owner
    Todo. That registration is what makes the table a binding contract for
    Wave 1.
    """

    actual = _detect_violations()
    declared = _declared_pairs()
    new: list[tuple[str, str, int, str]] = []
    for path, module, lineno, owner in actual:
        if (path, module) not in declared:
            new.append((path, module, lineno, owner))
    assert not new, (
        "Found new architecture-boundary violations not registered in "
        "KNOWN_VIOLATIONS. Either remove the offending import or explicitly "
        "register it in KNOWN_VIOLATIONS with an owner Todo (5/6/7/8):\n  - "
        + "\n  - ".join(
            f"{path}:{lineno}: {module} (would belong to {owner})"
            for path, module, lineno, owner in new
        )
    )


def test_known_violations_match_detected_set() -> None:
    """Reverse direction: every declared violation must still be detected.

    Catches the case where someone deletes an offending import line but
    forgets to update ``KNOWN_VIOLATIONS`` (which would mask future
    regressions of the same kind).
    """

    actual_pairs = {(p, m) for p, m, _ln, _o in _detect_violations()}
    declared = _declared_pairs()
    stale = declared - actual_pairs
    assert not stale, (
        "KNOWN_VIOLATIONS contains rows that no longer correspond to a real "
        "import. Remove them so the table stays a true baseline contract:\n  - "
        + "\n  - ".join(f"{p}:{m}" for p, m in sorted(stale))
    )


def test_rules_cover_all_target_layers() -> None:
    """Sanity: every target layer described in the plan has a matching rule."""

    expected_layers = {
        ("src/api", "src.infrastructure", "Todo 6"),
        ("src/mcp", "src.infrastructure", "Todo 6"),
        ("src/tasks", "src.infrastructure", "Todo 7"),
        ("src/services", "src.application", "Todo 5"),
        ("src/services", "src.infrastructure", "Todo 5"),
        ("src/core", "src.db.session", "Todo 8"),
    }
    actual_layers = {(r.source_root, r.target_module_prefix, r.owner_todo) for r in RULES}
    missing = expected_layers - actual_layers
    assert not missing, "Boundary rules missing from RULES:\n  - " + "\n  - ".join(
        f"{a}->{b} ({c})" for a, b, c in sorted(missing)
    )


# ---------------------------------------------------------------------------
# Runtime / fixture-based violation test
# ---------------------------------------------------------------------------


def _inject_temp_violation(path: Path) -> Path:
    """Create a tiny Python file that violates one of the rules.

    Returns the temp path. The caller MUST delete the file in a ``finally``
    block.
    """

    payload = (
        '"""Temporary fixture injected by the architecture-boundary test.\n'
        "Do not commit this file: deleting it is part of the test.\n"
        '"""\n'
        "\n"
        "from src.infrastructure.tasks.celery_dispatcher import (\n"
        "    CeleryTaskDispatcher,\n"
        ")\n"
    )
    path.write_text(payload, encoding="utf-8")
    return path


def test_violation_added_at_runtime_fails() -> None:
    """Injecting ``from src.infrastructure...`` into ``src/api`` must be detected.

    The fixture file is created inside ``src/api/`` (the same package the
    rule scans), the detector is rerun, and the test asserts that the new
    ``(path, module)`` pair appears in the undeclared set. Then the fixture
    is deleted and the detector is rerun to confirm the violation is gone.
    """

    fixture = PROJECT_ROOT / "src" / "api" / "_boundary_test_fixture.py"
    assert not fixture.exists(), (
        "Pre-existing fixture left behind by a previous run; aborting to avoid clobbering."
    )

    try:
        _inject_temp_violation(fixture)

        actual = _detect_violations()
        declared = _declared_pairs()
        undeclared = [(p, m, ln, o) for p, m, ln, o in actual if (p, m) not in declared]

        fixture_rel = str(fixture.relative_to(PROJECT_ROOT))
        matched = [entry for entry in undeclared if entry[0] == fixture_rel]
        assert matched, (
            "Runtime violation fixture was injected but the detector did "
            "not flag it. Detector output:\n  - "
            + "\n  - ".join(f"{p}:{ln}: {m}" for p, m, ln, _ in undeclared)
        )
        # Sanity: the matched entry must reference src.infrastructure
        for _path, module, _ln, _o in matched:
            assert module.startswith("src.infrastructure"), (
                f"Unexpected module in matched runtime violation: {module}"
            )
    finally:
        if fixture.exists():
            fixture.unlink()

    # Post-condition: with the fixture gone, the detector is clean again.
    actual_after = _detect_violations()
    declared_after = _declared_pairs()
    undeclared_after = [
        (p, m, ln, o) for p, m, ln, o in actual_after if (p, m) not in declared_after
    ]
    assert not undeclared_after, (
        "Detector still reports undeclared violations after the runtime "
        "fixture was removed:\n  - "
        + "\n  - ".join(f"{p}:{ln}: {m}" for p, m, ln, _ in undeclared_after)
    )


# ---------------------------------------------------------------------------
# Git-diff guard
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path = PROJECT_ROOT) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def _modified_python_paths(ref: str) -> list[Path]:
    out = _git("diff", "--name-only", "--diff-filter=AM", ref)
    return [
        PROJECT_ROOT / line.strip() for line in out.splitlines() if line.strip().endswith(".py")
    ]


def _module_imports_in_file(path: Path) -> set[str]:
    return {m for m, _ln in _iter_import_modules(path)}


def _module_imports_at_ref(path: Path, ref: str) -> set[str] | None:
    """Return the set of imported module names in ``path`` at git ``ref``.

    Returns ``None`` if the file did not exist at that ref.
    """

    rel = str(path.relative_to(PROJECT_ROOT))
    blob = subprocess.run(
        ("git", "show", f"{ref}:{rel}"),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if blob.returncode != 0:
        return None
    try:
        tree = ast.parse(blob.stdout, filename=rel)
    except SyntaxError:
        return set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    return imports


def test_no_new_violations_introduced() -> None:
    """No uncommitted/working-tree change may introduce a new violation.

    For every Python file modified relative to ``HEAD`` that lives under one
    of the scanned source roots, compute the **set difference** of imported
    modules between ``HEAD`` and the working tree. Any module that is a
    new import under a forbidden ``target_module_prefix`` (per the rule
    that owns this file) is a regression.

    The diff is restricted to imports inside tracked source files; the
    fixture file used by ``test_violation_added_at_runtime_fails`` is left
    untouched by this test (and is also cleaned up at the end of its own
    run, so it never appears in ``git status``).
    """

    declared = _declared_pairs()
    scanned_roots = {Path(PROJECT_ROOT / r.source_root) for r in RULES}

    new_offenders: list[tuple[str, str, str]] = []
    for path in _modified_python_paths("HEAD"):
        if not any(_is_under(path, root) for root in scanned_roots):
            continue
        before = _module_imports_at_ref(path, "HEAD")
        if before is None:
            # file added in the working tree; every import is "new".
            before = set()
        after = _module_imports_in_file(path)
        new_imports = after - before
        if not new_imports:
            continue
        rule = _rule_for_source_path(path)
        if rule is None:
            continue
        for module in sorted(new_imports):
            if not module.startswith(rule.target_module_prefix):
                continue
            if (str(path.relative_to(PROJECT_ROOT)), module) in declared:
                # New violation in a tracked file, but already declared as
                # an exception. That is acceptable: it just means the
                # table was updated first. Anything else is a real
                # regression.
                continue
            new_offenders.append((str(path.relative_to(PROJECT_ROOT)), module, rule.name))

    assert not new_offenders, (
        "Working-tree changes introduced new architecture-boundary violations "
        "that are not registered in KNOWN_VIOLATIONS:\n  - "
        + "\n  - ".join(f"{p}: {m} (rule={r})" for p, m, r in new_offenders)
    )


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _rule_for_source_path(path: Path) -> BoundaryRule | None:
    rel = path.relative_to(PROJECT_ROOT)
    for rule in RULES:
        rule_root = PROJECT_ROOT / rule.source_root
        try:
            rel.relative_to(rule_root)
        except ValueError:
            continue
        return rule
    return None


# ---------------------------------------------------------------------------
# Diagnostic: print current violation summary when run with -s.
#
# Useful during review and as a baseline report; this is not a test of
# behavior but makes the table machine-readable from ``pytest -s`` output.
# ---------------------------------------------------------------------------


def test_report_current_violations() -> None:
    """Emit a human-readable listing of current violations (informational).

    Always passes; exists so ``pytest -s`` produces a clean baseline report
    that reviewers can diff against ``KNOWN_VIOLATIONS`` directly. See
    ``.omo/evidence/architecture-consolidation/02-architecture-boundaries/manifest.md``
    for the captured output.
    """

    actual = _detect_violations()
    print("\nArchitecture dependency boundary — current state")
    print(f"  rules: {len(RULES)}")
    print(f"  declared exceptions: {len(KNOWN_VIOLATIONS)}")
    print(f"  detected violations: {len(actual)}")
    grouped: dict[str, list[tuple[str, str, int]]] = {}
    for path, module, lineno, owner in actual:
        grouped.setdefault(owner, []).append((path, module, lineno))
    for owner in sorted(grouped):
        print(f"\n  [{owner}]")
        for path, module, lineno in sorted(grouped[owner]):
            in_table = (path, module) in _declared_pairs()
            tag = " (declared)" if in_table else " *** UNDECLARED ***"
            print(f"    {path}:{lineno}: {module}{tag}")


def __iter_python_files_for_tests() -> Iterable[Path]:
    """Public re-export kept for clarity in failure messages."""

    return _iter_python_files(PROJECT_ROOT)
