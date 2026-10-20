"""Validate exact-head check-run evidence before matching app-pinned requirements."""
from __future__ import annotations

import re


def _positive_id(value) -> bool:
    return type(value) is int and value > 0


def _validate_run(run):
    if not isinstance(run, dict) or not _positive_id(run.get("id")):
        raise ValueError("Invalid check-run identity")
    app = run.get("app")
    if not isinstance(app, dict) or not _positive_id(app.get("id")):
        raise ValueError("Invalid check-run app identity")
    if not isinstance(run.get("name"), str) or not run["name"]:
        raise ValueError("Invalid check-run context")
    if not isinstance(run.get("head_sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", run["head_sha"]):
        raise ValueError("Invalid check-run SHA")
    if not isinstance(run.get("html_url"), str) or not run["html_url"].startswith("https://github.com/"):
        raise ValueError("Missing check-run URL")
    if "conclusion" not in run or not isinstance(run.get("status"), str):
        raise ValueError("Incomplete check-run result")


def checked_runs(pages):
    if not isinstance(pages, list) or not pages:
        raise ValueError("Missing check-run pages")
    runs = []
    counts = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
            raise ValueError("Invalid check-run page")
        total = page.get("total_count")
        if type(total) is not int or total < 0:
            raise ValueError("Invalid check-run count")
        counts.append(total)
        runs.extend(page["check_runs"])
    for run in runs:
        _validate_run(run)
    if len({r["id"] for r in runs}) != len(runs) or any(count != len(runs) for count in counts):
        raise ValueError("Incomplete check-run pagination")
    return runs
