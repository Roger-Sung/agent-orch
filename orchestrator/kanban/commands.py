"""Manual card commands and CAS (spec S3-S4, S6 approval freeze, S8 Done gate).

This is T1 of the T1.8 task graph: **metadata only**.  Nothing here submits a
task, claims a night, reads a quota snapshot or starts a provider.  A command
arrives as a CLI-written inbox request, the daemon hands it to
:func:`handle_request`, and the whole effect is one row in ``kanban_cards``
plus one row in ``kanban_events``.

Decisions worth stating, because they are the ones a later slice must not
quietly undo:

* **The event is the record, the card is the projection.**  Every command -
  accepted or rejected - appends exactly one ``kanban_events`` row inside the
  same transaction as the card update.  A rejected command records its reason
  and leaves the revision alone (S3).  Card history is therefore complete even
  though T1 never creates a task, which is what AC02 asks for.
* **``operation_id`` is the idempotency key and it is the events primary key.**
  A resend of the same operation with the same payload hash replays the
  recorded result and writes nothing.  A resend with a *different* payload is
  ``idempotency_conflict`` - and cannot itself be stored, because the primary
  key is already taken by the original.  That is the append-only contract
  working as intended, not a gap: the original event still says what happened.
* **``revision`` is compare-and-swap, not a lock.**  Every mutating command
  carries ``expected_revision`` and the UPDATE carries ``AND revision=?``.  A
  zero row count is ``revision_conflict``.  Two concurrent manual events
  therefore have exactly one winner without any new lease table (S2 forbids
  one).
* **Only ``approve`` writes an approval.**  ``create`` and ``edit`` refuse any
  ``approval_*``/``task_id``/``request_id``/``revision``/``manual_state`` key,
  so ``ready`` is unreachable except through an operator approve command.
  Approval wording inside a spec, a note or any other agent-written artifact is
  content, never a command: the spec marker is a *precondition* of approve, not
  a substitute for it (S4).
* **Reserved framing is refused, not stripped.**  Card text and every approved
  source is checked for the engine's own writer-owned markers.  Silently
  removing them would change the bytes an operator approved; accepting them
  would let task content look like engine control framing (S6).
* **Done validates artifacts, not path strings.**  The gate decision, its
  lifecycle YAMLs and the gate review input are parsed and hashed on disk, and
  the closeout binding block must appear in ``routing.task_summary``.  T1
  supplies the night-side values from a fixture binding file because no night
  row exists yet; T8 replaces that source with committed rows.  The *checks*
  are the deliverable, the fixture is only where their inputs come from.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ..execution import PLAN_BEGIN, PLAN_END
from ..profile import ProfileError, canonical_json, load_profile
from ..runner import (
    CONVERGENCE_BEGIN,
    CONVERGENCE_END,
    ENVELOPE_BEGIN,
    ENVELOPE_END,
)

#: The inbox request ``action`` this module owns.
KANBAN_ACTION = "kanban"

# SQLite INTEGER is signed 64-bit; Python/JSON integers are unbounded.
MAX_REVISION = (1 << 63) - 1


def _valid_revision(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_REVISION

#: Bumped only when the frozen approval payload's shape changes; an old
#: ``approval_hash`` must stay recomputable from the payload that produced it.
APPROVAL_SCHEMA_VERSION = 1

COMMANDS = (
    "create",
    "edit",
    "approve",
    "withdraw",
    "return",
    "pause",
    "done",
    "archive",
    "quota-snapshot",
    "quota-invalidate",
    "report-progress",
    "place",
)

PROGRESS_STATUSES = ("not_started", "in_progress", "blocked", "needs_decision", "reported_done")

PRIORITIES = ("high", "normal", "low")

#: Writer-owned framing the engine reserves for itself.  Task content that
#: merely embeds one of these is refused outright (S6).
RESERVED_MARKERS = (
    ENVELOPE_BEGIN,
    ENVELOPE_END,
    PLAN_BEGIN,
    PLAN_END,
    CONVERGENCE_BEGIN,
    CONVERGENCE_END,
)

#: S3: a card's risk map must answer every reference dimension.  A missing key
#: is not "low risk", it is an unanswered question, so approve refuses it.
REQUIRED_RISK_KEYS = (
    "money",
    "durable_state",
    "transaction",
    "mq",
    "concurrency",
    "migration",
    "cross_module_contract",
    "security",
    "irreversible_side_effect",
)

#: A true answer on any of these is what makes a human closeout stop-gate
#: mandatory for Done.  Frozen into the approval payload so Done reads it from
#: committed evidence instead of from the Done command (S8).
GATE_FORCING_RISK_KEYS = (
    "money",
    "durable_state",
    "transaction",
    "migration",
    "security",
    "irreversible_side_effect",
)

#: S4: changing any of these invalidates the approval that was granted over the
#: previous values.  ``title``/``note``/``priority`` are deliberately absent -
#: they are display text and are never executor input.
SCOPE_FIELDS = (
    "repo_path",
    "worktree_path",
    "git_common_dir",
    "change_name",
    "spec_path",
    "acceptance_path",
    "spec_review_pointer",
    "spec_review_hash",
    "base_head",
    "candidate_fingerprint",
    "risk",
    "estimate_by_pool",
    "allowed_commands",
    "profile_name",
    "provider",
    "model",
    "effort",
    "routing_digest",
    "config_digest",
)
DESCRIPTIVE_FIELDS = ("title", "note", "priority")
EDITABLE_FIELDS = SCOPE_FIELDS + DESCRIPTIVE_FIELDS

#: Fields whose value is a JSON object in the request and a JSON text column in
#: the card row.
JSON_OBJECT_FIELDS = ("risk", "estimate_by_pool")
JSON_ARRAY_FIELDS = ("allowed_commands",)
JSON_FIELDS = JSON_OBJECT_FIELDS + JSON_ARRAY_FIELDS

#: Everything approve needs resolved before it can freeze anything.
REQUIRED_APPROVAL_FIELDS = (
    "repo_path",
    "worktree_path",
    "git_common_dir",
    "change_name",
    "spec_path",
    "acceptance_path",
    "spec_review_pointer",
    "spec_review_hash",
    "base_head",
    "candidate_fingerprint",
    "risk",
    "estimate_by_pool",
    "allowed_commands",
    "profile_name",
    "provider",
    "model",
    "effort",
    "routing_digest",
    "config_digest",
)

#: S6 / native approved-spec: an explicit marker line, not a word in prose.
APPROVAL_MARKER_KEYS = ("status", "approval", "decision")
APPROVAL_MARKER_VALUES = ("approved", "ready", "accepted", "final")

#: Manual states from which a command may still change the card.
TERMINAL_STATES = ("done", "archived")

#: The closeout binding block (S8 P0-C).  The order is fixed because the block
#: is compared as a byte substring of ``routing.task_summary``; a reordered
#: block is a different block.
BINDING_BEGIN = "<!-- kanban-closeout-binding:v1 -->"
BINDING_END = "<!-- /kanban-closeout-binding -->"
BINDING_BLOCK_FIELDS = (
    "night_task_id",
    "night_id",
    "card_id",
    "approval_generation",
    "approval_hash",
    "input_hash",
    "profile_hash",
    "final_candidate_fingerprint",
    "review_seal_hash",
    "audit_seal_hash",
    "apply_report_path",
    "apply_report_hash",
    "review_report_path",
    "review_report_hash",
    "audit_report_path",
    "audit_report_hash",
)
#: Carried by the binding but outside the canonical block: the block's field
#: list is fixed by S8 and these are inputs to checks, not part of the pointer
#: set a reviewer reads.
BINDING_EXTRA_FIELDS = (
    "night_request_id",
    "closeout_task_id",
    "terminal_done_at",
    "last_seal_at",
    "remaining_evidence",
)
BINDING_REPORTS = (
    ("apply_report_path", "apply_report_hash"),
    ("review_report_path", "review_report_hash"),
    ("audit_report_path", "audit_report_hash"),
)


class KanbanError(RuntimeError):
    """A request the handler cannot even classify as a card command.

    Distinct from a *rejected* command: a rejection is a recorded event with a
    reason, this is a malformed request that never names a command.
    """


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload_hash(command: str, payload: dict[str, Any]) -> str:
    """The idempotency fingerprint of a command.

    The command name is inside the hash: the same operation id resent as a
    different command with an identical payload is a different command, and
    must conflict rather than replay.  ``canonical_json`` is the engine's
    existing byte-exact encoder, so a payload rebuilt in another process
    hashes identically.
    """
    return hashlib.sha256(canonical_json({"command": command, "payload": payload})).hexdigest()


def approval_hash(payload: dict[str, Any]) -> str:
    """``SHA256(canonical_json(approval_payload))`` (S6).

    The hash is never a member of the payload it covers, so it stays
    recomputable from the payload alone.
    """
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def reserved_marker_in(text: str) -> str | None:
    for marker in RESERVED_MARKERS:
        if marker in text:
            return marker
    return None


def spec_is_explicitly_approved(text: str) -> bool:
    """An explicit ``Status: approved`` style marker line.

    Mirrors the native approved-spec rule: a marker word inside prose does not
    count, the line must be a key/value of its own.
    """
    for line in text.splitlines():
        stripped = line.strip().lstrip("-*# ").strip()
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        if key.strip().lower() not in APPROVAL_MARKER_KEYS:
            continue
        if value.strip().lower() in APPROVAL_MARKER_VALUES:
            return True
    return False


def render_closeout_binding(binding: dict[str, Any]) -> str:
    """The canonical binding block an operator pastes into the closeout description.

    T7 generates this from committed rows; it lives here because the Done
    handler must compare against exactly the bytes the generator produces.
    """
    lines = [BINDING_BEGIN]
    for field in BINDING_BLOCK_FIELDS:
        lines.append(f"{field}: {binding[field]}")
    lines.append(BINDING_END)
    return "\n".join(lines)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def build_request(
    command: str,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """The inbox request a CLI writes for one card command.

    ``operation_id`` defaults to ``request_id`` so the ordinary single-shot CLI
    call needs no extra flag, while a deliberate resend can reuse one
    operation id under a fresh request id.
    """
    if command not in COMMANDS:
        raise KanbanError(f"unsupported kanban command: {command!r}")
    request_id = request_id or str(uuid.uuid4())
    return {
        "request_id": request_id,
        "action": KANBAN_ACTION,
        "command": command,
        "operation_id": operation_id or request_id,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


def handle_request(controller: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Execute one card command against ``controller``'s database.

    ``controller`` is the daemon's single writer; only ``.conn`` and ``.home``
    are used, so nothing here can start a task or touch a lease.
    """
    command = request.get("command")
    if command not in COMMANDS:
        raise KanbanError(f"unsupported kanban command: {command!r}")
    operation_id = request.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise KanbanError("kanban request requires an operation_id")
    try:
        if str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError
    except ValueError as exc:
        raise KanbanError(f"invalid operation_id: {operation_id!r}") from exc
    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise KanbanError("kanban request requires a payload object")
    actor = payload.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        raise KanbanError("kanban request requires an actor")

    digest = payload_hash(command, payload)
    conn: sqlite3.Connection = controller.conn
    home: Path = Path(controller.home)

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM kanban_events WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if existing is not None:
            outcome = _replay(existing, digest)
            conn.execute("COMMIT")
            return outcome
        outcome = _dispatch(conn, home, command, operation_id, digest, payload)
        conn.execute("COMMIT")
        return outcome
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _replay(existing: sqlite3.Row, digest: str) -> dict[str, Any]:
    """The recorded result of an operation id that has already been used."""
    if existing["payload_hash"] != digest:
        # Deliberately not recorded: the primary key already belongs to the
        # original command, and overwriting it would destroy the evidence that
        # makes this conflict detectable at all.
        return {
            "command": existing["kind"],
            "operation_id": existing["operation_id"],
            "card_id": existing["card_id"],
            "result": "rejected",
            "reason": "idempotency_conflict",
            "revision": None,
            "recorded": False,
            "replayed": False,
        }
    return {
        "command": existing["kind"],
        "operation_id": existing["operation_id"],
        "card_id": existing["card_id"],
        "result": existing["result"],
        "reason": existing["reason"],
        "revision": existing["result_revision"],
        "recorded": True,
        "replayed": True,
    }


def _dispatch(
    conn: sqlite3.Connection,
    home: Path,
    command: str,
    operation_id: str,
    digest: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if command in {"quota-snapshot", "quota-invalidate"}:
        from .quota import invalidate_command, snapshot_command

        handler = {
            "quota-snapshot": snapshot_command,
            "quota-invalidate": invalidate_command,
        }[command]
        return handler(conn, operation_id, digest, payload)
    handler = {
        "create": _create,
        "edit": _edit,
        "approve": _approve,
        "withdraw": _withdraw,
        "return": _return,
        "pause": _pause,
        "done": _done,
        "archive": _archive,
        "report-progress": _report_progress,
        "place": _place,
    }[command]
    return handler(conn, home, command, operation_id, digest, payload)


# ---------------------------------------------------------------------------
# Event and card writes
# ---------------------------------------------------------------------------


def _reject(
    conn: sqlite3.Connection,
    command: str,
    operation_id: str,
    digest: str,
    payload: dict[str, Any],
    reason: str,
    *,
    card_id: str | None,
    card_exists: bool,
) -> dict[str, Any]:
    """Record the refusal and leave the card exactly as it was (S3)."""
    expected_revision = payload.get("expected_revision")
    if not _valid_revision(expected_revision):
        expected_revision = None
    conn.execute(
        "INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,task_id,"
        "expected_revision,result_revision,actor,at,result,reason,payload,metadata_delta)"
        " VALUES(?,?,?,?,NULL,?,NULL,?,?, 'rejected',?,?,NULL)",
        (
            operation_id,
            digest,
            command,
            card_id if card_exists else None,
            expected_revision,
            payload["actor"],
            _now_ms(),
            reason,
            json.dumps(_public_payload(payload), ensure_ascii=False, sort_keys=True),
        ),
    )
    return {
        "command": command,
        "operation_id": operation_id,
        "card_id": card_id,
        "result": "rejected",
        "reason": reason,
        "revision": None,
        "recorded": True,
        "replayed": False,
    }


def _accept(
    conn: sqlite3.Connection,
    command: str,
    operation_id: str,
    digest: str,
    payload: dict[str, Any],
    *,
    card_id: str,
    result_revision: int,
    event_payload: str | None = None,
    metadata_delta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conn.execute(
        "INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,task_id,"
        "expected_revision,result_revision,actor,at,result,reason,payload,metadata_delta)"
        " VALUES(?,?,?,?,NULL,?,?,?,?, 'accepted',NULL,?,?)",
        (
            operation_id,
            digest,
            command,
            card_id,
            payload.get("expected_revision"),
            result_revision,
            payload["actor"],
            _now_ms(),
            event_payload
            if event_payload is not None
            else json.dumps(_public_payload(payload), ensure_ascii=False, sort_keys=True),
            json.dumps(metadata_delta, ensure_ascii=False, sort_keys=True)
            if metadata_delta is not None
            else None,
        ),
    )
    return {
        "command": command,
        "operation_id": operation_id,
        "card_id": card_id,
        "result": "accepted",
        "reason": None,
        "revision": result_revision,
        "recorded": True,
        "replayed": False,
    }


def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload as recorded on the event.

    Identical to the submitted payload today; the indirection exists so a later
    slice can drop a field from the record without changing the idempotency
    hash, which must keep covering the submitted bytes.
    """
    return payload


def _card(conn: sqlite3.Connection, card_id: Any) -> sqlite3.Row | None:
    if not isinstance(card_id, str) or not card_id:
        return None
    return conn.execute(
        "SELECT * FROM kanban_cards WHERE card_id=?", (card_id,)
    ).fetchone()


def _cas_update(
    conn: sqlite3.Connection,
    card_id: str,
    expected_revision: int,
    assignments: dict[str, Any],
) -> bool:
    """One compare-and-swap card update; False means someone else moved first."""
    assignments = dict(assignments)
    assignments["revision"] = expected_revision + 1
    assignments["updated_at"] = _now_ms()
    columns = ", ".join(f"{name}=?" for name in assignments)
    cursor = conn.execute(
        f"UPDATE kanban_cards SET {columns} WHERE card_id=? AND revision=?",
        (*assignments.values(), card_id, expected_revision),
    )
    return cursor.rowcount == 1


def _guard(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    allow_terminal: bool = False,
    allow_claimed: bool = False,
) -> tuple[sqlite3.Row | None, str | None]:
    """The checks every mutating command shares: card, revision, terminality."""
    card_id = payload.get("card_id")
    card = _card(conn, card_id)
    if card is None:
        return None, "card_not_found"
    expected = payload.get("expected_revision")
    if not isinstance(expected, int) or isinstance(expected, bool):
        return card, "expected_revision_required"
    if not _valid_revision(expected):
        return card, "invalid_expected_revision"
    if expected != card["revision"]:
        return card, "revision_conflict"
    if expected == MAX_REVISION:
        return card, "revision_exhausted"
    if not allow_terminal and card["manual_state"] in TERMINAL_STATES:
        return card, "card_is_terminal"
    if card["task_id"] is not None and not allow_claimed:
        # T1 never binds a task, so this is unreachable today.  It is here
        # because the first slice that *can* bind one must replace this
        # conservative predicate with the S4 active/unknown-writer predicate
        # and boundary-intent handling.  A non-null historical task_id alone
        # is not a permanent blocker; T5/T6 own that transition.  Until then,
        # fail closed rather than silently edit a possibly active card.
        return card, "card_is_claimed"
    return card, None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def progress_scope_digest(card: Any) -> str:
    """Pure snapshot binding; no artifact reads or execution evidence claim."""
    card = dict(card)
    fields = SCOPE_FIELDS + ("approval_generation", "approval_hash", "task_id")
    return hashlib.sha256(canonical_json({key: card.get(key) for key in fields})).hexdigest()


def _report_progress(conn, home, command, operation_id, digest, payload):
    # Reporting is event metadata, safe even for linked/terminal cards; it is
    # never authority to change their lifecycle or their execution records.
    card, reason = _guard(conn, payload, allow_terminal=True, allow_claimed=True)
    card_id = payload.get("card_id")
    reject = lambda reason: _reject(
        conn, command, operation_id, digest, payload, reason,
        card_id=card_id if isinstance(card_id, str) else None, card_exists=card is not None,
    )
    if reason is not None:
        return reject(reason)
    allowed = {"card_id", "expected_revision", "actor", "report_status", "summary",
               "blocker", "decision", "next_step", "source_refs"}
    if set(payload) - allowed:
        return reject("unsupported_progress_fields")
    if payload.get("report_status") not in PROGRESS_STATUSES:
        return reject("invalid_report_status")
    if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
        return reject("summary_required")
    for key in ("summary", "blocker", "decision", "next_step"):
        text = payload.get(key)
        if text is not None and (not isinstance(text, str) or len(text) > 4000):
            return reject("invalid_progress_text")
    refs = payload.get("source_refs", [])
    if (not isinstance(refs, list) or len(refs) > 20 or any(
        not isinstance(ref, str) or not ref.strip() or len(ref) > 1000 for ref in refs
    )):
        return reject("invalid_source_refs")
    texts = [payload.get(key) for key in ("summary", "blocker", "decision", "next_step", "actor")] + refs
    if any(reserved_marker_in(text) for text in texts if isinstance(text, str)):
        return reject("unsupported_input_framing")
    if len(payload["actor"]) > 500:
        return reject("invalid_progress_actor")
    event = {**payload, "source_refs": refs, "progress_binding": {
        "schema_version": 1,
        "approval_generation": card["approval_generation"],
        "scope_digest": progress_scope_digest(card),
    }}
    expected = payload["expected_revision"]
    if not _cas_update(conn, card["card_id"], expected, {}):
        return reject("revision_conflict")
    return _accept(conn, command, operation_id, digest, payload,
                   card_id=card["card_id"], result_revision=expected + 1,
                   event_payload=json.dumps(event, ensure_ascii=False, sort_keys=True),
                   metadata_delta={"progress_reported": True, "execution_verified": False})


def _place(conn, home, command, operation_id, digest, payload):
    """Placement is reversible display metadata; never change lifecycle/binding."""
    card, reason = _guard(conn, payload, allow_terminal=True, allow_claimed=True)
    reject = lambda why: _reject(conn, command, operation_id, digest, payload,
                                 card_id=payload.get("card_id"), card_exists=card is not None, reason=why)
    if reason:
        return reject(reason)
    if card["manual_state"] == "archived":
        return reject("card_is_archived")
    if set(payload) != {"card_id", "expected_revision", "actor", "destination", "user_request"}:
        return reject("invalid_place_payload")
    if payload.get("destination") not in {"board", "backlog"}:
        return reject("invalid_destination")
    request = payload.get("user_request")
    if (not isinstance(request, str) or not request.strip() or len(request) > 4000 or
        reserved_marker_in(request) or len(payload["actor"]) > 500 or reserved_marker_in(payload["actor"])):
        return reject("explicit_user_request_required")
    if payload["destination"] == "backlog":
        from .view import project
        from .read import COLUMNS
        data = {"cards": [dict(card)], "quota": []}
        for key, table in (("events", "kanban_events"), ("nights", "kanban_nights")):
            data[key] = [dict(row) for row in conn.execute("SELECT " + ','.join(COLUMNS[key]) + " FROM " + table + " WHERE card_id=?", (card["card_id"],))]
        data["tasks"] = [dict(row) for row in conn.execute("SELECT " + ','.join(COLUMNS['tasks']) + " FROM tasks WHERE id=?", (card["task_id"],))] if card["task_id"] else []
        from .read import _summaries
        summaries = _summaries(conn, [dict(card)])
        data.update(queue_locations=summaries["queue_locations"], summary_flags=summaries["summary_flags"])
        if project(data)[0]["group"] != "待處理":
            return reject("backlog_requires_effective_pending")
    expected = payload["expected_revision"]
    if not _cas_update(conn, card["card_id"], expected, {}):
        return reject("revision_conflict")
    return _accept(conn, command, operation_id, digest, payload,
                   card_id=card["card_id"], result_revision=expected + 1,
                   metadata_delta={"queue_location": payload["destination"]})


def _create(conn, home, command, operation_id, digest, payload):
    card_id = payload.get("card_id")
    existing = _card(conn, card_id)
    reject = lambda reason: _reject(  # noqa: E731 - one-line local alias
        conn, command, operation_id, digest, payload, reason,
        card_id=card_id if isinstance(card_id, str) else None,
        card_exists=existing is not None,
    )
    if not isinstance(card_id, str) or not card_id:
        return reject("card_id_required")
    if existing is not None:
        return reject("card_exists")
    if "expected_revision" in payload and not _valid_revision(payload["expected_revision"]):
        return reject("invalid_expected_revision")
    fields, reason = _read_fields(payload.get("fields"), allow=EDITABLE_FIELDS)
    if reason is not None:
        return reject(reason)
    title = fields.get("title")
    if not isinstance(title, str) or not title.strip():
        return reject("title_required")
    priority = fields.get("priority", "normal")
    if priority not in PRIORITIES:
        return reject("invalid_priority")
    fields["priority"] = priority
    marker = _marker_in_fields(fields)
    if marker is not None:
        return reject("unsupported_input_framing")

    now = _now_ms()
    columns = {"card_id": card_id, "revision": 0, "manual_state": "inbox",
               "created_at": now, "updated_at": now, **_storable(fields)}
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO kanban_cards({names}) VALUES({marks})", tuple(columns.values())
    )
    return _accept(
        conn, command, operation_id, digest, payload,
        card_id=card_id, result_revision=0,
        metadata_delta={"manual_state": "inbox", **_storable(fields), "queue_location": "backlog"},
    )


def _edit(conn, home, command, operation_id, digest, payload):
    card, reason = _guard(conn, payload)
    card_id = payload.get("card_id")
    reject = lambda r: _reject(  # noqa: E731
        conn, command, operation_id, digest, payload, r,
        card_id=card_id if isinstance(card_id, str) else None, card_exists=card is not None,
    )
    if reason is not None:
        return reject(reason)
    fields, reason = _read_fields(payload.get("fields"), allow=EDITABLE_FIELDS)
    if reason is not None:
        return reject(reason)
    if not fields:
        return reject("no_fields")
    if "title" in fields and (not isinstance(fields["title"], str) or not fields["title"].strip()):
        return reject("title_required")
    if "priority" in fields and fields["priority"] not in PRIORITIES:
        return reject("invalid_priority")
    if _marker_in_fields(fields) is not None:
        return reject("unsupported_input_framing")

    assignments = _storable(fields)
    delta = dict(assignments)
    scope_touched = sorted(set(fields) & set(SCOPE_FIELDS))
    if scope_touched and card["approval_hash"] is not None:
        # S4: scope-bearing edits invalidate the approval that covered the old
        # values.  The generation stays, so the next approve is a new one.
        assignments.update(
            {
                "approval_hash": None,
                "approval_actor": None,
                "approval_at": None,
                "approval_event_id": None,
                "spec_hash": None,
                "acceptance_hash": None,
                "profile_hash": None,
            }
        )
        delta["approval_revoked"] = True
    if scope_touched and card["manual_state"] == "ready":
        assignments["manual_state"] = "inbox"
        delta["manual_state"] = "inbox"
    if scope_touched:
        assignments["last_reason"] = "scope_edited"

    expected = payload["expected_revision"]
    if not _cas_update(conn, card["card_id"], expected, assignments):
        return reject("revision_conflict")
    delta["scope_fields_changed"] = scope_touched
    return _accept(
        conn, command, operation_id, digest, payload,
        card_id=card["card_id"], result_revision=expected + 1, metadata_delta=delta,
    )


def _approve(conn, home, command, operation_id, digest, payload):
    card, reason = _guard(conn, payload)
    card_id = payload.get("card_id")
    reject = lambda r: _reject(  # noqa: E731
        conn, command, operation_id, digest, payload, r,
        card_id=card_id if isinstance(card_id, str) else None, card_exists=card is not None,
    )
    if reason is not None:
        return reject(reason)
    if card["manual_state"] == "ready" and card["approval_hash"] is not None:
        return reject("already_approved")

    frozen, reason = build_approval_payload(card)
    if reason is not None:
        return reject(reason)
    digest_approval = approval_hash(frozen)

    expected = payload["expected_revision"]
    event_payload = canonical_json(frozen).decode("utf-8")
    # The event carries the immutable payload and must exist before the card
    # can point at it (S3: cards.approval_event_id references it).
    result = _accept(
        conn, command, operation_id, digest, payload,
        card_id=card["card_id"], result_revision=expected + 1,
        event_payload=event_payload,
        metadata_delta={
            "manual_state": "ready",
            "approval_generation": frozen["approval_generation"],
            "approval_hash": digest_approval,
            "gate_required": frozen["gate_required"],
        },
    )
    if not _cas_update(
        conn,
        card["card_id"],
        expected,
        {
            "manual_state": "ready",
            "approval_generation": frozen["approval_generation"],
            "approval_actor": payload["actor"],
            "approval_at": _now_ms(),
            "approval_hash": digest_approval,
            "approval_event_id": operation_id,
            "spec_hash": frozen["spec_hash"],
            "acceptance_hash": frozen["acceptance_hash"],
            "profile_hash": frozen["profile_hash"],
            "last_reason": None,
        },
    ):
        # Unreachable: BEGIN IMMEDIATE holds the write lock and _guard already
        # compared this revision, so nothing can have moved in between.  Raise
        # rather than record a second event, because the accepted one is
        # already written and the whole transaction must roll back.
        raise KanbanError(
            f"kanban approve lost a CAS that cannot race: card={card['card_id']}"
        )
    result["approval_hash"] = digest_approval
    result["approval_generation"] = frozen["approval_generation"]
    result["gate_required"] = frozen["gate_required"]
    return result


def build_approval_payload(card: sqlite3.Row) -> tuple[dict[str, Any], str | None]:
    """Freeze everything an execution of this card would be judged against (S6).

    Returns ``(payload, None)`` or ``({}, reason)``.  The payload deliberately
    excludes ``title``/``note``/``priority`` and its own hash, and it carries
    the *content* of the approved sources, not only their paths, so a later
    edit of the file on disk cannot change what was approved.
    """
    missing = [name for name in REQUIRED_APPROVAL_FIELDS if card[name] in (None, "")]
    if missing:
        return {}, f"missing_scope:{missing[0]}"

    for name in ("repo_path", "worktree_path", "git_common_dir"):
        if not Path(card[name]).is_dir():
            return {}, f"missing_directory:{name}"

    sources: dict[str, tuple[str, str]] = {}
    for name in ("spec_path", "acceptance_path", "spec_review_pointer"):
        path = Path(card[name])
        if not path.is_file():
            return {}, f"missing_artifact:{name}"
        text = path.read_text(encoding="utf-8")
        if reserved_marker_in(text) is not None:
            return {}, "unsupported_input_framing"
        sources[name] = (text, sha256_text(text))

    spec_text, spec_hash = sources["spec_path"]
    acceptance_text, acceptance_hash = sources["acceptance_path"]
    review_text, review_hash = sources["spec_review_pointer"]
    profile_path = Path(card["profile_name"])
    if not profile_path.is_file():
        return {}, "missing_artifact:profile_name"
    profile_text = profile_path.read_text(encoding="utf-8")
    if reserved_marker_in(profile_text) is not None:
        return {}, "unsupported_input_framing"
    try:
        profile = load_profile(profile_path)
    except ProfileError:
        return {}, "invalid_profile"
    profile_snapshot = profile.to_dict()
    profile_bytes = canonical_json(profile_snapshot)
    profile_hash = hashlib.sha256(profile_bytes).hexdigest()

    if not spec_is_explicitly_approved(spec_text):
        return {}, "spec_not_approved"
    if not acceptance_text.strip():
        return {}, "acceptance_empty"
    if not review_text.strip():
        return {}, "spec_review_empty"
    if card["spec_review_hash"] != review_hash:
        return {}, "spec_review_hash_mismatch"

    try:
        risk = json.loads(card["risk"])
        estimate = json.loads(card["estimate_by_pool"])
        allowed_commands = json.loads(card["allowed_commands"])
    except (TypeError, json.JSONDecodeError):
        return {}, "invalid_json_field"
    if (
        not isinstance(risk, dict)
        or not isinstance(estimate, dict)
        or not isinstance(allowed_commands, list)
    ):
        return {}, "invalid_json_field"
    if any(key not in risk for key in REQUIRED_RISK_KEYS) or len(risk) != len(REQUIRED_RISK_KEYS):
        return {}, "incomplete_risk"
    if any(not isinstance(value, bool) for value in risk.values()):
        return {}, "incomplete_risk"
    from .quota import valid_pool_key

    if not estimate or any(
        not valid_pool_key(key)
        or not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        for key, value in estimate.items()
    ):
        return {}, "invalid_estimate"
    if not allowed_commands or any(
        not isinstance(value, str) or not value.strip()
        for value in allowed_commands
    ):
        return {}, "invalid_allowed_commands"
    if any(reserved_marker_in(value) is not None for value in allowed_commands):
        return {}, "unsupported_input_framing"
    if card["effort"] not in ("low", "medium", "high"):
        return {}, "invalid_effort"
    if not card["routing_digest"] or not card["config_digest"]:
        return {}, "missing_routing_config_digest"

    payload = {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "card_id": card["card_id"],
        "approval_generation": card["approval_generation"] + 1,
        "repo_path": str(Path(card["repo_path"]).resolve()),
        "worktree_path": str(Path(card["worktree_path"]).resolve()),
        "git_common_dir": str(Path(card["git_common_dir"]).resolve()),
        "base_head": card["base_head"],
        "candidate_fingerprint": card["candidate_fingerprint"],
        "change_name": card["change_name"],
        "spec_path": str(Path(card["spec_path"]).resolve()),
        "spec_text": spec_text,
        "spec_hash": spec_hash,
        "acceptance_path": str(Path(card["acceptance_path"]).resolve()),
        "acceptance_text": acceptance_text,
        "acceptance_hash": acceptance_hash,
        "spec_review_pointer": str(Path(card["spec_review_pointer"]).resolve()),
        "spec_review_hash": review_hash,
        "risk": risk,
        "estimate_by_pool": estimate,
        "allowed_commands": allowed_commands,
        "profile_path": str(profile_path.resolve()),
        "profile_snapshot": profile_snapshot,
        "profile_hash": profile_hash,
        "provider": card["provider"],
        "model": card["model"],
        "effort": card["effort"],
        "routing_digest": card["routing_digest"],
        "config_digest": card["config_digest"],
        # S9 is a property of the approved work contract, so it is frozen with
        # it rather than re-decided at dispatch time.
        "allowed_write_roots": [str(Path(card["worktree_path"]).resolve())],
        "forbidden_side_effects": [
            "commit", "push", "merge", "deploy", "destructive_cleanup",
        ],
        "interpretation_envelope": False,
        "execution_plan": False,
        "gate_required": any(bool(risk[key]) for key in GATE_FORCING_RISK_KEYS),
    }
    return payload, None


def _withdraw(conn, home, command, operation_id, digest, payload):
    return _invalidate(
        conn, command, operation_id, digest, payload,
        target_state="inbox", require_approval=True, reason_code="approval_withdrawn",
    )


def _return(conn, home, command, operation_id, digest, payload):
    return _invalidate(
        conn, command, operation_id, digest, payload,
        target_state="returned", require_approval=False, reason_code="manually_returned",
    )


def _invalidate(
    conn, command, operation_id, digest, payload, *, target_state, require_approval, reason_code
):
    card, reason = _guard(conn, payload)
    card_id = payload.get("card_id")
    reject = lambda r: _reject(  # noqa: E731
        conn, command, operation_id, digest, payload, r,
        card_id=card_id if isinstance(card_id, str) else None, card_exists=card is not None,
    )
    if reason is not None:
        return reject(reason)
    if require_approval and card["approval_hash"] is None:
        return reject("not_approved")

    expected = payload["expected_revision"]
    if not _cas_update(
        conn,
        card["card_id"],
        expected,
        {
            "manual_state": target_state,
            # The generation is kept: history and any later night must still
            # see which approval world this card came from (S3).
            "approval_actor": None,
            "approval_at": None,
            "approval_hash": None,
            "approval_event_id": None,
            "spec_hash": None,
            "acceptance_hash": None,
            "profile_hash": None,
            "last_reason": reason_code,
        },
    ):
        return reject("revision_conflict")
    return _accept(
        conn, command, operation_id, digest, payload,
        card_id=card["card_id"], result_revision=expected + 1,
        metadata_delta={
            "manual_state": target_state,
            "approval_revoked": card["approval_hash"] is not None,
            "last_reason": reason_code,
        },
    )


def _archive(conn, home, command, operation_id, digest, payload):
    card, reason = _guard(conn, payload)
    card_id = payload.get("card_id")
    reject = lambda r: _reject(  # noqa: E731
        conn, command, operation_id, digest, payload, r,
        card_id=card_id if isinstance(card_id, str) else None, card_exists=card is not None,
    )
    if reason is not None:
        return reject(reason)
    expected = payload["expected_revision"]
    if not _cas_update(
        conn, card["card_id"], expected,
        {"manual_state": "archived", "last_reason": "manually_archived"},
    ):
        return reject("revision_conflict")
    # S4: Archive is not a success claim; it says only that nobody is working
    # on this card any more.
    return _accept(
        conn, command, operation_id, digest, payload,
        card_id=card["card_id"], result_revision=expected + 1,
        metadata_delta={"manual_state": "archived", "success_claimed": False},
    )


def _pause(conn, home, command, operation_id, digest, payload):
    """Record the intent to stop at the next stage boundary (S4).

    T1 has no scheduler and no running stage, so a pause can never be
    *consumed* here.  It is accepted and recorded as pending rather than
    applied, because reporting anything else would claim a writer had stopped.
    The boundary intent lives in the event, which T5 reads at a stage boundary.
    """
    card, reason = _guard(conn, payload)
    card_id = payload.get("card_id")
    reject = lambda r: _reject(  # noqa: E731
        conn, command, operation_id, digest, payload, r,
        card_id=card_id if isinstance(card_id, str) else None, card_exists=card is not None,
    )
    if reason is not None:
        return reject(reason)
    expected = payload["expected_revision"]
    if not _cas_update(
        conn, card["card_id"], expected,
        {"last_reason": "manual_pause_pending"},
    ):
        return reject("revision_conflict")
    result = _accept(
        conn, command, operation_id, digest, payload,
        card_id=card["card_id"], result_revision=expected + 1,
        metadata_delta={
            "pending": True,
            "applies_at": "next_stage_boundary",
            "manual_state": card["manual_state"],
            "last_reason": "manual_pause_pending",
        },
    )
    result["pending"] = True
    result["applies_at"] = "next_stage_boundary"
    result["manual_state"] = card["manual_state"]
    return result


# ---------------------------------------------------------------------------
# Done (S8, including the P0-C closeout gate binding)
# ---------------------------------------------------------------------------


def _done(conn, home, command, operation_id, digest, payload):
    card, reason = _guard(conn, payload)
    card_id = payload.get("card_id")
    reject = lambda r: _reject(  # noqa: E731
        conn, command, operation_id, digest, payload, r,
        card_id=card_id if isinstance(card_id, str) else None, card_exists=card is not None,
    )
    if reason is not None:
        return reject(reason)

    reason = _validate_done(conn, home, card, payload)
    if reason is not None:
        return reject(reason)

    binding = payload["binding"]
    expected = payload["expected_revision"]
    # The event is written first: the card's last_evidence_event_id is a
    # foreign key into it, so the evidence pointer cannot exist before the
    # evidence does.
    result = _accept(
        conn, command, operation_id, digest, payload,
        card_id=card["card_id"], result_revision=expected + 1,
        metadata_delta={
            "manual_state": "done",
            "binding": binding,
            "gate_decision_path": payload.get("gate_decision_path"),
            "gate_decision_hash": payload.get("gate_decision_hash"),
            "deployed": False,
        },
    )
    if not _cas_update(
        conn, card["card_id"], expected,
        {"manual_state": "done", "last_reason": "manually_accepted",
         "last_evidence_event_id": operation_id},
    ):
        # Unreachable under BEGIN IMMEDIATE; see _approve.
        raise KanbanError(f"kanban done lost a CAS that cannot race: card={card['card_id']}")
    return result


def _validate_done(
    conn: sqlite3.Connection, home: Path, card: sqlite3.Row, payload: dict[str, Any]
) -> str | None:
    """Every Done predicate this slice can check, in refusal-priority order.

    Returns a reason code, or None when Done may proceed.
    """
    binding = payload.get("binding")
    if not isinstance(binding, dict):
        return "binding_required"
    for field in (*BINDING_BLOCK_FIELDS, *BINDING_EXTRA_FIELDS):
        if field not in binding:
            return "binding_incomplete"

    # The card side of the binding comes from committed rows, never from the
    # command: a Done that names a different card or a stale approval is not a
    # naming mistake, it is the forgery this check exists for.
    if binding["card_id"] != card["card_id"]:
        return "binding_mismatch"
    if card["approval_generation"] == 0 or card["approval_event_id"] is None:
        return "card_never_approved"
    if binding["approval_generation"] != card["approval_generation"]:
        return "binding_mismatch"
    if binding["approval_hash"] != card["approval_hash"]:
        return "binding_mismatch"

    supplied = payload.get("final_candidate_fingerprint")
    if not isinstance(supplied, str) or not supplied:
        return "final_candidate_missing"
    if not binding["final_candidate_fingerprint"]:
        return "final_candidate_missing"
    if supplied != binding["final_candidate_fingerprint"]:
        return "final_candidate_mismatch"

    for field in ("review_seal_hash", "audit_seal_hash", "input_hash", "profile_hash"):
        if not binding[field]:
            return "seal_missing"
    if binding["profile_hash"] != card["profile_hash"]:
        return "binding_mismatch"

    for path_field, hash_field in BINDING_REPORTS:
        report = Path(str(binding[path_field]))
        if not binding[hash_field]:
            return "report_hash_missing"
        if not report.is_file():
            return "report_missing"
        if sha256_file(report) != binding[hash_field]:
            return "report_hash_mismatch"

    remaining = binding["remaining_evidence"]
    if not isinstance(remaining, list):
        return "binding_incomplete"
    if remaining:
        return "remaining_evidence_incomplete"

    approval = _approval_payload(conn, card)
    if approval is None:
        return "card_never_approved"
    if not approval.get("gate_required"):
        return None
    return _validate_gate(conn, home, card, payload, binding)


def _approval_payload(conn: sqlite3.Connection, card: sqlite3.Row) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload FROM kanban_events WHERE operation_id=? AND kind='approve'"
        " AND result='accepted'",
        (card["approval_event_id"],),
    ).fetchone()
    if row is None or not row["payload"]:
        return None
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _validate_gate(
    conn: sqlite3.Connection,
    home: Path,
    card: sqlite3.Row,
    payload: dict[str, Any],
    binding: dict[str, Any],
) -> str | None:
    """The S8 / P0-C closeout stop-gate chain, read-only and on-disk."""
    from ..start import _read_yaml  # local: start.py is heavy and unrelated here

    raw_path = payload.get("gate_decision_path")
    supplied_hash = payload.get("gate_decision_hash")
    if not isinstance(raw_path, str) or not raw_path:
        return "gate_decision_missing"
    decision_path = Path(raw_path)
    if not decision_path.is_file():
        return "gate_decision_missing"
    if not isinstance(supplied_hash, str) or not supplied_hash:
        return "gate_decision_hash_missing"
    if sha256_file(decision_path) != supplied_hash:
        return "gate_decision_hash_mismatch"

    try:
        decision = _read_yaml(decision_path)
    except (OSError, ValueError):
        return "gate_decision_invalid"
    if not isinstance(decision, dict) or decision.get("type") != "stop_gate_decision":
        return "gate_decision_invalid"
    closeout_task_id = decision.get("task_id")
    if not isinstance(closeout_task_id, str) or not closeout_task_id:
        return "gate_decision_invalid"
    if decision_path.resolve() != (
        home / "tasks" / f"{closeout_task_id}-gate-decision.yaml"
    ).resolve():
        return "gate_binding_mismatch"
    if decision.get("decision") != "ALLOW":
        return "gate_not_decided"

    tasks_dir = home / "tasks"
    task_path = tasks_dir / f"{closeout_task_id}.yaml"
    routing_path = tasks_dir / f"{closeout_task_id}-routing.yaml"
    review_input_path = tasks_dir / f"{closeout_task_id}-gate-review-input.md"
    if not task_path.is_file() or not routing_path.is_file():
        return "gate_binding_mismatch"
    try:
        task_record = _read_yaml(task_path)
        routing = _read_yaml(routing_path)
    except (OSError, ValueError):
        return "gate_binding_mismatch"
    if not isinstance(task_record, dict) or not isinstance(routing, dict):
        return "gate_binding_mismatch"
    if (
        task_record.get("task_id") != closeout_task_id
        or routing.get("task_id") != closeout_task_id
    ):
        return "gate_binding_mismatch"

    # Order matters: a lifecycle that was never routed as a stop gate gets the
    # route reason, not the not-decided one (S8 item 2 before item 4).
    if routing.get("stop_gate") is not True:
        return "gate_not_required_route"
    task_gate = task_record.get("gate")
    routing_gate = routing.get("gate")
    if not isinstance(task_gate, dict) or not isinstance(routing_gate, dict):
        return "gate_binding_mismatch"
    if canonical_json(task_gate) != canonical_json(routing_gate):
        return "gate_binding_mismatch"
    gate = routing_gate
    if gate.get("type") != "stop_gate":
        return "gate_binding_mismatch"
    if gate.get("status") != "decided":
        return "gate_not_decided"
    if (
        gate.get("decision") != "ALLOW"
        or gate.get("stage") != "done"
        or task_record.get("stage") != "done"
    ):
        return "gate_not_decided"
    task_decision = task_record.get("gate_decision")
    routing_decision = routing.get("gate_decision")
    if not isinstance(task_decision, dict) or not isinstance(routing_decision, dict):
        return "gate_binding_mismatch"
    if canonical_json(task_decision) != canonical_json(routing_decision):
        return "gate_binding_mismatch"
    gate_decision = routing_decision
    if canonical_json(gate_decision) != canonical_json(decision):
        return "gate_binding_mismatch"
    if gate_decision.get("final_stage") != "done":
        return "gate_not_decided"
    recorded_decision_path = gate_decision.get("decision_artifact_path")
    if not isinstance(recorded_decision_path, str) or (
        Path(recorded_decision_path).resolve() != decision_path.resolve()
    ):
        return "gate_binding_mismatch"

    task_execution = task_record.get("execution_result")
    routing_execution = routing.get("execution_result")
    if not isinstance(task_execution, dict) or not isinstance(routing_execution, dict):
        return "gate_binding_mismatch"
    if canonical_json(task_execution) != canonical_json(routing_execution):
        return "gate_binding_mismatch"
    if (
        routing_execution.get("controller_lifecycle_stage") != "done"
        or routing_execution.get("controller_status") != "done"
    ):
        return "gate_binding_mismatch"
    # Before an idempotent start-sync after gate-allow, the last execution
    # summary still describes the pending stop gate.  start-sync deliberately
    # rewrites that projection to the decided final stage afterwards.  Both
    # are native, valid representations of the same immutable ALLOW artifact.
    gate_required = routing_execution.get("gate_required")
    lifecycle_stage = routing_execution.get("lifecycle_stage")
    if gate_required is True:
        if lifecycle_stage != "waiting_user":
            return "gate_binding_mismatch"
    elif gate_required is False:
        if lifecycle_stage != gate.get("stage"):
            return "gate_binding_mismatch"
    else:
        return "gate_binding_mismatch"
    execution = routing.get("execution")
    if not isinstance(execution, dict):
        return "gate_binding_mismatch"
    provenance = {
        "request_id": routing_execution.get("request_id"),
        "processed_result_path": routing_execution.get("processed_result_path"),
        "controller_task_id": routing_execution.get("controller_task_id"),
        "controller_status": routing_execution.get("controller_status"),
        "controller_lifecycle_stage": routing_execution.get("controller_lifecycle_stage"),
        "pattern": routing.get("pattern"),
        "profile": execution.get("profile"),
        "executor": routing.get("executor"),
        "reviewer": routing.get("reviewer"),
        "route_source": routing.get("route_source"),
    }
    if any(decision.get(key) != value for key, value in provenance.items()):
        return "gate_binding_mismatch"

    # P0-C: the description is the only canonical carrier.  `--scope` never
    # reaches routing.task_summary, so a binding pasted only there fails here.
    if not review_input_path.is_file():
        return "gate_binding_mismatch"
    summary = _embedded_task_summary(review_input_path.read_text(encoding="utf-8"))
    if summary is None:
        return "gate_binding_mismatch"
    if render_closeout_binding(binding) not in summary:
        return "gate_binding_mismatch"

    decided_at = _parse_time(decision.get("decided_at"))
    terminal_done_at = _parse_time(binding.get("terminal_done_at"))
    last_seal_at = _parse_time(binding.get("last_seal_at"))
    if decided_at is None or terminal_done_at is None or last_seal_at is None:
        return "gate_stale_candidate"
    if decided_at <= terminal_done_at or decided_at <= last_seal_at:
        return "gate_stale_candidate"

    if closeout_task_id != binding.get("closeout_task_id"):
        return "gate_binding_mismatch"
    if closeout_task_id in (binding["night_task_id"], binding["night_request_id"]):
        return "gate_binding_mismatch"
    if _closeout_already_used(conn, closeout_task_id):
        return "closeout_task_id_reused"
    return None


def _embedded_task_summary(text: str) -> str | None:
    """``routing.task_summary`` out of a gate review input's embedded routing JSON."""
    marker = "\n## Routing Decision\n"
    index = text.find(marker)
    if index < 0:
        return None
    rest = text[index + len(marker):]
    start = rest.find("```json\n")
    if start < 0:
        return None
    end = rest.find("\n```", start)
    if end < 0:
        return None
    try:
        routing = json.loads(rest[start + len("```json\n"):end])
    except json.JSONDecodeError:
        return None
    summary = routing.get("task_summary") if isinstance(routing, dict) else None
    return summary if isinstance(summary, str) else None


def _closeout_already_used(conn: sqlite3.Connection, closeout_task_id: str) -> bool:
    """S8 item 6: one closeout lifecycle belongs to one card and generation."""
    for row in conn.execute(
        "SELECT metadata_delta FROM kanban_events WHERE kind='done' AND result='accepted'"
    ):
        try:
            delta = json.loads(row["metadata_delta"] or "{}")
        except json.JSONDecodeError:
            continue
        binding = delta.get("binding")
        if not isinstance(binding, dict):
            continue
        if binding.get("closeout_task_id") == closeout_task_id:
            return True
    return False


# ---------------------------------------------------------------------------
# Field plumbing
# ---------------------------------------------------------------------------


def _read_fields(
    raw: Any, *, allow: tuple[str, ...]
) -> tuple[dict[str, Any], str | None]:
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "invalid_fields"
    for name in raw:
        if name not in allow:
            # Approval, task binding, revision and manual state are engine-owned.
            # Refusing them here is what makes `ready` unreachable without an
            # operator approve command (S4).
            return {}, "unsupported_field"
    for name, value in raw.items():
        if name in JSON_OBJECT_FIELDS:
            if not isinstance(value, dict):
                return {}, "invalid_fields"
        elif name in JSON_ARRAY_FIELDS:
            if not isinstance(value, list):
                return {}, "invalid_fields"
        elif value is not None and not isinstance(value, str):
            return {}, "invalid_fields"
    return dict(raw), None


def _storable(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if name in JSON_FIELDS:
            out[name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            out[name] = value
    return out


def _marker_in_fields(fields: dict[str, Any]) -> str | None:
    for name, value in fields.items():
        if isinstance(value, str) and reserved_marker_in(value) is not None:
            return name
        if name in JSON_FIELDS and reserved_marker_in(
            json.dumps(value, ensure_ascii=False)
        ) is not None:
            return name
    return None
