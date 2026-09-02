"""PostgreSQL backup / verify / restore utility (Todo 24).

Produces a checksummed ``pg_dump`` custom-format dump, verifies the
checksum, and restores the dump into a PostgreSQL target (a THROWAWAY
database or schema in ops practice) so operators can round-trip a
snapshot before an irreversible migration.

Three subcommands (mirroring the ops workflow in the README / ADR
backup runbook):

* ``backup   --database-url <url> [--schema <name>] --output <path>``
  Dump the database (or one schema) to ``<path>`` and write
  ``<path>.sha256`` with the SHA-256 hex digest.
* ``verify   --output <path>``
  Recompute the digest of ``<path>`` and compare against
  ``<path>.sha256``; exits non-zero on mismatch.
* ``restore  --database-url <url> [--schema <name>] --dump <path>``
  Restore ``<path>`` into the target. When ``--schema`` is given the
  dump is filtered to that schema's objects.

Uses the ``pg_dump`` / ``pg_restore`` binaries from ``PATH``; they must
match the target server major version (pg_dump refuses a mismatched
server). Example:

    python3 scripts/backup_restore_db.py backup \
        --database-url "$DATABASE_URL" --output /tmp/hm.dump
    python3 scripts/backup_restore_db.py verify --output /tmp/hm.dump
    python3 scripts/backup_restore_db.py restore \
        --database-url "$DATABASE_URL" --dump /tmp/hm.dump
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from sqlalchemy.engine.url import make_url


def _pg_connect_args(database_url: str) -> tuple[list[str], str, str]:
    """Translate a SQLAlchemy URL into ``pg_dump``/``pg_restore`` args.

    Returns ``(args, dbname, password)`` where ``dbname`` is the bare
    database name to pass as the target and ``password`` is the password
    to inject into the subprocess ``env`` (never echoed in ``args``).
    """
    url = make_url(database_url)
    args: list[str] = []
    if url.host:
        args += ["--host", str(url.host)]
    if url.port:
        args += ["--port", str(url.port)]
    if url.username:
        args += ["--username", str(url.username)]
    if not url.database:
        raise ValueError("DATABASE_URL must name a database")
    return args, str(url.database), str(url.password or "")


def _pg_env(password: str) -> dict[str, str]:
    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password
    return env


def _checksum_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".sha256")


def compute_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of ``path`` in streaming fashion."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_backup(
    database_url: str,
    output: Path,
    schema: Optional[str] = None,
) -> Path:
    """Dump ``database_url`` (optionally a single ``schema``) to ``output``.

    Writes the checksum sidecar ``<output>.sha256`` alongside the dump.
    Returns ``output``.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
    if schema:
        cmd += ["--schema", schema]
    connect_args, dbname, password = _pg_connect_args(database_url)
    cmd += connect_args
    cmd.append(dbname)
    with output.open("wb") as stream:
        subprocess.run(cmd, stdout=stream, check=True, env=_pg_env(password))
    _checksum_path(output).write_text(
        f"{compute_sha256(output)}  {output.name}\n", encoding="utf-8"
    )
    return output


def verify_checksum(output: Path) -> bool:
    """Return ``True`` when the dump checksum matches its sidecar."""
    sidecar = _checksum_path(output)
    if not sidecar.exists():
        raise FileNotFoundError(f"checksum file not found: {sidecar}")
    expected_line = sidecar.read_text(encoding="utf-8").strip()
    if not expected_line:
        raise ValueError(f"empty checksum file: {sidecar}")
    expected = expected_line.split()[0]
    actual = compute_sha256(output)
    return actual == expected


def run_restore(
    database_url: str,
    dump: Path,
    schema: Optional[str] = None,
) -> None:
    """Restore ``dump`` into ``database_url`` (optionally filtered by schema)."""
    if not dump.exists():
        raise FileNotFoundError(f"dump not found: {dump}")
    cmd = ["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "--verbose"]
    if schema:
        cmd += ["--schema", schema]
    connect_args, dbname, password = _pg_connect_args(database_url)
    cmd += connect_args
    cmd += ["--dbname", dbname]
    cmd.append(str(dump))
    # A partial restore must abort so the operator cannot mistake a
    # partial restore for success; ``--exit-on-error`` enforces that.
    subprocess.run(cmd, check=True, env=_pg_env(password))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.backup_restore_db",
        description="Checksummed PostgreSQL backup / verify / restore.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="Dump the database/schema + checksum.")
    backup.add_argument("--database-url", required=True)
    backup.add_argument("--schema", default=None)
    backup.add_argument("--output", required=True)
    backup.add_argument("--verify", action="store_true", help="Verify immediately after backup.")

    verify = sub.add_parser("verify", help="Verify a dump checksum.")
    verify.add_argument("--output", required=True)

    restore = sub.add_parser("restore", help="Restore a dump into a database.")
    restore.add_argument("--database-url", required=True)
    restore.add_argument("--schema", default=None)
    restore.add_argument("--dump", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "backup":
        output = run_backup(args.database_url, Path(args.output), schema=args.schema)
        print(f"backup written: {output} ({_checksum_path(output).name})")
        if args.verify:
            ok = verify_checksum(output)
            print("verify: " + ("OK" if ok else "MISMATCH"))
            return 0 if ok else 1
        return 0
    if args.command == "verify":
        ok = verify_checksum(Path(args.output))
        print("verify: " + ("OK" if ok else "MISMATCH"))
        return 0 if ok else 1
    if args.command == "restore":
        run_restore(args.database_url, Path(args.dump), schema=args.schema)
        print("restore: OK")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
