"""Explicit CI policy at the real SQLite/HTTP completion boundary."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.config import set_config_value
from hermes_cli.kanban_db_connect import connect

REPO = "acme/private"
PR = f"https://github.com/{REPO}/pull/6"
SHA = "a" * 40
CONTEXTS = ["Go tests", "Container build", "Chromium"]
POLICY = {"mode": "explicit", "base_branch": "main", "required_checks": [
    {"context": name, "app_id": 15368} for name in CONTEXTS]}


@pytest.fixture
def explicit_github(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    state = {"requests": [], "conclusion": "success", "head": SHA, "branch": "main"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            if state.get("http_failure") and "/statuses" in self.path:
                self.send_error(503)
                return
            if self.path == "/graphql" or "/rules/" in self.path or "/protection" in self.path:
                self.send_error(403, "Protection APIs forbidden by explicit policy")
                return
            if "/pulls/" in self.path:
                value = {"head": {"sha": state["head"]}, "base": {"ref": state["branch"]},
                         "state": state.get("pr_state", "closed" if state.get("closed") else "open"),
                         "merged": state.get("merged", False)}
            elif "/check-runs" in self.path:
                runs = [{"id": i + 42, "name": name, "head_sha": SHA, "app": {"id": 15368},
                         "status": "completed", "conclusion": state["conclusion"],
                         "html_url": f"https://github.com/{REPO}/actions/runs/{i + 42}"}
                        for i, name in enumerate(CONTEXTS)]
                if state.get("run_without_conclusion"):
                    for run in runs:
                        run.pop("conclusion")
                        run["state"] = "success"
                mutations = {"float_app": lambda: runs[-1]["app"].update(id=15368.0),
                             "null_id": lambda: runs[-1].update(id=None),
                             "string_id": lambda: runs[-1].update(id="44"),
                             "no_url": lambda: runs[-1].pop("html_url")}
                for fault, mutate in mutations.items():
                    if state.get(fault):
                        mutate()
                if state.get("missing"):
                    runs.pop()
                if state.get("app"):
                    runs[-1]["app"]["id"] = 999
                if state.get("stale"):
                    runs[-1]["head_sha"] = "b" * 40
                if state.get("pending"):
                    runs[-1]["status"] = "in_progress"
                value = [{"total_count": len(runs), "check_runs": runs[:2]},
                         {"total_count": len(runs), "check_runs": runs[2:]}]
                if state.get("float_count"):
                    value[-1]["total_count"] = float(len(runs))
                if state.get("count"):
                    value[-1]["total_count"] += 1
                if state.get("duplicate"):
                    value[-1]["check_runs"].append(runs[0])
                if state.get("malformed"):
                    value = {"check_runs": runs}
                if state.get("race"):
                    state["race"]()
            elif "/statuses" in self.path:
                value = [[]]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys, urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    try:
        yield state, home
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("linux")
def test_explicit_policy_completes_from_pinned_exact_head_without_protection(explicit_github):
    state, home = explicit_github
    set_config_value("kanban.pr_acceptance_policies", json.dumps({REPO: POLICY}))
    with connect() as conn:
        tid = kb.create_task(conn, title="Publish", assignee="default", completion_contract=REPO)
        child = kb.create_task(conn, title="Independent QA", parents=[tid])
        assert kb.complete_task(conn, tid, summary="Published implementation", metadata={"published_pr": PR})
        assert kb.get_task(conn, tid).completion_contract == PR
        assert kb.get_task(conn, child).status == "ready"
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["ok"] and receipt["head_sha"] == SHA
    assert receipt["policy_source"] == "config:kanban.pr_acceptance_policies"
    assert receipt["policy_scope"] == {"profile_home": str(home), "repository": REPO, "base_branch": "main"}
    assert {c["name"] for c in receipt["checks"]} == set(CONTEXTS)
    assert all(c["app_id"] == 15368 and c["url"] and c["id"] for c in receipt["checks"])
    assert sum("/pulls/6" in p for p in state["requests"]) == 2
    assert not any(p == "/graphql" or "/rules/" in p or "/protection" in p for p in state["requests"])


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("fault", ["failure", "cancelled", "skipped", "neutral", "timed_out", None,
    "pending", "missing", "app", "stale", "count", "duplicate", "head", "base", "closed", "invalid_head", "initial_base", "malformed", "http_failure", "run_without_conclusion", "final_state", "merged_string", "float_app", "null_id", "string_id", "no_url", "float_count"])
def test_explicit_policy_rejects_incomplete_or_changed_evidence(explicit_github, fault):
    state, _ = explicit_github
    set_config_value("kanban.pr_acceptance_policies", json.dumps({REPO: POLICY}))
    flags = {name: {name: True} for name in (
        "pending", "missing", "app", "stale", "count", "duplicate", "malformed", "http_failure", "run_without_conclusion", "float_app", "null_id", "string_id", "no_url", "float_count")}
    changes = {"head": ("head", "b" * 40), "base": ("branch", "other"), "closed": ("closed", True), "final_state": ("pr_state", None)}
    faults = {**flags, "initial_base": {"branch": "other"}, "invalid_head": {"head": "not-a-sha"},
              "merged_string": {"closed": True, "merged": "false"}}
    state.update(faults.get(fault, {"conclusion": fault}))
    if fault in changes:
        state["conclusion"] = "success"
        def change():
            key, value = changes[fault]
            state[key] = value
        state["race"] = change
    with connect() as conn:
        tid = kb.create_task(conn, title="Reject", assignee="default", completion_contract=PR)
        assert not kb.complete_task(conn, tid, summary="Attempt", metadata={"published_pr": PR,
            "pr_acceptance_policies": {REPO: POLICY}, "receipt": {"ok": True}})
        assert kb.get_task(conn, tid).status != "done"
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
        assert not receipt["ok"]
        assert kb.get_task(conn, tid).completion_contract == PR
        assert not kb.complete_task(conn, tid, summary="Sibling retry", metadata={"published_pr": PR[:-1] + "7"})
    assert not any(p == "/graphql" or "/rules/" in p for p in state["requests"])


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("bad", [None, [], "explicit", {}, {**POLICY, "mode": "fallback"},
    {**POLICY, "required_checks": []}, {**POLICY, "base_branch": "bad..ref"},
    {**POLICY, "required_checks": [{"context": "Go tests", "app_id": True}]},
    {**POLICY, "required_checks": [{"context": "Go tests", "app_id": -1}]},
    {**POLICY, "required_checks": [{"context": "Go tests"}]},
    {**POLICY, "required_checks": [POLICY["required_checks"][0]] * 2},
    {**POLICY, "typo": 1}, "broken_yaml", "root_list", "root_scalar", "root_null"])
def test_explicit_policy_config_errors_fail_closed_before_network(explicit_github, bad):
    state, home = explicit_github
    content = "kanban: [" if bad == "broken_yaml" else json.dumps({"kanban": {"pr_acceptance_policies": {REPO: bad}}})
    content = {"root_list": "[]", "root_scalar": "hello", "root_null": "null"}.get(bad if isinstance(bad, str) else "", content)
    (home / "config.yaml").write_text(content)
    if isinstance(bad, str) and bad.startswith("root_"):
        from hermes_cli.config_effective import load_user_config_effective
        load_user_config_effective()  # A normalized fail-open cache cannot authorize acceptance.
    with connect() as conn:
        tid = kb.create_task(conn, title="Invalid config", assignee="default", completion_contract=PR)
        assert not kb.complete_task(conn, tid, summary="Attempt", metadata={"published_pr": PR})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "config"
    assert state["requests"] == []


@pytest.mark.platforms("linux")
def test_inaccessible_config_cannot_be_treated_as_absent(explicit_github):
    state, home = explicit_github
    protected = home / "private-config"
    protected.mkdir()
    (protected / "config.yaml").write_text(json.dumps({"kanban": {"pr_acceptance_policies": {REPO: POLICY}}}))
    (home / "config.yaml").symlink_to(protected / "config.yaml")
    with connect() as conn:
        tid = kb.create_task(conn, title="Unreadable", assignee="default", completion_contract=PR)
        protected.chmod(0)
        try:
            assert not kb.complete_task(conn, tid, summary="Attempt", metadata={"published_pr": PR})
            receipt = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
            assert receipt["classification"] == "config"
            assert state["requests"] == []
        finally:
            protected.chmod(0o700)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("has_policy", [True, False])
def test_acceptance_policy_uses_assignee_not_callers_config(explicit_github, has_policy):
    state, home = explicit_github
    target = home / "profiles" / "b"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text(json.dumps({"kanban": {"pr_acceptance_policies": {REPO: POLICY} if has_policy else {}}}))
    set_config_value("kanban.pr_acceptance_policies", json.dumps({REPO: {**POLICY, "base_branch": "wrong"}}))
    from hermes_constants import get_hermes_home
    with connect() as conn:
        tid = kb.create_task(conn, title="Assignee scope", assignee="b", completion_contract=PR)
        assert kb.complete_task(conn, tid, summary="Attempt", metadata={"published_pr": PR}) is has_policy
    assert ("/graphql" in state["requests"]) is (not has_policy)
    assert get_hermes_home() == home


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("assignee", ["default", "b"])
def test_frozen_policy_is_owned_by_admitted_profile(explicit_github, assignee):
    import subprocess

    state, home = explicit_github
    target = home / "profiles" / "b"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text("{}")
    # Disk is not an admitted worker's config; do not reset or patch its frozen policy.
    (home / "config.yaml").write_text("kanban: [")
    script = (
        "import json; from agent.safe_worker_policy import _bind_worker_config; "
        f"_bind_worker_config(config={ {'kanban': {'pr_acceptance_policies': {REPO: POLICY}}}!r}); "
        "from hermes_cli.kanban_pr_acceptance import collect_acceptance; "
        f"print(json.dumps(collect_acceptance({PR!r}, {PR!r}, assignee={assignee!r})))"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=True)
    receipt = json.loads(result.stdout)
    assert receipt["ok"] is (assignee == "default")
    if assignee == "b":
        assert receipt["classification"] == "config"
        assert state["requests"] == []


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("race", ["run", "assignee"])
def test_explicit_policy_receipt_cannot_outlive_ownership(explicit_github, race):
    state, _ = explicit_github
    set_config_value("kanban.pr_acceptance_policies", json.dumps({REPO: POLICY}))
    with connect() as conn:
        tid = kb.create_task(conn, title="Race", assignee="default", completion_contract=PR)
        run_id = kb.claim_task(conn, tid).current_run_id if race == "run" else None
        def replace():
            with connect() as rival:
                if race == "assignee":
                    assert kb.assign_task(rival, tid, "b")
                else:
                    assert kb.block_task(rival, tid, reason="Replace run")
                    assert kb.unblock_task(rival, tid)
                    assert kb.claim_task(rival, tid).current_run_id != run_id
        state["race"] = replace
        assert not kb.complete_task(conn, tid, summary="Attempt", expected_run_id=run_id, metadata={"published_pr": PR})
        assert kb.get_task(conn, tid).status != "done"
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_readonly_probe_reports_real_requests_without_board_mutation(explicit_github, conclusion):
    import subprocess

    state, _ = explicit_github
    state["conclusion"] = conclusion
    set_config_value("kanban.pr_acceptance_policies", json.dumps({REPO: POLICY}))
    with connect() as conn:
        before = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
    result = subprocess.run([sys.executable, "-m", "hermes_cli.kanban_pr_probe", "--contract", PR,
                             "--published-pr", PR, "--assignee", "default"],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == (0 if conclusion == "success" else 1), result.stderr
    report = json.loads(result.stdout)
    assert report["receipt"]["ok"] is (conclusion == "success")
    assert report["api_requests"] == [p.removeprefix("/") for p in state["requests"]]
    assert len(report["api_requests"]) == 4
    assert report["collector_path"].endswith("hermes_cli/kanban_pr_acceptance.py")
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == before
