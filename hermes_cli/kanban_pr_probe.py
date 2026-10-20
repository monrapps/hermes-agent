"""Read-only acceptance probe: python -m hermes_cli.kanban_pr_probe.

No board is opened or mutated. The lifecycle owner must recollect at completion;
this output is diagnostic evidence, not an importable acceptance receipt.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv=None) -> int:
    from hermes_cli import kanban_pr_acceptance as acceptance

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=acceptance.validate_contract)
    parser.add_argument("--published-pr")
    parser.add_argument("--assignee", required=True)
    args = parser.parse_args(argv)
    requests = []

    def observe(event, values):
        # Audit only the argv endpoint, never the subprocess environment or credentials.
        if event == "subprocess.Popen":
            command = values[1]
            if isinstance(command, (list, tuple)) and len(command) > 2 and command[:2] == ["gh", "api"]:
                requests.append(command[2])

    sys.addaudithook(observe)
    receipt = acceptance.collect_acceptance(args.contract, args.published_pr, assignee=args.assignee)
    print(json.dumps({"receipt": receipt, "api_requests": requests,
                      "collector_path": acceptance.__file__}, indent=2))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
