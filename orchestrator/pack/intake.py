"""`orch pack start`: manifest -> provisional pack -> contract -> contract review.

Intake is where a change becomes packs the engine can run, and the order is
load-bearing: the manifest is validated before anything is created, and the
contract is assembled and hashed before a reviewer is dispatched.  A pack that
exists before its manifest is known good would have to be unwound; a contract
review of an unhashed contract could not be bound to what it reviewed.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

from .errors import ContractRejected
from .identities import contract_hash, environment_digest, package_digest, plan_digest
from .manifest import active_obligations, approved_checks, deferred_obligations, validate_manifest
from .store import PackStore
from .target import TargetPackage

ENGINE_EXCLUDES = ("pack-state.json",)

PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")


def unsatisfied_placeholders(template: Sequence[str], params: dict[str, Any]) -> set[str]:
    """Placeholders in an argv template that no parameter supplies."""
    needed: set[str] = set()
    for item in template:
        needed.update(PLACEHOLDER.findall(item))
    return needed - set(params)


CONTRACT_RECORD = "contract"
TARGET_RECORD = "target_dir"


def assemble_contract(*, target: TargetPackage, change: str, pack_id: str,
                      manifest: dict[str, Any], requirement: str,
                      manifest_sha256_value: str, base_revision: str,
                      candidate_input: str | None, environment: dict[str, Any],
                      target_checks: Sequence[dict[str, Any]] = (),
                      dependencies: dict[str, Any] | None = None,
                      approvals: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the closed contract for one pack (IDENTITIES §2.4).

    Only fields the spec lists appear here.  Adding one would create an input
    that changes behaviour but not the hash, which is precisely the failure the
    closed set exists to prevent.
    """
    pack = next(p for p in manifest["packs"] if p["id"] == pack_id)
    profile = target.profile
    # The target's own `checks[]` is authoritative for what a selector means -
    # normalising markdown, resolving aliases, anything target-specific.  The
    # engine derives the *set* from the manifest but must not re-translate the
    # selectors itself, or there would be two answers (joint-r2).
    declared_selectors = {
        entry["check_id"]: entry for entry in target_checks
        if entry.get("pack") in (None, pack_id)
    }
    checks = approved_checks(manifest, pack_id)
    for check in checks:
        override = declared_selectors.get(check["check_id"])
        if override is None:
            raise ContractRejected(
                "untranslated_selector",
                f"{check['check_id']}: the target did not report this check",
            )
        check["selector"] = override["selector"]
        check["result_kind"] = override.get("result_kind", check["result_kind"])
        # Obligation-derived checks are `obligation_selector` kind: the selector
        # is a command fragment for the target's obligation template, not the
        # name of a declared check (ENVELOPES §1.2).  Named profile checks are
        # the separate `target_fixed` / `command` kinds.
        declared = target.obligation_check()
        check["tools"] = list(declared.get("tools", []))
        check["argv_template"] = list(declared.get("argv_template", []))
        check["result_kind"] = declared.get("result_kind", check["result_kind"])
        check["params"] = dict(declared.get("params") or {})
        check["params"]["selector"] = check["selector"]
        missing = unsatisfied_placeholders(check["argv_template"], check["params"])
        if missing:
            # Caught at assembly, not at run time: a contract whose command
            # cannot be rendered is unrunnable, and discovering that during a
            # verify invocation would spend a dispatch to learn it.
            raise ContractRejected(
                "unrenderable_check",
                f"{check['check_id']}: no value for {sorted(missing)}",
            )

    return {
        "policy_version": "pack-v1",
        "target_id": target.target_id,
        "change": change,
        "pack": pack_id,
        "contract_version": pack["contract_version"],
        "contract_revision": 1,
        "manifest_sha256": manifest_sha256_value,
        "requirement_fingerprint": requirement,
        "plan_digest": plan_digest(manifest),
        "active_obligations": active_obligations(manifest, pack_id),
        "deferred_obligations": deferred_obligations(manifest, pack_id),
        "files_writable": sorted(pack["files_writable"]),
        "base_revision": base_revision,
        "candidate_fingerprint_input": candidate_input,
        "dependency_revisions": dict(dependencies or {}),
        "evidence_receipts": {},
        "approved_checks": checks,
        "prerun_checks": [{"check_id": check["check_id"], "params": check["params"]}
                          for check in checks],
        "prerun_reads": [],
        "readable_roots": sorted(profile.get("readable_roots") or []),
        "hotspot_paths": sorted(profile.get("hotspot_paths") or []),
        "declared_config_refs": [],
        "excludes": {
            "engine": list(ENGINE_EXCLUDES),
            "target": list(profile.get("target_excludes") or []),
        },
        "protected_source_globs": sorted(profile.get("protected_source_globs") or []),
        "home_seeds": list(profile.get("home_seeds") or []),
        "budget_policy": {
            "attempt_cap": 2, "round_cap": 4, "reviewer_retry": 2, "verify_retry": 2,
            "exception_grants": 1, "wait_cap": 900, "recovery_ops": 2, "call_budget": 12,
        },
        "execution_policy": {},
        "prompt_templates": [],
        "environment_digest": environment_digest(environment),
        "approvals": approvals or {"allow_apply": None, "allow_major_plan_change": None},
    }


def build_environment(target: TargetPackage, tool_versions: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {
        "target_package_digest": package_digest(target.root),
        "target_package_version": target.version,
        "tool_versions": tool_versions,
        "provider_cli_versions": {},
        "engine_package_digest": None,
        "engine_version": None,
        "config_refs_content": [],
        "effective_env": {},
        "loader_policy": {
            "python": ["-I", "-S", "-B", "-X", "pycache_prefix={fresh_empty_dir}"],
            "node": {"execPath_pinned": True, "options_env": "cleared"},
        },
    }


def dependency_revisions(store: PackStore, pack: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Bind each upstream pack's accepted receipt, or report what is missing.

    A pack that reads another pack's deliverable has to name *which* accepted
    revision of it, or the work is written against something that may already
    have changed (IDENTITIES §2.4.1).  An unsatisfied dependency is not a
    contract defect to review around - the pack simply is not ready, which is
    what `blocked_deps` means (STATE-TABLE §1.1).
    """
    bound: dict[str, Any] = {}
    missing: list[str] = []
    for upstream in (pack.get("depends") or {}).get("packs", []):
        upstream_id = upstream["id"] if isinstance(upstream, dict) else upstream
        receipts = [r for r in store.receipts(upstream_id, "acceptance")]
        if not receipts:
            missing.append(upstream_id)
            continue
        latest = receipts[-1]
        bound[upstream_id] = {
            "candidate_output": latest["payload"].get("candidate_output"),
            "accepted_contract_hash": latest["payload"].get("contract_hash"),
            "acceptance_receipt_sha256": latest["sha256"],
        }
    return bound, missing


def start_packs(store: PackStore, *, target: TargetPackage, change_dir: Path,
                base_revision: str, host_boot_id: str | None = None,
                workspace: Path | None = None) -> list[dict[str, Any]]:
    """Validate the change and create one provisional pack per manifest pack.

    Returns one record per pack with its assembled contract hash, ready for the
    contract review each of them must pass before any producer is dispatched.
    """
    payload = target.validate_plan(change_dir)
    manifest = payload["manifest"]
    requirement = target.requirement_fingerprint(change_dir)["digest"]

    # Validated first: creating packs from a manifest that then turns out to be
    # malformed would leave rows describing work nobody authorised.
    validate_manifest(manifest, validator_version=target.validator_version)

    import hashlib
    import json as _json

    manifest_bytes = _json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    manifest_sha = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()

    tool_versions: dict[str, dict[str, str]] = {}
    obligation_check = target.obligation_check()
    for pack in manifest["packs"]:
        for check in approved_checks(manifest, pack["id"]):
            tools = list(obligation_check.get("tools", []))
            if tools:
                # Per invocation, keyed the way EV-V-003 compares it.
                from .verify_runner import invocation_id

                tool_versions[invocation_id(check["check_id"], {})] = target.env_probe(
                    tools, workspace=workspace)
    environment = build_environment(target, tool_versions)

    started: list[dict[str, Any]] = []
    for pack in manifest["packs"]:
        bound, missing = dependency_revisions(store, pack)
        if missing:
            # Not contracted at all: there is nothing to review until the
            # upstream is accepted, and contracting now would bind a revision
            # that does not exist yet.
            store.create_pack(pack["id"], target_id=target.target_id,
                              change=change_dir.name, state="blocked_deps",
                              host_boot_id=host_boot_id)
            store.update_pack(pack["id"],
                              blockers=[{"reason": "blocked_deps", "packs": sorted(missing)}])
            started.append({"pack": pack["id"], "contract_hash": None, "contract": None,
                            "blocked_on": sorted(missing)})
            continue
        contract = assemble_contract(
            target=target, change=change_dir.name, pack_id=pack["id"], manifest=manifest,
            requirement=requirement, manifest_sha256_value=manifest_sha,
            base_revision=base_revision, candidate_input=None, environment=environment,
            target_checks=payload.get("checks", []),
            dependencies=bound,
        )
        digest = contract_hash(contract)
        store.create_pack(pack["id"], target_id=target.target_id, change=change_dir.name,
                          state="contracting", host_boot_id=host_boot_id)
        store.update_pack(pack["id"], contract_hash=digest,
                          contract_version=pack["contract_version"],
                          return_point="claimed")
        # The contract body, not only its hash: every later stage works from it,
        # and a hash alone cannot be read back.  Keyed by the hash so a revised
        # contract is a new record rather than an overwrite of the one a sealed
        # call was bound to.
        store.add_record(f"CONTRACT-{digest.split(':', 1)[1][:16]}", CONTRACT_RECORD,
                         {"contract_hash": digest, "contract": contract,
                          # The probed environment, not only its digest: a later
                          # stage that re-probed would get a different answer,
                          # which is the drift the digest exists to detect.
                          "environment": environment},
                         pack_id=pack["id"])
        started.append({"pack": pack["id"], "contract_hash": digest, "contract": contract})
    return started


def current_record(store: PackStore, pack_id: str) -> dict[str, Any] | None:
    """The whole contract record - contract and the environment it was built on."""
    pack = store.get_pack(pack_id)
    for record in store.records_of_kind(CONTRACT_RECORD, pack_id):
        if not record["revoked"] and record["payload"]["contract_hash"] == pack["contract_hash"]:
            return record["payload"]
    return None


def current_contract(store: PackStore, pack_id: str) -> dict[str, Any] | None:
    """The contract the pack is running under right now, or None.

    Matched on the pack's own `contract_hash` rather than "the newest record":
    a revision that has been recorded but not adopted must not be handed to a
    stage still bound to the previous one.
    """
    pack = store.get_pack(pack_id)
    for record in store.records_of_kind(CONTRACT_RECORD, pack_id):
        if not record["revoked"] and record["payload"]["contract_hash"] == pack["contract_hash"]:
            return record["payload"]["contract"]
    return None
