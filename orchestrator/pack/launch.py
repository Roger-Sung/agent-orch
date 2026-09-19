"""`orch pack-start`: turn a change directory into packs the daemon can run.

The pieces existed and nothing joined them: `start_packs` built the pack rows
and their contracts, and no task was ever created against them, so the daemon
had nothing to pick up and every pack-v1 branch in the controller was
unreachable from the outside.

One task per manifest pack, sharing the pack's id.  The controller reads the
pack by `task["id"]`, so the two identities are the same one; giving a pack a
task under a different name would leave the machine looking up a pack that does
not exist.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..execution import SCHEMA_VERSION, render_plan, resolve_request
from ..profile import Profile, load_profile
from . import revocation
from .intake import start_packs
from .target import load_target

# Which pack role each stage of the pack-v1 profile is dispatched under.
STAGE_ROLES = {
    "contract_review": "contract_review",
    "apply": "producer",
    "prerun": "producer",
    "review": "reviewer",
    "repair": "producer",
}

ROLE_MODEL_ARG = {"producer": "producer_model", "contract_review": "reviewer_model",
                  "reviewer": "reviewer_model"}


def _identifier(value: str) -> str:
    """A path-safe identity for the execution request."""
    return "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in value)


def build_request(profile: Profile, *, change: str, pack_id: str,
                  producer_model: str, reviewer_model: str,
                  effort: str = "medium") -> dict[str, Any]:
    """The execution request for one pack, covering every nonterminal stage."""
    models = {"producer_model": producer_model, "reviewer_model": reviewer_model}
    stages: dict[str, Any] = {}
    for name, stage in profile.stages.items():
        if stage.terminal:
            continue
        try:
            role = STAGE_ROLES[name]
        except KeyError:
            raise ValueError(
                f"stage {name!r} has no pack role; the profile and this launcher disagree"
            ) from None
        stages[name] = {"role": role, "provider": stage.owner,
                        "model": models[ROLE_MODEL_ARG[role]], "effort": effort}
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": "pack-v1",
        "logical_work_id": _identifier(change),
        "spec_series_id": _identifier(pack_id),
        "stages": stages,
    }


def _input_text(record: dict[str, Any], plan_frame: str) -> str:
    """The frozen input: the plan prefix, then the contract the pack runs under.

    The contract is the task text rather than a summary of it, because every
    stage's prompt tells the provider to work from the contract and a summary
    would be a second, divergent statement of the same thing.
    """
    contract = json.dumps(record["contract"], ensure_ascii=False, indent=2, sort_keys=True)
    return (f"{plan_frame}# Pack {record['pack']}\n\n"
            f"Contract hash: {record['contract_hash']}\n\n"
            "## Dispatch contract\n\n```json\n" + contract + "\n```\n")


def launch_packs(controller: Any, *, target_dir: Path, change_dir: Path,
                 workspace: Path, profile_path: Path, base_revision: str,
                 producer_model: str, reviewer_model: str,
                 effort: str = "medium") -> list[dict[str, Any]]:
    """Create every pack of a change, and a task for each that is ready to run."""
    target = load_target(Path(target_dir))
    profile = load_profile(Path(profile_path))
    intake_dir = Path(controller.home) / "pack-intake"
    intake_dir.mkdir(parents=True, exist_ok=True)

    started = start_packs(
        controller.pack_store, target=target, change_dir=Path(change_dir),
        base_revision=base_revision, workspace=Path(workspace),
        host_boot_id=revocation.boot_session_uuid(),
    )

    launched: list[dict[str, Any]] = []
    for record in started:
        pack_id = record["pack"]
        if record.get("contract") is None:
            # Blocked on an upstream that has not been accepted: contracting it
            # now would bind a revision that does not exist yet.
            launched.append({"pack": pack_id, "task": None,
                             "blocked_on": record.get("blocked_on", [])})
            continue
        request = build_request(profile, change=Path(change_dir).name, pack_id=pack_id,
                                producer_model=producer_model,
                                reviewer_model=reviewer_model, effort=effort)
        plan = resolve_request(request, profile)
        path = intake_dir / f"{pack_id}.md"
        path.write_text(_input_text(record, render_plan(plan)), encoding="utf-8")
        controller.submit(profile.type, Path(profile_path), path,
                          task_id=pack_id, workspace=Path(workspace))
        launched.append({"pack": pack_id, "task": pack_id,
                         "contract_hash": record["contract_hash"]})
    return launched
