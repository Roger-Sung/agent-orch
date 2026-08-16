from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from .cli import default_home
from .controller import Controller


def main() -> int:
    root = Path(__file__).resolve().parent
    fake = root / "examples" / "fake_agent.py"
    python = shlex.quote(sys.executable)
    fake_path = shlex.quote(str(fake))
    os.environ["ORCH_CLAUDE_COMMAND"] = f"{python} {fake_path} claude"
    os.environ["ORCH_CODEX_COMMAND"] = f"{python} {fake_path} codex"
    controller = Controller(default_home())
    try:
        task_id = controller.submit(
            "demo-loop", root / "examples" / "demo-loop.yaml", root / "examples" / "demo-input.md"
        )
        result = controller.run_until_stop(task_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
