"""Process-local holder for the composition-root ``Container``.

The API, the MCP server and the Celery task layer all need the same
wired ports (dispatcher, LLM factory, orchestrator). Keeping one holder
here means those three entry points cannot drift apart, and tests have a
single place to substitute fakes.

Production code calls :func:`get_container`; tests call
:func:`set_container_for_tests` and restore with
:func:`reset_container_for_tests`.
"""

from __future__ import annotations

from src.application.bootstrap import Container, get_production_container

_container: Container = get_production_container()


def get_container() -> Container:
    return _container


def set_container_for_tests(container: Container) -> None:
    global _container
    _container = container


def reset_container_for_tests() -> None:
    global _container
    _container = get_production_container()


__all__ = ["get_container", "reset_container_for_tests", "set_container_for_tests"]
