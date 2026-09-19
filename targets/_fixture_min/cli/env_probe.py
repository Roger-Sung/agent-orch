"""env-probe: report the version of each declared tool, or refuse."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import emit, fail  # noqa: E402

PROBES = {"sh": ["sh", "-c", "echo sh-$0", "1"]}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--argv", default="[]")
    parser.add_argument("--env", default="{}")
    parser.add_argument("--tools", required=True)
    # Conditional flag (PLAN §4): the engine always passes it when it knows
    # the workspace. Nothing here is workspace-dependent, so it is accepted
    # and ignored - refusing it would fail a launch the contract allows.
    parser.add_argument("--workspace")
    args = parser.parse_args(argv)

    tools = json.loads(args.tools)
    env = json.loads(args.env)
    versions = {}
    for tool in tools:
        probe = PROBES.get(tool)
        if probe is None:
            # An undeclared tool is a contract error: guessing a version would
            # put an unverified value into the environment identity.
            return fail(f"no probe for tool {tool!r}")
        result = subprocess.run(probe, capture_output=True, text=True, env=env or None)
        if result.returncode != 0:
            return fail(f"probe for {tool!r} failed: {result.stderr.strip()}")
        versions[tool] = result.stdout.strip()
    # The key set must equal what was asked for, exactly (EV-V-003).
    return emit(versions)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
