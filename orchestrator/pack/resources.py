"""Engine-owned resource assignment and release verification (PLAN §3.2).

A verify run borrows a MySQL schema, a Redis key prefix and some directories.
Two rules make the borrowing safe:

* **The engine assigns, the target uses.**  Names are derived from the pack and
  operation, so two runs can never collide, and nothing the CLI reports can
  change which resource it was given.
* **Release is verified, not reported.**  `cleanup[].status` is the CLI's claim;
  `verify_released` is the engine looking.  A claim of `released` that the check
  contradicts is recorded as `cleanup_claim_mismatch` rather than believed - a
  leaked schema would otherwise let the next run inherit this one's state and
  quietly pass (ENVELOPES §3.4).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Protocol, Sequence

RELEASED = "released"
STILL_PRESENT = "still_present"
UNKNOWN = "unknown"


class ResourceAdapter(Protocol):
    """How one kind of resource is created, released and checked."""

    kind: str

    def create(self, name: str) -> None: ...
    def release(self, name: str) -> None: ...
    def verify_released(self, name: str) -> str: ...


class DirectoryAdapter:
    kind = "dir"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, name: str) -> Path:
        return self.root / name

    def create(self, name: str) -> None:
        self._path(name).mkdir(parents=True, exist_ok=True)

    def release(self, name: str) -> None:
        shutil.rmtree(self._path(name), ignore_errors=True)

    def verify_released(self, name: str) -> str:
        return STILL_PRESENT if self._path(name).exists() else RELEASED


class MySQLSchemaAdapter:
    """Schema lifecycle over an injected connector.

    The connector is injected rather than imported so the engine has no driver
    dependency and the fixtures can drive every failure mode - including the one
    that matters most, a probe that cannot answer.
    """

    kind = "mysql_schema"

    def __init__(self, connector: Any) -> None:
        self.connector = connector

    def create(self, name: str) -> None:
        self.connector.execute(f"CREATE SCHEMA `{name}`")

    def release(self, name: str) -> None:
        self.connector.execute(f"DROP SCHEMA IF EXISTS `{name}`")

    def verify_released(self, name: str) -> str:
        try:
            rows = self.connector.query("SHOW SCHEMAS LIKE %s", (name,))
        except Exception:
            # Cannot see is not the same as absent.  Reporting `unknown` keeps
            # the observation unusable instead of passing on a guess.
            return UNKNOWN
        return RELEASED if not rows else STILL_PRESENT


class RedisPrefixAdapter:
    kind = "redis_prefix"

    def __init__(self, client: Any) -> None:
        self.client = client

    def create(self, name: str) -> None:
        # A prefix needs no creation; assignment is the whole act.
        return None

    def release(self, name: str) -> None:
        for key in list(self.client.scan_iter(match=f"{name}*")):
            self.client.delete(key)

    def verify_released(self, name: str) -> str:
        try:
            remaining = list(self.client.scan_iter(match=f"{name}*"))
        except Exception:
            return UNKNOWN
        return RELEASED if not remaining else STILL_PRESENT


class ResourceManager:
    """Assigns resources for one operation and audits their release."""

    def __init__(self, adapters: dict[str, ResourceAdapter]) -> None:
        self.adapters = adapters

    def assign(self, pack_id: str, op_id: str,
               declarations: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create each declared resource under a name derived from the operation."""
        assigned: list[dict[str, Any]] = []
        for declaration in declarations:
            kind = declaration["kind"]
            adapter = self.adapters.get(kind)
            if adapter is None:
                raise KeyError(f"no adapter for resource kind {kind!r}")
            name = self.name_for(kind, pack_id, op_id, declaration["resource_id"])
            adapter.create(name)
            assigned.append({
                "resource_id": declaration["resource_id"],
                "kind": kind,
                "name": name,
                "engine_owned": bool(declaration.get("engine_owned", False)),
            })
        return assigned

    @staticmethod
    def name_for(kind: str, pack_id: str, op_id: str, resource_id: str) -> str:
        stem = f"{pack_id}_{op_id}_{resource_id}".lower()
        safe = "".join(ch if ch.isalnum() else "_" for ch in stem)
        if kind == "redis_prefix":
            return f"orch:{safe}:"
        return f"orch_{safe}"

    def release_engine_owned(self, assigned: Sequence[dict[str, Any]]) -> dict[str, str]:
        """Clean up what the engine owns and report per resource."""
        results: dict[str, str] = {}
        for resource in assigned:
            if not resource["engine_owned"]:
                continue
            adapter = self.adapters[resource["kind"]]
            try:
                adapter.release(resource["name"])
            except Exception:
                results[resource["resource_id"]] = UNKNOWN
                continue
            results[resource["resource_id"]] = adapter.verify_released(resource["name"])
        return results

    def verify_released(self, assigned: Sequence[dict[str, Any]]) -> dict[str, str]:
        """Check what the *target* was supposed to release."""
        results: dict[str, str] = {}
        for resource in assigned:
            if resource["engine_owned"]:
                continue
            adapter = self.adapters[resource["kind"]]
            try:
                results[resource["resource_id"]] = adapter.verify_released(resource["name"])
            except Exception:
                results[resource["resource_id"]] = UNKNOWN
        return results


def cleanup_gate(cli_cleanup: Sequence[dict[str, Any]],
                 verified: dict[str, str],
                 engine_cleanup: dict[str, str]) -> tuple[bool, list[str]]:
    """ENVELOPES §3.4 condition 3: all three sub-conditions, with reasons.

    The claim/observation mismatch is reported separately from a plain leak,
    because they call for different responses: one is a broken CLI, the other a
    broken cleanup.
    """
    reasons: list[str] = []

    claims = {entry["resource_id"]: entry.get("status") for entry in cli_cleanup}
    if any(status == "failed" for status in claims.values()):
        # A cleanup the CLI itself calls failed is not rescued by the engine
        # finding the resource gone: that CLI's reporting cannot be trusted.
        reasons.append("cli_cleanup_failed")

    # (b) what the target was told to release, checked by the engine.
    for resource_id, observed in sorted(verified.items()):
        if observed == RELEASED:
            continue
        reasons.append(f"cleanup_not_released:{resource_id}:{observed}")
        if claims.get(resource_id) == RELEASED:
            # Separate from the leak itself: a CLI that reports success over a
            # resource still present is broken in a different way.
            reasons.append(f"cleanup_claim_mismatch:{resource_id}")

    # (c) what the engine owns, released by the engine.
    for resource_id, observed in sorted(engine_cleanup.items()):
        if observed != RELEASED:
            reasons.append(f"engine_cleanup_failed:{resource_id}:{observed}")

    return (not reasons), sorted(set(reasons))


# --------------------------------------------------------------------------
# read-only dependency cache (IMPLEMENTATION-PLAN §3.2)
# --------------------------------------------------------------------------

def warm_dependency_cache(source: Path, target: Path, *, digest_fn=None) -> dict[str, Any]:
    """Populate an engine-owned read-only dependency cache.

    Each verify operation gets an empty tool home so no state carries between
    runs; without a shared read-only cache that would mean re-resolving every
    dependency, over the network, on every invocation.  The cache is built once
    by this command, made read-only, and its digest joins the environment
    identity so a changed cache is a visible environment refresh rather than an
    invisible input.

    Which tool this serves, and which environment variable points at it, are
    declared by the target profile: the engine only owns the directory and its
    digest.
    """
    source, target = Path(source), Path(target)
    if not source.is_dir():
        raise FileNotFoundError(f"no dependency cache at {source}")
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=False, ignore_dangling_symlinks=True)

    files = 0
    for path in target.rglob("*"):
        if path.is_file():
            files += 1
            path.chmod(0o444)
    for path in sorted(target.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
    target.chmod(0o555)

    digest = None
    if digest_fn is not None:
        digest = digest_fn(target)
    return {"path": str(target), "files": files, "digest": digest}
