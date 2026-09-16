"""validate-plan for the fixture target: a fixed tasks.md becomes a manifest.

Only the parts the engine's contract actually consumes are produced here, and
`text_changed_tasks` is computed the way the real validator must: by comparing
the *previous* normalised task text, which is why `--previous-tasks` exists at
all (joint-r2).  Without that input the field is omitted, and the engine is
required to treat its absence as "cannot be minor" rather than as "nothing
changed".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import canonical, emit, fail, sha256_file  # noqa: E402

TASK_HEADING = re.compile(r"^##\s+(?P<id>T\d+)\s*[—-]\s*(?P<title>.+)$")
FILES_LINE = re.compile(r"^Files:\s*(?P<files>.+)$")


def parse_tasks(text: str) -> dict[str, dict]:
    """Return {task_id: {title, files, prose}} from the fixed markdown shape."""
    tasks: dict[str, dict] = {}
    current = None
    for line in text.splitlines():
        heading = TASK_HEADING.match(line.strip())
        if heading:
            current = heading.group("id")
            tasks[current] = {"title": heading.group("title").strip(), "files": [], "prose": []}
            continue
        if current is None:
            continue
        files = FILES_LINE.match(line.strip())
        if files:
            tasks[current]["files"] = [p.strip() for p in files.group("files").split(",")]
            continue
        if line.strip():
            tasks[current]["prose"].append(line.strip())
    return tasks


def prose_of(task: dict) -> str:
    """Everything except the `files` field - the text a minor must not change."""
    return task["title"] + "\n" + "\n".join(task["prose"])


def build_manifest(change_dir: Path, requirement: str, version: int) -> dict:
    tasks = parse_tasks((change_dir / "tasks.md").read_text(encoding="utf-8"))
    files_writable = sorted({f for task in tasks.values() for f in task["files"]})
    obligations = [{
        "id": "O1",
        "source": "specs/hello/test-spec.md#1.1",
        "disposition": "active",
        "claimed_by": [{"task": task_id, "disposition": "active"} for task_id in sorted(tasks)],
        "forbids": [],
        "verify_by": ["unit"],
        "selector": "hello",
    }]
    manifest = {
        "schema_version": 1,
        "change": change_dir.name,
        "generated_by": f"validate_plan.py@{(change_dir.parent / 'VERSION').read_text().strip()}"
        if (change_dir.parent / "VERSION").is_file() else "validate_plan.py@0.1.0",
        "source_fingerprints": {
            "design_md": "sha256:" + sha256_file(change_dir / "design.md"),
            "test_spec": {
                "specs/hello/test-spec.md":
                    "sha256:" + sha256_file(change_dir / "specs" / "hello" / "test-spec.md"),
            },
        },
        "requirement_fingerprint": requirement,
        "plan_fingerprint": "",
        "packs": [{
            "id": "P1",
            "contract_version": version,
            "title": "hello",
            "tasks": sorted(tasks),
            "files_writable": files_writable,
            "obligations": obligations,
            "depends": {"packs": [], "evidence": []},
            "tier": "standard",
            "tier_basis_ref": "design.md#tier",
        }],
        "deferred": [],
        "evidence_declared": [],
        "coverage": {
            "obligations_total": 1,
            "obligations_active": 1,
            "obligations_deferred": 0,
            "obligations_uncovered": [],
        },
    }
    return manifest


def plan_fingerprint(manifest: dict) -> str:
    subset = {
        "packs": [{
            "id": pack["id"], "tasks": pack["tasks"], "files_writable": pack["files_writable"],
            "obligations": [ob["id"] for ob in pack["obligations"]],
            "depends": pack["depends"], "tier": pack["tier"],
        } for pack in manifest["packs"]],
        "deferred": [item["ref"] if isinstance(item, dict) else item
                     for item in manifest["deferred"]],
        "evidence_declared": manifest["evidence_declared"],
    }
    return hashlib.sha256(canonical(subset)).hexdigest()[:12]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--change-dir", required=True)
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--accept-plan-change", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--previous-manifest")
    parser.add_argument("--previous-tasks")
    parser.add_argument("--contract-version", type=int, default=1)
    args = parser.parse_args(argv)

    change_dir = Path(args.change_dir)
    if not (change_dir / "tasks.md").is_file():
        return fail("no tasks.md in change dir", 3)

    # Imported, not spawned: a second interpreter started from here would not
    # go through the engine bootstrap, so it would write bytecode into the
    # package and change the package digest (IDENTITIES §2.6).
    import requirement_fingerprint

    files, problem = requirement_fingerprint.collect(change_dir)
    if problem:
        return fail(f"rejected: {problem}")
    blob = bytearray()
    for relpath, path in sorted(files, key=lambda item: item[0]):
        blob += relpath.encode() + b"\x00" + path.read_bytes() + b"\x00"
    requirement = "sha256:" + hashlib.sha256(bytes(blob)).hexdigest()
    manifest = build_manifest(change_dir, requirement, args.contract_version)
    manifest["plan_fingerprint"] = plan_fingerprint(manifest)

    payload = {
        "manifest": manifest,
        "coverage": manifest["coverage"],
        # Keyed by obligation id, with the selector already normalised: the
        # target is the single authority for what a selector means, and the
        # engine consumes this rather than re-deriving it (joint-r2).
        "checks": [
            {"check_id": ob["id"], "pack": pack["id"],
             "selector": ob["selector"], "result_kind": "test"}
            for pack in manifest["packs"] for ob in pack["obligations"]
            if ob.get("selector")
        ],
    }

    if args.previous_tasks:
        previous = parse_tasks(Path(args.previous_tasks).read_text(encoding="utf-8"))
        current = parse_tasks((change_dir / "tasks.md").read_text(encoding="utf-8"))
        changed = sorted(
            task_id for task_id in current
            if task_id in previous and prose_of(previous[task_id]) != prose_of(current[task_id])
        )
        payload["plan_change"] = {"packs": {"P1": {"text_changed_tasks": changed}}}
    # Without --previous-tasks the field is omitted entirely: reporting `[]`
    # would assert prose is unchanged when it was never compared.

    if args.write_manifest:
        (change_dir / "pack-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return emit(payload)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
