"""Transaction-level regression probes for human-only triage gates."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import threading

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()


def _snapshot(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("tasks", "task_runs", "task_links", "task_events", "task_comments")
    }


@pytest.mark.parametrize("primitive", ["specify", "decompose"])
@pytest.mark.parametrize("kind,recurrences", [("needs_input", 1), ("capability", 1), ("transient", 2), (None, 2)])
def test_waiting_writer_reads_committed_human_gate(board, primitive, kind, recurrences):
    with kbc.connect_closing() as writer:
        tid = kb.create_task(writer, title="Original scope", body="Personal gate", assignee="owner", triage=True)
        db_path = writer.execute("PRAGMA database_list").fetchone()[2]
        ready, go, attempted = threading.Event(), threading.Event(), threading.Event()
        result, errors = [], []

        def late_result():
            try:
                with sqlite3.connect(db_path, isolation_level=None, timeout=15) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.set_trace_callback(lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None)
                    ready.set()
                    assert go.wait(15)
                    if primitive == "specify":
                        result.append(kb.specify_triage_task(conn, tid, title="Rewrite", body="Execute", assignee="other"))
                    else:
                        result.append(decompose_triage_task(conn, tid, root_assignee="other", children=[{"title": "child"}]))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=late_result, daemon=True)
        thread.start()
        try:
            assert ready.wait(15)
            with kb.write_txn(writer):
                writer.execute("UPDATE tasks SET block_kind=?, block_recurrences=? WHERE id=?", (kind, recurrences, tid))
                before = _snapshot(writer)
                go.set()
                assert attempted.wait(15), "late output must actually contend for the write transaction"
        finally:
            go.set()
            thread.join(20)
        assert not thread.is_alive()
        assert errors == []
        assert result == ([False] if primitive == "specify" else [None])
        assert _snapshot(writer) == before


def test_recovery_audit_failure_rolls_back_every_change(board, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Gate", triage=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET block_kind='needs_input', block_recurrences=2 WHERE id=?", (tid,))
        before = _snapshot(conn)

        def failed_audit(*args, **kwargs):
            raise RuntimeError("synthetic event failure")

        monkeypatch.setattr(kb, "_append_event", failed_audit)
        with pytest.raises(RuntimeError, match="synthetic event failure"):
            kb.block_task(conn, tid, kind="needs_input", reason="metadata only")
        assert _snapshot(conn) == before
