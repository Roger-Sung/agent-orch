"""Manifest envelope validation and derivation (ENVELOPES §1).

The target's own author-side rules (VP-xxx) stay in the target; the engine only
runs the EM codes below.  Two of them are worth flagging because they exist to
stop a *silently* wrong plan rather than a malformed one:

* **EM-005/EM-013** tie an obligation's disposition to the claims that produced
  it, so a deferred obligation cannot appear without a deferral record naming
  the very task that deferred it.
* **EM-008** recomputes the plan fingerprint instead of trusting the reported
  one - a checksum the producer supplies proves nothing about the producer.
"""
from __future__ import annotations

from typing import Any, Iterable

from .errors import EnvelopeInvalid
from .identities import plan_fingerprint

TOP_LEVEL_KEYS = frozenset({
    "schema_version", "change", "generated_by", "source_fingerprints",
    "requirement_fingerprint", "plan_fingerprint", "packs", "deferred",
    "evidence_declared", "coverage",
})
PACK_KEYS = frozenset({
    "id", "contract_version", "title", "tasks", "files_writable",
    "obligations", "depends", "tier",
})
# Conditional, not absent from the schema: PACK_MANIFEST_SCHEMA requires
# `tier_basis_ref` only for a `low` tier pack, where the six justifying lines
# have to point somewhere, and a real validator omits the key entirely above
# that tier.  ENVELOPES EM-002 listed it inside the exact key set until the
# implementation step corrected it (CLOSURE §12a).
CONDITIONAL_PACK_KEYS = frozenset({"tier_basis_ref"})
TIERS_REQUIRING_BASIS = frozenset({"low"})
OBLIGATION_KEYS = frozenset({
    "id", "source", "disposition", "claimed_by", "forbids", "verify_by", "selector",
})
CLAIM_KEYS = frozenset({"task", "disposition"})
DEFERRED_KEYS = frozenset({"ref", "owner", "release_condition", "affected_packs", "affected_tasks"})
COVERAGE_KEYS = frozenset({
    "obligations_total", "obligations_active", "obligations_deferred", "obligations_uncovered",
})
DISPOSITIONS = frozenset({"active", "deferred"})

_HEX = set("0123456789abcdef")


def _reject(code: str, detail: str) -> None:
    raise EnvelopeInvalid(code, detail)


def _require_exact_keys(code: str, where: str, value: Any, expected: frozenset[str]) -> None:
    if not isinstance(value, dict):
        _reject(code, f"{where} is not an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        _reject(code, f"{where} missing={missing} extra={extra}")


def _unique(values: Iterable[Any]) -> bool:
    seen = set()
    for value in values:
        if value in seen:
            return False
        seen.add(value)
    return True


def _has_cycle(edges: dict[str, list[str]]) -> bool:
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {node: WHITE for node in edges}

    def visit(node: str) -> bool:
        colour[node] = GREY
        for nxt in edges.get(node, []):
            if colour.get(nxt, WHITE) == GREY:
                return True
            if colour.get(nxt, WHITE) == WHITE and visit(nxt):
                return True
        colour[node] = BLACK
        return False

    return any(colour[node] == WHITE and visit(node) for node in list(edges))


def validate_manifest(manifest: Any, *, validator_version: str | None = None) -> None:
    """Run EM-001 .. EM-013.  Raises ``EnvelopeInvalid`` with the failing code."""
    _require_exact_keys("EM-001", "manifest", manifest, TOP_LEVEL_KEYS)
    if manifest["schema_version"] != 1:
        _reject("EM-001", f"schema_version {manifest['schema_version']!r}")

    packs = manifest["packs"]
    if not isinstance(packs, list):
        _reject("EM-002", "packs is not a list")
    if not _unique(pack.get("id") for pack in packs):
        _reject("EM-002", "duplicate pack id")

    pack_ids = {pack.get("id") for pack in packs}
    all_claims: list[tuple[str, str, str]] = []  # (pack, task, disposition)

    for pack in packs:
        where = f"pack {pack.get('id')!r}"
        _require_exact_keys("EM-002", where, pack,
                            PACK_KEYS | (set(pack) & CONDITIONAL_PACK_KEYS))
        if pack.get("tier") in TIERS_REQUIRING_BASIS and not pack.get("tier_basis_ref"):
            _reject("EM-002", f"{where}: tier {pack['tier']!r} requires tier_basis_ref")
        if not isinstance(pack["contract_version"], int) or pack["contract_version"] < 1:
            _reject("EM-002", f"pack {pack['id']}: contract_version")
        tasks = pack["tasks"]
        if not isinstance(tasks, list) or not tasks or not _unique(tasks):
            _reject("EM-002", f"pack {pack['id']}: tasks must be non-empty and unique")

        obligations = pack["obligations"]
        if not _unique(ob.get("id") for ob in obligations):
            _reject("EM-003", f"pack {pack['id']}: duplicate obligation id")

        for ob in obligations:
            where = f"pack {pack['id']} obligation {ob.get('id')!r}"
            _require_exact_keys("EM-003", where, ob, OBLIGATION_KEYS)
            if ob["disposition"] not in DISPOSITIONS:
                _reject("EM-003", f"{where}: disposition")
            selector = ob["selector"]
            if selector is not None and (not isinstance(selector, str) or not selector):
                _reject("EM-003", f"{where}: selector must be a non-empty string or null")

            claims = ob["claimed_by"]
            if not isinstance(claims, list) or not claims:
                _reject("EM-004", f"{where}: claimed_by must be non-empty")
            for claim in claims:
                _require_exact_keys("EM-004", f"{where} claim", claim, CLAIM_KEYS)
                if claim["task"] not in tasks:
                    _reject("EM-004", f"{where}: claim task {claim['task']!r} not in pack.tasks")
                if claim["disposition"] not in DISPOSITIONS:
                    _reject("EM-004", f"{where}: claim disposition")
                all_claims.append((pack["id"], claim["task"], claim["disposition"]))

            aggregated = "active" if any(c["disposition"] == "active" for c in claims) else "deferred"
            if ob["disposition"] != aggregated:
                _reject("EM-005", f"{where}: disposition {ob['disposition']} != aggregate {aggregated}")

        depends = pack["depends"] or {}
        for dep in depends.get("packs", []):
            if dep.get("id") not in pack_ids:
                _reject("EM-006", f"pack {pack['id']}: depends on unknown pack {dep.get('id')!r}")
        for evidence in depends.get("evidence", []):
            if evidence not in manifest["evidence_declared"]:
                _reject("EM-006", f"pack {pack['id']}: undeclared evidence {evidence!r}")

    edges = {
        pack["id"]: [dep["id"] for dep in (pack["depends"] or {}).get("packs", [])]
        for pack in packs
    }
    if _has_cycle(edges):
        _reject("EM-006", "dependency cycle")

    deferred_items = manifest["deferred"]
    for item in deferred_items:
        _require_exact_keys("EM-007", "deferred item", item, DEFERRED_KEYS)
        if not item["owner"] or not item["release_condition"]:
            _reject("EM-007", f"deferred {item['ref']!r}: owner/release_condition must be non-empty")
        if not set(item["affected_packs"]) <= pack_ids:
            _reject("EM-007", f"deferred {item['ref']!r}: unknown affected_packs")
        for pack in packs:
            if pack["id"] in item["affected_packs"]:
                if not set(item["affected_tasks"]) <= set(pack["tasks"]):
                    _reject("EM-007", f"deferred {item['ref']!r}: unknown affected_tasks")

    recomputed = plan_fingerprint(manifest)
    if manifest["plan_fingerprint"] != recomputed:
        _reject("EM-008", f"reported {manifest['plan_fingerprint']!r} != recomputed {recomputed!r}")

    fingerprint = manifest["requirement_fingerprint"]
    if (
        not isinstance(fingerprint, str)
        or not fingerprint.startswith("sha256:")
        or len(fingerprint) != 71
        or not set(fingerprint[7:]) <= _HEX
    ):
        _reject("EM-009", f"requirement_fingerprint {fingerprint!r}")

    coverage = manifest["coverage"]
    _require_exact_keys("EM-010", "coverage", coverage, COVERAGE_KEYS)
    for key in ("obligations_total", "obligations_active", "obligations_deferred"):
        if not isinstance(coverage[key], int) or coverage[key] < 0:
            _reject("EM-010", f"coverage.{key}")
    if coverage["obligations_uncovered"] != []:
        _reject("EM-010", "coverage.obligations_uncovered must be empty")

    total = sum(len(pack["obligations"]) for pack in packs)
    active = sum(
        1 for pack in packs for ob in pack["obligations"] if ob["disposition"] == "active"
    )
    if coverage["obligations_total"] != total:
        _reject("EM-010", f"obligations_total {coverage['obligations_total']} != {total}")
    if coverage["obligations_active"] != active:
        _reject("EM-010", f"obligations_active {coverage['obligations_active']} != {active}")
    if coverage["obligations_deferred"] != total - active:
        _reject("EM-010", f"obligations_deferred != {total - active}")

    seen_paths: dict[str, str] = {}
    for pack in packs:
        for path in pack["files_writable"]:
            if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
                _reject("EM-011", f"pack {pack['id']}: invalid writable path {path!r}")
            if path in seen_paths:
                _reject("EM-011", f"path {path!r} claimed by {seen_paths[path]} and {pack['id']}")
            seen_paths[path] = pack["id"]

    generated_by = manifest["generated_by"]
    if not isinstance(generated_by, str) or "@" not in generated_by:
        _reject("EM-012", f"generated_by {generated_by!r}")
    if validator_version is not None:
        _, _, version = generated_by.partition("@")
        if version != validator_version:
            _reject("EM-012", f"generated_by version {version!r} != package {validator_version!r}")

    deferred_pairs = {
        (pack_id, task)
        for item in deferred_items
        for pack_id in item["affected_packs"]
        for task in item["affected_tasks"]
    }
    for pack_id, task, disposition in all_claims:
        if disposition == "deferred" and (pack_id, task) not in deferred_pairs:
            _reject("EM-013", f"deferred claim ({pack_id}, {task}) has no deferred[] entry")


def active_obligations(manifest: dict[str, Any], pack_id: str) -> list[str]:
    """``A(pack, contract_hash)`` - ids only, sorted for a stable contract."""
    pack = next(p for p in manifest["packs"] if p["id"] == pack_id)
    return sorted(ob["id"] for ob in pack["obligations"] if ob["disposition"] == "active")


def deferred_obligations(manifest: dict[str, Any], pack_id: str) -> list[str]:
    pack = next(p for p in manifest["packs"] if p["id"] == pack_id)
    return sorted(ob["id"] for ob in pack["obligations"] if ob["disposition"] == "deferred")


def deferred_projection(manifest: dict[str, Any], pack_id: str) -> dict[str, list[dict[str, str]]]:
    """Every deferral record behind each deferred obligation, listed not merged.

    Merging would hide that two different owners deferred the same obligation
    for different reasons, and the release condition is per record.
    """
    pack = next(p for p in manifest["packs"] if p["id"] == pack_id)
    out: dict[str, list[dict[str, str]]] = {}
    for ob in pack["obligations"]:
        if ob["disposition"] != "deferred":
            continue
        tasks = {c["task"] for c in ob["claimed_by"] if c["disposition"] == "deferred"}
        records = [
            {"owner": item["owner"], "release_condition": item["release_condition"], "ref": item["ref"]}
            for item in manifest["deferred"]
            if pack_id in item["affected_packs"] and tasks & set(item["affected_tasks"])
        ]
        out[ob["id"]] = records
    return out


def approved_checks(manifest: dict[str, Any], pack_id: str) -> list[dict[str, Any]]:
    """Derive one obligation_selector check per obligation that names a selector."""
    pack = next(p for p in manifest["packs"] if p["id"] == pack_id)
    return [
        {
            "check_id": ob["id"],
            "kind": "obligation_selector",
            "result_kind": "test",
            "selector": ob["selector"],
        }
        for ob in sorted(pack["obligations"], key=lambda o: o["id"])
        if ob["selector"] is not None
    ]
