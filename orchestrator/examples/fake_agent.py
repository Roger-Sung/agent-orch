#!/usr/bin/env python3
"""Deterministic CLI-shaped provider used only by the MVP demo and tests."""

from __future__ import annotations

import os
import sys


def _route_success_mode() -> bool:
    return "ORCH_FAKE_AGENT_ROUTE_SUCCESS" in os.environ


def _success_outcome(prompt: str) -> str | None:
    marker = "Allowed typed outcomes:"
    for line in prompt.splitlines():
        if marker not in line:
            continue
        raw = line.split(marker, 1)[1].strip().rstrip(".")
        allowed = {part.strip() for part in raw.split(",") if part.strip()}
        for preferred in ("implemented", "applied", "reviewed", "ready", "allow", "drafted", "submit"):
            if preferred in allowed:
                return preferred
        return sorted(allowed)[0] if allowed else None
    return None


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: fake_agent.py OWNER PROMPT", file=sys.stderr)
        return 2
    owner = sys.argv[1]
    prompt = sys.argv[2]
    if _route_success_mode():
        outcome = _success_outcome(prompt)
        if outcome is None:
            print("fake route-success mode could not infer an allowed outcome", file=sys.stderr)
            return 2
        print(f"{owner} fake route-success stage")
        print('{"usage":{"input_tokens":11,"output_tokens":7,"total_tokens":18}}')
        print(f"ORCHESTRATOR_OUTCOME: {outcome}")
        return 0
    if owner == "claude":
        print("claude demo stage: drafted from", len(prompt), "prompt characters")
        print("ORCHESTRATOR_OUTCOME: submit")
        return 0
    if owner == "codex":
        print("codex demo stage: requesting one more draft pass")
        print("ORCHESTRATOR_OUTCOME: block")
        return 0
    print(f"unknown owner: {owner}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
