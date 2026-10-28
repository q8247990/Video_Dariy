"""Tests for the parameterized focus-key SQL predicate."""

from sqlalchemy.dialects import postgresql

from src.services.measures.queries import focus_keys_condition


def test_focus_keys_condition_binds_keys_instead_of_interpolating() -> None:
    clause = focus_keys_condition(["kid's room", "front door"])
    compiled = clause.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "focus_keys" in sql
    assert "CAST(" in sql.upper()
    assert "array[" not in sql.lower()
    # The user-controlled keys must never appear in the SQL text itself.
    assert "kid" not in sql
    assert compiled.params["focus_keys"] == ["front door", "kid's room"]


def test_focus_keys_condition_deduplicates_and_sorts() -> None:
    clause = focus_keys_condition(["b", "a", "b"])
    compiled = clause.compile(dialect=postgresql.dialect())

    assert compiled.params["focus_keys"] == ["a", "b"]
