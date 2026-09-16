"""Contract field ownership and re-version classification (IDENTITIES §3).

Every contract change lands in exactly one of three author classes plus a set
of refresh kinds, and that pair decides four downstream things: who has to
authorise it, whether the version and revision move, whether the work attempt
survives, and whether existing observations may still be used.

The asymmetry is deliberate.  ``minor`` is the only class that needs no human,
so its seven conditions are conjunctive and checked against a *projection* of
the contract rather than the whole thing - anything not explicitly listed as
author-side cannot buy its way into a minor.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..profile import canonical_json

# §3.1 ownership.  Scope keys are immutable inside a contract series: a change
# there is a different pack, not a transition.
SCOPE_KEYS = ("policy_version", "target_id", "change", "pack")

P_CONTRACT_FIELDS = (
    "approved_checks", "prerun_checks", "prerun_reads", "readable_roots",
    "hotspot_paths", "declared_config_refs", "excludes", "protected_source_globs",
    "home_seeds", "budget_policy", "execution_policy", "prompt_templates",
)

REFRESH_BASE_FIELDS = ("base_revision", "candidate_fingerprint_input")
REFRESH_DEPENDENCY_FIELDS = ("dependency_revisions", "evidence_receipts")
REFRESH_ENVIRONMENT_FIELDS = ("environment_digest",)

# Observation survival per §3.2, from most to least permissive.  Composition
# takes the strictest, which is why this is an ordered ranking and not a set.
OBSERVATION_POLICY_ORDER = ("reuse", "verify_stale", "all_stale")


class Classification:
    """The outcome of classifying one contract transition."""

    def __init__(
        self,
        author_class: str,
        refresh: set[str],
        *,
        reasons: Sequence[str] = (),
        minor_blockers: Sequence[str] = (),
    ) -> None:
        self.author_class = author_class
        self.refresh = set(refresh)
        self.reasons = list(reasons)
        self.minor_blockers = list(minor_blockers)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Classification({self.author_class!r}, refresh={sorted(self.refresh)!r})"

    @property
    def requires_major_approval(self) -> bool:
        return self.author_class == "major"

    @property
    def requires_environment_ack(self) -> bool:
        return "environment" in self.refresh

    @property
    def new_work_attempt(self) -> bool:
        """Only a base refresh rebuilds the attempt; everything else continues."""
        return "base" in self.refresh

    @property
    def superseded_allowed(self) -> bool:
        """`superseded_by_contract` needs a major; dependency refresh forbids it.

        The forbid only bites when the author side did not change - a major
        already carries its own permission (§3.2 composition).
        """
        if self.author_class == "major":
            return True
        return "dependency" not in self.refresh

    @property
    def observation_policy(self) -> str:
        policies = {"reuse"}
        if self.author_class == "major":
            policies.add("all_stale")
        if "base" in self.refresh or "environment" in self.refresh:
            policies.add("all_stale")
        if "dependency" in self.refresh:
            policies.add("verify_stale")
        return max(policies, key=OBSERVATION_POLICY_ORDER.index)

    def version_delta(self, *, pack_subset_changed: bool) -> tuple[int, int]:
        """``(version_delta, revision_delta)`` - refresh never moves either."""
        if self.author_class == "none":
            return 0, 0
        if self.author_class == "minor":
            return 1, 1
        return (1 if pack_subset_changed else 0), 1


def _equal(old: Any, new: Any) -> bool:
    return canonical_json({"v": old}) == canonical_json({"v": new})


def _projection(contract: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    from .identities import project_secret_refs

    out: dict[str, Any] = {}
    for field in fields:
        value = contract.get(field)
        if field == "execution_policy" and isinstance(value, dict):
            value = {
                role: ({**spec, "env": project_secret_refs(spec["env"])} if isinstance(spec, dict) and "env" in spec else spec)
                for role, spec in value.items()
            }
        out[field] = value
    return out


def refresh_kinds(old: dict[str, Any], new: dict[str, Any]) -> set[str]:
    kinds: set[str] = set()
    if any(not _equal(old.get(f), new.get(f)) for f in REFRESH_BASE_FIELDS):
        kinds.add("base")
    if any(not _equal(old.get(f), new.get(f)) for f in REFRESH_DEPENDENCY_FIELDS):
        kinds.add("dependency")
    if any(not _equal(old.get(f), new.get(f)) for f in REFRESH_ENVIRONMENT_FIELDS):
        kinds.add("environment")
    return kinds


def minor_conditions(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    old_manifest: dict[str, Any],
    new_manifest: dict[str, Any],
    pack_id: str,
    text_changed_tasks: list[str] | None,
    claimed_paths: set[str] = frozenset(),
    active_writer_paths: set[str] = frozenset(),
) -> list[str]:
    """Return the blockers; an empty list means all seven conditions hold.

    ``text_changed_tasks is None`` means the validator did not report the field.
    That is a blocker, not a pass: the engine does not parse the target's
    markdown, so without the list it cannot know whether prose changed.
    """
    blockers: list[str] = []

    # (1) requirement fingerprint unchanged.
    if not _equal(old.get("requirement_fingerprint"), new.get("requirement_fingerprint")):
        blockers.append("requirement_fingerprint changed")

    old_pack = next((p for p in old_manifest.get("packs", []) if p["id"] == pack_id), None)
    new_pack = next((p for p in new_manifest.get("packs", []) if p["id"] == pack_id), None)
    if old_pack is None or new_pack is None:
        blockers.append("pack missing from a manifest side")
        return blockers

    # (2) P_pack equal, ignoring files_writable and the derived contract_version.
    def pack_projection(pack: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in pack.items() if k not in {"files_writable", "contract_version"}}

    if not _equal(pack_projection(old_pack), pack_projection(new_pack)):
        blockers.append("P_pack changed")

    # (3) contract_version +1 exactly.
    if new_pack.get("contract_version") != old_pack.get("contract_version", 0) + 1:
        blockers.append("contract_version is not from+1")

    # (4) files_writable strictly grows, with no competing claim or hotspot.
    old_paths = set(old_pack.get("files_writable", []))
    new_paths = set(new_pack.get("files_writable", []))
    if not old_paths < new_paths:
        blockers.append("files_writable is not a strict superset")
    else:
        added = new_paths - old_paths
        if added & claimed_paths:
            blockers.append(f"added paths already claimed: {sorted(added & claimed_paths)}")
        if added & active_writer_paths:
            blockers.append(f"added paths have an active writer: {sorted(added & active_writer_paths)}")
        from .identities import glob_match

        hotspots = new.get("hotspot_paths", []) or []
        hit = sorted(p for p in added if any(glob_match(h, p) for h in hotspots))
        if hit:
            blockers.append(f"added paths match hotspot_paths: {hit}")

    # (5) P_contract equal.
    if not _equal(_projection(old, P_CONTRACT_FIELDS), _projection(new, P_CONTRACT_FIELDS)):
        blockers.append("P_contract changed")

    # (6) every other pack byte-equal, files_writable included.
    def others(manifest: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in p.items() if k != "contract_version"}
            for p in manifest.get("packs", [])
            if p["id"] != pack_id
        ]

    if not _equal(others(old_manifest), others(new_manifest)):
        blockers.append("another pack changed")

    # (7) top-level deferred / evidence_declared unchanged.
    if not _equal(old_manifest.get("deferred"), new_manifest.get("deferred")):
        blockers.append("manifest deferred changed")
    if not _equal(old_manifest.get("evidence_declared"), new_manifest.get("evidence_declared")):
        blockers.append("manifest evidence_declared changed")

    # P_source must be identical - a source file that changed shape without
    # moving the requirement fingerprint is still an author-side change.
    if not _equal(
        old_manifest.get("source_fingerprints", {}).get("design_md"),
        new_manifest.get("source_fingerprints", {}).get("design_md"),
    ) or not _equal(
        old_manifest.get("source_fingerprints", {}).get("test_spec"),
        new_manifest.get("source_fingerprints", {}).get("test_spec"),
    ):
        blockers.append("P_source changed")

    # (c) tasks prose.
    if text_changed_tasks is None:
        blockers.append("validator did not report text_changed_tasks")
    elif text_changed_tasks:
        blockers.append(f"tasks with changed prose: {sorted(text_changed_tasks)}")

    return blockers


def classify(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    old_manifest: dict[str, Any],
    new_manifest: dict[str, Any],
    pack_id: str,
    text_changed_tasks: list[str] | None = None,
    claimed_paths: set[str] = frozenset(),
    active_writer_paths: set[str] = frozenset(),
) -> Classification:
    """Classify one transition into ``author_class`` plus a refresh set."""
    refresh = refresh_kinds(old, new)

    author_fields_changed = (
        not _equal(_projection(old, P_CONTRACT_FIELDS), _projection(new, P_CONTRACT_FIELDS))
        or not _equal(
            [p for p in old_manifest.get("packs", [])],
            [p for p in new_manifest.get("packs", [])],
        )
        or not _equal(old_manifest.get("deferred"), new_manifest.get("deferred"))
        or not _equal(old_manifest.get("evidence_declared"), new_manifest.get("evidence_declared"))
        or not _equal(old_manifest.get("source_fingerprints"), new_manifest.get("source_fingerprints"))
        or not _equal(old.get("requirement_fingerprint"), new.get("requirement_fingerprint"))
    )

    if not author_fields_changed:
        return Classification("none", refresh)

    blockers = minor_conditions(
        old, new,
        old_manifest=old_manifest,
        new_manifest=new_manifest,
        pack_id=pack_id,
        text_changed_tasks=text_changed_tasks,
        claimed_paths=set(claimed_paths),
        active_writer_paths=set(active_writer_paths),
    )
    if blockers:
        return Classification("major", refresh, minor_blockers=blockers)
    return Classification("minor", refresh)


def transition_record(
    *,
    transition_id: str,
    old: dict[str, Any],
    new: dict[str, Any],
    classification: Classification,
    from_version: int,
    from_revision: int,
    pack_subset_changed: bool,
    reverify: Sequence[str],
    changed_or_removed: Sequence[str],
) -> dict[str, Any]:
    """Build the append-only transition record (IDENTITIES §3.3).

    ``reverify`` / ``changed_or_removed`` live here rather than in the contract:
    the contract is a closed set, and adding fields to it would introduce inputs
    that no hash covers.
    """
    version_delta, revision_delta = classification.version_delta(
        pack_subset_changed=pack_subset_changed
    )
    return {
        "transition_id": transition_id,
        "from_hash": old.get("contract_hash"),
        "to_hash": new.get("contract_hash"),
        "from_version": from_version,
        "to_version": from_version + version_delta,
        "from_revision": from_revision,
        "to_revision": from_revision + revision_delta,
        "author_class": classification.author_class,
        "refresh": sorted(classification.refresh),
        "reverify": sorted(reverify),
        "changed_or_removed": sorted(changed_or_removed),
    }
