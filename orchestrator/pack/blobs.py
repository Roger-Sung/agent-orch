"""Content-addressed blob store and observation sealing (ENVELOPES §2.5).

Two things live here:

* ``BlobStore`` - bytes in, ``sha256`` out, immutable once written.  Candidate
  file contents and symlink link-text go in at freeze time (IDENTITIES §2.3) so
  a later ``read`` observation is served from the store rather than from a
  worktree that may have moved on.
* ``seal_observation`` - wraps acquired bytes in the ``{acquisition, content}``
  envelope the spec requires, writes it under the bundle directory, and derives
  the ``projection`` that EV-R rules are allowed to read.

The projection is the whole point of the wrapper: rules read *derived* fields,
and every source field comes from ``acquisition`` - what the engine recorded at
capture time - never from parsing ``content`` back out again.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..profile import canonical_json
from .errors import PackError

# Acquisition keys per observation kind (ENVELOPES §2.5).  The engine writes
# exactly these at capture time; a missing one is a bug in the caller, not a
# reviewer-visible condition, so it raises rather than producing a half record.
ACQUISITION_KEYS: dict[str, frozenset[str]] = {
    "verify": frozenset(
        {"kind", "candidate_fingerprint", "contract_hash", "operation_id", "invocation_id",
         "result_kind", "status", "subject", "produced_by"}
    ),
    "read": frozenset(
        {"kind", "candidate_fingerprint", "source_path", "byte_range", "truncated",
         "operation_id", "produced_by"}
    ),
    "prior_review": frozenset({"kind", "round", "review_sha256", "produced_by"}),
    "contract": frozenset({"kind", "contract_hash", "produced_by"}),
    "manifest_slice": frozenset(
        {"kind", "manifest_sha256", "pack", "contract_version", "produced_by"}
    ),
}

# Projection keys per kind (ENVELOPES §2.5).  Only these may be referenced by
# EV-R rules.
PROJECTION_KEYS: dict[str, tuple[str, ...]] = {
    "verify": ("result_kind", "status", "candidate_fingerprint", "operation_id",
               "invocation_id", "subject"),
    "read": ("path", "candidate_fingerprint", "truncated"),
    "prior_review": ("round", "review_sha256"),
    "contract": ("contract_hash",),
    "manifest_slice": ("manifest_sha256", "pack", "contract_version"),
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BlobStore:
    """Immutable content-addressed store rooted at ``root``.

    Writes are atomic and idempotent: the same bytes always land on the same
    path, and re-putting them is a no-op rather than a rewrite, so a crash
    between two puts can never leave a partially different blob behind.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest[2:]

    def put(self, data: bytes) -> str:
        digest = sha256_hex(data)
        target = self._path(digest)
        if target.exists():
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        return digest

    def has(self, digest: str) -> bool:
        return self._path(digest).exists()

    def get(self, digest: str) -> bytes:
        try:
            data = self._path(digest).read_bytes()
        except OSError as exc:
            raise PackError(f"blob {digest} unreadable: {exc}") from exc
        # The store is content-addressed, so a mismatch means the file was
        # tampered with or truncated underneath us - never silently serve it.
        actual = sha256_hex(data)
        if actual != digest:
            raise PackError(f"blob {digest} content mismatch (found {actual})")
        return data


def _projection(acquisition: dict[str, Any]) -> dict[str, Any]:
    kind = acquisition["kind"]
    if kind == "read":
        # `path` is the projection name for what acquisition calls source_path.
        source = dict(acquisition)
        source["path"] = source.get("source_path")
        return {key: source.get(key) for key in PROJECTION_KEYS[kind]}
    return {key: acquisition.get(key) for key in PROJECTION_KEYS[kind]}


def seal_observation(
    store: BlobStore,
    bundle_dir: Path,
    observation_id: str,
    acquisition: dict[str, Any],
    content: Any,
    *,
    usable: bool,
    subject: Any = None,
) -> dict[str, Any]:
    """Write one ``{acquisition, content}`` wrapper and return its observation.

    ``content`` is whatever the acquisition produced - a JSON value for verify
    envelopes and manifest slices, a string for a file read.  The returned dict
    is the observation as it appears in the bundle, with ``payload.sha256``
    covering the whole wrapper file (not just the content).
    """
    kind = acquisition.get("kind")
    expected = ACQUISITION_KEYS.get(kind)
    if expected is None:
        raise PackError(f"unknown observation kind {kind!r}")
    if set(acquisition) != expected:
        missing = sorted(expected - set(acquisition))
        extra = sorted(set(acquisition) - expected)
        raise PackError(f"acquisition for {kind} has missing={missing} extra={extra}")

    wrapper = canonical_json({"acquisition": acquisition, "content": content})
    digest = sha256_hex(wrapper)
    store.put(wrapper)

    locator = f"observations/{observation_id}.json"
    target = bundle_dir / locator
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(wrapper)

    return {
        "id": observation_id,
        "kind": kind,
        "subject": subject,
        "payload": {"locator": locator, "sha256": digest},
        "projection": _projection(acquisition),
        "usable": bool(usable),
        "produced_by": acquisition["produced_by"],
    }
