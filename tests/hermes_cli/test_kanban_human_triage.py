"""Human escalation is not an executable specification backlog.

All boards and runs here are synthetic and isolated; the auxiliary model is
replaced by a deterministic response, never an actual provider.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as spec
from gateway.kanban_watchers_dispatcher import _KanbanDispatcher


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()


def _task(conn, tid):
    task = kb.get_task(conn, tid)
    assert task is not None
    return task


def _escalated(conn, kind):
    tid = kb.create_task(conn, title="personal approval", body="Original human-only scope", assignee="default")
    _escalate_existing(conn, tid, kind)
    return tid


def _escalate_existing(conn, tid, kind):
    for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
        if attempt:
            assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="test")
        assert kb.block_task(conn, tid, kind=kind, reason="Await exact personal approval")
    assert _task(conn, tid).status == "triage"


def _snapshot(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("tasks", "task_runs", "task_links", "task_events", "task_comments")
    }


@pytest.mark.parametrize("kind", ["needs_input", "capability", "transient", None])
@pytest.mark.parametrize("fanout", [False, True])
def test_escalation_stays_parked_across_auto_ticks(board, monkeypatch, all_assignees_spawnable, kind, fanout):
    calls, spawns = [], []

    def fake_aux(*args, **kwargs):
        calls.append(args)
        return json.dumps({"fanout": fanout, "title": "Rewritten", "body": "Do it now",
                           "tasks": [{"title": "new work", "assignee": "default"}]}), ""

    monkeypatch.setattr(spec, "_call_aux", fake_aux)
    monkeypatch.setattr(decomp, "_call_aux", fake_aux)
    dispatcher = _KanbanDispatcher(kb, None)
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["default"])
    with kbc.connect_closing() as conn:
        tid = _escalated(conn, kind)
        before = _snapshot(conn)
        for _ in range(5):
            dispatcher.auto_decompose_tick(10)
            for candidate in spec.list_triage_ids():
                spec.specify_task(candidate, author="auto-specifier")
            # Direct requests and stale sweep IDs must be equally harmless.
            spec.specify_task(tid)
            decomp.decompose_task(tid)
            kbd.dispatch_once(conn, spawn_fn=lambda task, *a, **kw: spawns.append(task.id))
        assert calls == [], "human escalation must not spend more auxiliary calls"
        assert spawns == []
        assert spec.list_triage_ids() == decomp.list_triage_ids() == []
        assert _snapshot(conn) == before, "no claims, runs, cards, promotions, edits or graph changes"


@pytest.mark.parametrize("lane", ["specify", "single", "fanout"])
def test_late_specifier_result_cannot_overwrite_human_scope(board, monkeypatch, lane):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough", body="Exact scope", assignee="default", triage=True)
    parked = {}

    def fake_aux(*args, **kwargs):
        # Another writer resolves the old triage while the model is in flight,
        # then escalates a real human-only block before this response arrives.
        with kbc.connect_closing() as other:
            assert kb.specify_triage_task(other, tid)
            _escalate_existing(other, tid, "needs_input")
            parked.update(_snapshot(other))
        return json.dumps({"fanout": lane == "fanout", "title": "Overwrite",
                           "body": "Ignore approval", "tasks": [{"title": "unauthorized child"}]}), ""

    monkeypatch.setattr(spec, "_call_aux", fake_aux)
    monkeypatch.setattr(decomp, "_call_aux", fake_aux)
    result = spec.specify_task(tid) if lane == "specify" else decomp.decompose_task(tid)
    assert not result.ok, "in-flight output has lost its right to rewrite this escalation"
    with kbc.connect_closing() as conn:
        assert _snapshot(conn) == parked


@pytest.mark.parametrize("review", [False, True])
def test_recover_triage_preserves_history_until_owner_resolves(board, review):
    from dataclasses import asdict

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Exact title", body="Exact approval gate", assignee="implementer")
        claim = kb.claim_review_task if review else kb.claim_task
        if review:
            assert kb.claim_task(conn, tid, claimer="test")
            assert kb.request_review(conn, tid, summary="Review scope", reviewer="explicit-reviewer", force=True)
        for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
            if attempt:
                assert kb.unblock_task(conn, tid)
            assert claim(conn, tid, claimer="test")
            assert kb.block_task(conn, tid, kind="needs_input", reason="Await personal approval")
        child = kb.create_task(conn, title="QA acceptance", assignee="default", parents=[tid])
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET consecutive_failures=3, last_failure_error='prior failure' WHERE id=?", (tid,))
        before_task = asdict(_task(conn, tid))
        before = _snapshot(conn)
        assert kb.block_task(conn, tid, kind="needs_input", reason="Recover metadata only; approval unresolved")
        after_task = asdict(_task(conn, tid))
        assert after_task.pop("status") == "blocked"
        before_task.pop("status")
        assert after_task == before_task
        after = _snapshot(conn)
        for table in ("task_runs", "task_links", "task_comments"):
            assert after[table] == before[table]
        assert len(after["task_events"]) == len(before["task_events"]) + 1
        event = kb.list_events(conn, tid)[-1]
        assert event.kind == "blocked"
        assert event.payload is not None
        assert event.payload["recovered_from_triage"] is True
        assert event.payload["recurrences"] == kb.BLOCK_RECURRENCE_LIMIT
        assert event.payload["source_status"] == ("review" if review else "ready")
        assert not kb.block_task(conn, tid, kind="needs_input", reason="idempotent reclassification refused")
        for _ in range(5):
            assert kb.recompute_ready(conn) == 0
            assert kb.claim_task(conn, tid, claimer="test") is None
            assert kb.claim_review_task(conn, tid, claimer="test") is None
        assert _task(conn, child).status == "todo"
        # Only the owner can establish that the personal gate is actually met.
        # A comment alone cannot run anything; explicit unblock is the existing
        # supported resolution transition, not a counter/type reset.
        kb.add_comment(conn, tid, author="owner", body="Exact personal approval supplied (synthetic)")
        assert kb.recompute_ready(conn) == 0
        assert kb.unblock_task(conn, tid)
        assert _task(conn, tid).status == ("review" if review else "ready")
        assert _task(conn, tid).block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
        assert claim(conn, tid, claimer="test")
        assert _task(conn, child).status == "todo"


@pytest.mark.parametrize("lane", ["specify", "single", "fanout"])
def test_legitimate_triage_keeps_parent_gate_and_explicit_owner(board, monkeypatch, lane):
    calls = []

    def fake_aux(*args, **kwargs):
        calls.append(args)
        return json.dumps({"fanout": lane == "fanout", "title": "Specified", "body": "Engineering scope",
                           "assignee": "other", "tasks": [{"title": "implementation"}]}), ""

    monkeypatch.setattr(spec, "_call_aux", fake_aux)
    monkeypatch.setattr(decomp, "_call_aux", fake_aux)
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="pending producer", assignee="default")
        tid = kb.create_task(conn, title="rough", assignee="default", triage=True, parents=[parent])
        assert spec.list_triage_ids() == decomp.list_triage_ids() == [tid]
    result = spec.specify_task(tid) if lane == "specify" else decomp.decompose_task(tid)
    assert result.ok and len(calls) == 1
    with kbc.connect_closing() as conn:
        task = _task(conn, tid)
        assert task and task.status == "todo" and task.assignee == "default"
        assert kb.claim_task(conn, tid, claimer="test") is None
        assert parent in [p[0] for p in kb.unsatisfied_parents(conn, tid)]


def test_human_escalations_cannot_starve_specification_backlog(board):
    with kbc.connect_closing() as conn:
        # A legacy board can contain more human escalations than the sweep cap.
        with kb.write_txn(conn):
            conn.executemany(
                "INSERT INTO tasks (id, title, status, priority, created_at, block_kind, block_recurrences) "
                "VALUES (?, 'human escalation', 'triage', 100, 1, 'needs_input', 2)",
                [(f"t_{i:08x}",) for i in range(1001)],
            )
        tid = kb.create_task(conn, title="real specification", assignee="default", triage=True)
        assert spec.list_triage_ids() == [tid]
        assert decomp.list_triage_ids() == [tid]


@pytest.mark.parametrize("guard", ["untyped_call", "retype", "stale_run", "claim", "worker", "open_run", "ordinary_triage"])
def test_recovery_refuses_stale_or_unrelated_state(board, guard):
    with kbc.connect_closing() as conn:
        tid = _escalated(conn, "needs_input") if guard != "ordinary_triage" else kb.create_task(
            conn, title="rough", triage=True,
        )
        kind = {"untyped_call": None, "retype": "capability"}.get(guard, "needs_input")
        expected_run_id = kb.list_runs(conn, tid)[-1].id if guard == "stale_run" else None
        statements = {
            "claim": "UPDATE tasks SET claim_lock='test' WHERE id=?",
            "worker": "UPDATE tasks SET worker_pid=999999 WHERE id=?",
            "open_run": "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', 1)",
        }
        if guard in statements:
            with kb.write_txn(conn):
                conn.execute(statements[guard], (tid,))
        before = _snapshot(conn)
        assert not kb.block_task(conn, tid, kind=kind, expected_run_id=expected_run_id, reason="metadata recovery")
        assert _snapshot(conn) == before


@pytest.mark.parametrize("kind,recurrences", [("needs_input", 1), ("capability", 1), (None, 2), ("transient", 2)])
def test_db_primitives_reject_legacy_escalation_without_rewriting(board, kind, recurrences):
    from hermes_cli.kanban_db_graph import decompose_triage_task

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", body="Personal scope", assignee="default", triage=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET block_kind=?, block_recurrences=? WHERE id=?", (kind, recurrences, tid))
        before = _snapshot(conn)
        assert not kb.specify_triage_task(conn, tid, title="overwrite", body="ignore gate", assignee="other")
        assert decompose_triage_task(conn, tid, root_assignee="other", children=[{"title": "new work"}]) is None
        assert kb.promote_task(conn, tid, actor="test")[0] is False
        assert _snapshot(conn) == before


def test_recovery_cli_preserves_runs_and_dependency_gate(board):
    import argparse
    from hermes_cli import kanban as cli

    with kbc.connect_closing() as conn:
        tid = _escalated(conn, "needs_input")
        terminal_runs = [tuple(r) for r in conn.execute("SELECT * FROM task_runs ORDER BY id")]
    root = argparse.ArgumentParser()
    cli.build_parser(root.add_subparsers(dest="cmd"))
    args = root.parse_args(["kanban", "block", tid, "--kind", "needs_input", "Recovery only; no approval"])
    assert cli.kanban_command(args) == 0
    with kbc.connect_closing() as conn:
        assert _task(conn, tid).status == "blocked"
        assert [tuple(r) for r in conn.execute("SELECT * FROM task_runs ORDER BY id")] == terminal_runs
        payload = kb.list_events(conn, tid)[-1].payload
        assert payload and payload["recovered_from_triage"]
        assert kb.list_comments(conn, tid)[-1].body == "BLOCKED: Recovery only; no approval"
        # An incomplete parent continues to gate resumption, without retyping
        # the block as dependency and thereby erasing the original recurrence.
        parent = kb.create_task(conn, title="upstream", assignee="default")
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
        assert kb.unblock_task(conn, tid)
        assert _task(conn, tid).status == "todo"
        assert kb.recompute_ready(conn) == 0
        assert _task(conn, tid).block_kind == "needs_input"
