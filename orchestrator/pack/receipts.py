"""A provider call's sealed receipt - the only thing reconcile may act on.

§5's reconcile rule is that a dead operation whose sealed receipt is *verifiable*
gets its `E_*` replayed, and anything else stays unknown.  The engine had the
replay (`recovery_commit`) but not the verification: it took the caller's word
for the receipt, so a truncated, stale or mismatched one would have been sealed
as a genuine result - which is worse than staying unknown, because unknown is at
least visible.

"Verifiable" is four separate questions, and the codes below exist so a refusal
says which one failed rather than collapsing into "no usable receipt":

* the file is readable at all                       (RC-1)
* its bytes hash to what was recorded               (RC-2)
* it is a receipt, with exactly the fields one has  (RC-3)
* it belongs to *this* operation and call binding   (RC-4 / RC-5)

and the envelope inside it is legal for its stage   (RC-6), which the caller
supplies because only the caller knows which stage's rules apply.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..profile import canonical_json
from .blobs import sha256_hex
from .errors import PackError

RECEIPT_KEYS = frozenset({"policy_version", "op_id", "call_binding", "outcome", "envelope"})
POLICY_VERSION = "pack-v1"

UNREADABLE = "RC-1"
HASH_MISMATCH = "RC-2"
MALFORMED = "RC-3"
FOREIGN_OPERATION = "RC-4"
BINDING_MISMATCH = "RC-5"
ENVELOPE_ILLEGAL = "RC-6"


class ReceiptInvalid(PackError):
    """A sealed receipt that cannot be trusted; the operation stays unknown."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def seal(path: Path, *, op_id: str, call_binding: dict[str, Any], outcome: str,
         envelope: dict[str, Any]) -> str:
    """Write the receipt and return the hash that will be required to read it."""
    body = canonical_json({
        "policy_version": POLICY_VERSION,
        "op_id": op_id,
        "call_binding": call_binding,
        "outcome": outcome,
        "envelope": envelope,
    })
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return "sha256:" + sha256_hex(body)


def load(path: Path, *, expected_sha256: str, op_id: str,
         expected_binding: dict[str, Any] | None = None,
         validate: Callable[[dict[str, Any], str], None] | None = None) -> dict[str, Any]:
    """Return the receipt only if every check passes; otherwise raise.

    The hash is checked over the raw bytes before anything is parsed, so a file
    that was rewritten between sealing and reading cannot reach the parser at
    all.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ReceiptInvalid(UNREADABLE, str(exc)) from None

    actual = "sha256:" + sha256_hex(raw)
    if actual != expected_sha256:
        raise ReceiptInvalid(HASH_MISMATCH, f"sealed {expected_sha256}, found {actual}")

    try:
        receipt = json.loads(raw)
    except ValueError as exc:
        raise ReceiptInvalid(MALFORMED, f"not JSON: {exc}") from None
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        missing = sorted(RECEIPT_KEYS - set(receipt or {}))
        extra = sorted(set(receipt or {}) - RECEIPT_KEYS)
        raise ReceiptInvalid(MALFORMED, f"missing={missing} extra={extra}")
    if receipt["policy_version"] != POLICY_VERSION:
        raise ReceiptInvalid(MALFORMED, f"policy_version {receipt['policy_version']!r}")

    if receipt["op_id"] != op_id:
        # A receipt from a different operation hashes and parses perfectly well.
        raise ReceiptInvalid(FOREIGN_OPERATION, f"receipt is for {receipt['op_id']!r}")
    if expected_binding is not None and receipt["call_binding"] != expected_binding:
        raise ReceiptInvalid(
            BINDING_MISMATCH,
            f"receipt {receipt['call_binding']} != expected {expected_binding}")

    if validate is not None:
        try:
            validate(receipt["envelope"], receipt["outcome"])
        except PackError as exc:
            raise ReceiptInvalid(ENVELOPE_ILLEGAL, str(exc)) from None
    return receipt
