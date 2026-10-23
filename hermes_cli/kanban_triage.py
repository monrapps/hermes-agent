"""Distinguish executable specification triage from human escalation.

Use durable block metadata, not body prose or an automatic schema migration:
legacy escalations are already identifiable without rewriting their history.
"""
from __future__ import annotations

import sqlite3


def requires_human_triage(block_kind: str | None, block_recurrences: int) -> bool:
    """A personal/capability gate or any exhausted block loop is not scope work."""
    from hermes_cli.kanban_db import BLOCK_RECURRENCE_LIMIT

    return block_kind in {"needs_input", "capability"} or block_recurrences >= BLOCK_RECURRENCE_LIMIT


def recover_human_triage(
    conn: sqlite3.Connection, task_id: str, row: sqlite3.Row, *,
    kind: str | None, reason: str | None, expected_run_id: int | None,
) -> bool:
    """Inside block_task's write transaction, park escalation without replaying
    a run, resetting counters, changing scope/owner, or releasing dependencies.

    A typed cause cannot be swapped to reset loop accounting. A stale worker
    cannot claim this operator-only metadata repair using its old run id.
    """
    from hermes_cli.kanban_db import _append_event, _resume_status_from_events

    previous_kind = row["block_kind"]
    recurrences = int(row["block_recurrences"] or 0)
    if kind is None or expected_run_id is not None:
        return False
    if previous_kind is not None and kind != previous_kind:
        return False
    if not requires_human_triage(previous_kind, recurrences):
        return False
    source_status = _resume_status_from_events(conn, task_id)
    changed = conn.execute(
        "UPDATE tasks SET status = 'blocked', block_kind = ? "
        "WHERE id = ? AND status = 'triage' AND current_run_id IS NULL "
        "AND claim_lock IS NULL AND worker_pid IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM task_runs WHERE task_id = ? AND ended_at IS NULL)",
        (kind, task_id, task_id),
    ).rowcount
    if changed != 1:
        return False
    _append_event(conn, task_id, "blocked", {
        "kind": kind, "reason": reason, "recurrences": recurrences,
        "source_status": source_status, "recovered_from_triage": True,
    })
    return True
