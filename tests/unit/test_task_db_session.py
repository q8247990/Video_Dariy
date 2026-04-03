"""Tests for task_db_session context manager."""

from unittest.mock import MagicMock, patch

from src.db.session import task_db_session


@patch("src.db.session.SessionLocal")
def test_task_db_session_yields_session(mock_session_local: MagicMock) -> None:
    mock_db = MagicMock()
    mock_session_local.return_value = mock_db

    with task_db_session() as db:
        assert db is mock_db

    mock_session_local.assert_called_once()


@patch("src.db.session.SessionLocal")
def test_task_db_session_closes_on_normal_exit(mock_session_local: MagicMock) -> None:
    mock_db = MagicMock()
    mock_session_local.return_value = mock_db

    with task_db_session() as db:
        db.execute("SELECT 1")

    mock_db.close.assert_called_once()


@patch("src.db.session.SessionLocal")
def test_task_db_session_closes_on_exception(mock_session_local: MagicMock) -> None:
    mock_db = MagicMock()
    mock_session_local.return_value = mock_db

    try:
        with task_db_session():
            raise ValueError("simulated task failure")
    except ValueError:
        pass

    mock_db.close.assert_called_once()


@patch("src.db.session.SessionLocal")
def test_task_db_session_does_not_auto_commit(mock_session_local: MagicMock) -> None:
    mock_db = MagicMock()
    mock_session_local.return_value = mock_db

    with task_db_session() as db:
        db.add("something")

    mock_db.commit.assert_not_called()


@patch("src.db.session.SessionLocal")
def test_task_db_session_does_not_auto_rollback(mock_session_local: MagicMock) -> None:
    mock_db = MagicMock()
    mock_session_local.return_value = mock_db

    try:
        with task_db_session():
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    mock_db.rollback.assert_not_called()


@patch("src.db.session.SessionLocal")
def test_task_db_session_propagates_exception(mock_session_local: MagicMock) -> None:
    mock_db = MagicMock()
    mock_session_local.return_value = mock_db

    caught = False
    try:
        with task_db_session():
            raise RuntimeError("should propagate")
    except RuntimeError as exc:
        caught = True
        assert str(exc) == "should propagate"

    assert caught, "Exception should propagate out of context manager"
