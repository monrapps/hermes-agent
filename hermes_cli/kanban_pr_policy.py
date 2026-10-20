"""Profile-owned, opt-in exact-head CI requirements; never a protection-API fallback."""
from __future__ import annotations

from pathlib import Path
import re


class PolicyError(ValueError):
    """Invalid or inaccessible acceptance policy: fail closed before network reads."""


def _branch_valid(branch) -> bool:
    if not isinstance(branch, str) or not branch or branch == "@":
        return False
    if re.search(r"[\x00-\x20\x7f~^:?*\[\\]", branch) or any(s in branch for s in ("..", "//", "@{")):
        return False
    return all(part and not part.startswith(".") and not part.endswith((".", ".lock"))
               for part in branch.split("/"))


def _validate_policy(policy) -> dict:
    if not isinstance(policy, dict) or set(policy) != {"mode", "base_branch", "required_checks"}:
        raise PolicyError("Each PR acceptance policy requires mode, base_branch and required_checks.")
    if policy["mode"] != "explicit" or not _branch_valid(policy["base_branch"]):
        raise PolicyError("PR acceptance policy requires explicit mode and a valid base branch.")
    checks = policy["required_checks"]
    if not isinstance(checks, list) or not checks:
        raise PolicyError("Explicit PR acceptance requires a nonempty required_checks list.")
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"context", "app_id"}:
            raise PolicyError("Each required check needs exactly context and app_id.")
        context, app_id = check["context"], check["app_id"]
        if not isinstance(context, str) or not context.strip() or context != context.strip():
            raise PolicyError("Required check contexts must be nonempty exact names.")
        if type(app_id) is not int or app_id <= 0 or context in seen:
            raise PolicyError("Required checks need positive app IDs and unique contexts.")
        seen.add(context)
    return policy


def load_policy(repo: str, profile_home: str | None) -> tuple[dict | None, str]:
    """Read the assignee's effective config, with no caller-config or broken-YAML fallback."""
    import hermes_yaml as yaml
    from agent.safe_worker_policy import worker_config_snapshot
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.kanban_pr_acceptance import _REPO

    home = Path(profile_home) if profile_home else get_hermes_home()
    # A frozen worker config belongs to its admitted profile, not to a different assignee.
    if worker_config_snapshot() is not None and home.resolve() != get_hermes_home().resolve():
        raise PolicyError("Acceptance for another profile requires a fresh owner process.")
    secret_values = build_profile_secret_scope(home)
    token = set_hermes_home_override(home)
    secrets = set_secret_scope(secret_values, profile_home=str(home))
    try:
        config = load_user_config_effective(home / "config.yaml", fail_closed=True)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise PolicyError("Cannot read assignee PR acceptance configuration.") from exc
    finally:
        reset_secret_scope(secrets)
        reset_hermes_home_override(token)
    kanban = config.get("kanban", {})
    if not isinstance(kanban, dict):
        raise PolicyError("kanban must be a mapping.")
    policies = kanban.get("pr_acceptance_policies", {})
    if not isinstance(policies, dict):
        raise PolicyError("kanban.pr_acceptance_policies must be a repository mapping.")
    for key, policy in policies.items():
        if not isinstance(key, str) or not _REPO.fullmatch(key):
            raise PolicyError("PR acceptance policies require exact OWNER/REPO keys.")
        _validate_policy(policy)
    return policies.get(repo), str(home)


def pr_open_or_merged(pr) -> bool:
    return pr["state"] == "open" or (pr["state"] == "closed" and pr.get("merged") is True)


def explicit_requirements(repo, number, policy, profile_home):
    from hermes_cli.kanban_pr_acceptance import _api

    pr = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
    branch = pr["base"]["ref"]
    if branch != policy["base_branch"]:
        raise PolicyError("PR base does not match the configured explicit policy branch.")
    if not pr_open_or_merged(pr):
        raise ValueError("PR is closed without merge")
    required = {(r["context"], r["app_id"]) for r in policy["required_checks"]}
    return pr["head"]["sha"], branch, required


def repository_requirements(repo, number, profile_home):
    """Unconfigured repositories keep the existing branch/ruleset policy unchanged."""
    import json
    from urllib.parse import quote
    from hermes_cli.kanban_pr_acceptance import _api, _GateAuthError

    owner, name = repo.split("/")
    query = f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{pullRequest(number:{number:d}){{headRefOid baseRefName state\n            baseRef{{branchProtectionRule{{requiredStatusChecks{{context app{{databaseId}}}}}}}}}}}}}}'
    repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
    if repository is None:
        raise _GateAuthError(f"HTTP 404 on graphql {repo}")
    pr = repository["pullRequest"]
    sha, branch = pr["headRefOid"], pr["baseRefName"]
    if pr["state"] not in {"OPEN", "MERGED"}:
        raise ValueError("PR is closed or current head is unavailable")
    protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
    required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
    rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                 paginate=True, profile_home=profile_home)
    for page in rules:
        for rule in page:
            if rule["type"] == "required_status_checks":
                required.update((r["context"], r.get("integration_id"))
                                for r in rule["parameters"]["required_status_checks"])
    return sha, branch, required
