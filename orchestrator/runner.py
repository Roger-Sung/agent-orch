from __future__ import annotations

import codecs
import json
import os
import queue
import re
import selectors
import shlex
import signal
import shutil
import stat as stat_module
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .containment import ContainmentConfigError, ContainmentError, SandboxSetupError, prepare_sandbox


OUTCOME_RE = re.compile(r"^ORCHESTRATOR_OUTCOME:\s*([A-Za-z0-9_-]+)\s*$", re.MULTILINE)
RATE_LIMIT_SIGNATURES = (
    re.compile(r"\brate[_ -]?limit[_ -]?exceeded\b", re.IGNORECASE),
    re.compile(r"\byou(?:'|’)ve hit your (?:usage )?limit\b", re.IGNORECASE),
    re.compile(r"\busage limit (?:has been )?reached\b", re.IGNORECASE),
    re.compile(r"\btoo many requests\b", re.IGNORECASE),
    re.compile(r"\b(?:http(?: status)?\s*)?429\b.*\b(?:rate|quota|limit)\b", re.IGNORECASE),
)
SOCKET_SIGNATURES = (
    re.compile(r"\bFailedToOpenSocket\b", re.IGNORECASE),
    re.compile(r"\bConnectionRefused\b", re.IGNORECASE),
    re.compile(r"\bconnection refused\b", re.IGNORECASE),
    re.compile(r"\bECONNREFUSED\b", re.IGNORECASE),
    re.compile(r"\bfailed to connect\b", re.IGNORECASE),
)
LOCALHOST_SIGNATURE = re.compile(r"\b(localhost|127\.0\.0\.1|::1)\b", re.IGNORECASE)
DEFAULT_CODEX_SERVICE_TIERS = frozenset({"fast", "priority"})

# Identical to subprocess._communicate's read size, so the hand-rolled drain in
# SubprocessRunner._drain_pipe reads the provider pipe in exactly the chunks
# communicate() used to.
DRAIN_READ_BYTES = 32768
# Upper bound on one select() wait, so a silent child still wakes the drain.
# min(remaining, LIVE_POLL_SECONDS) is what stops it from extending the
# deadline the child runs under.
LIVE_POLL_SECONDS = 1.0

# Live stream: one run-local JSONL file beside the sealed stage log, evidence
# only. None of these has an environment override; an override would be a new
# operator knob with a new failure mode and nothing here needs one.
LIVE_SCHEMA_VERSION = 1
LIVE_MAX_BYTES = 8192              # whole-run footprint; never exceeded, never rewritten
LIVE_TERMINAL_RESERVE_BYTES = 512  # a stage_end line is under 200 bytes
LIVE_FRAGMENT_MAX_CHARS = 1024
LIVE_QUEUE_MAX_RECORDS = 256       # optional-record depth; the slot beyond it is the terminal slot
LIVE_CLOSE_JOIN_SECONDS = 1.0      # bounded, and taken only on a reaped-child path

# Worktree + git containment: pin the stage's working directory to a worktree and
# strip every push credential. This is enforcement, not an instruction in a
# prompt - an agent that decides to push has no credential and the hook rejects
# it anyway.
#
# This layer is not a sandbox on its own. Writes outside the workspace are
# prevented by L1 and detected by L2 (see containment.py); what remains
# unaddressed is process isolation and network egress - the agent still runs as
# the same UNIX user and can read whatever that user can read. See
# docs/threat-model.md for the layer boundaries and the residual risk.
CONTAINMENT_BLOCKED_ENV = frozenset(
    {
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
        "GIT_ASKPASS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_COUNT",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)
CONTAINMENT_PRE_PUSH_HOOK = """#!/bin/sh
echo "agent-orch containment: push is disabled for orchestrated tasks" >&2
exit 1
"""


# ---------------------------------------------------------------------------
# Interpretation Envelope — the task-level system contract.
#
# The envelope is a six-axis record resolved once at intake, carried inside the
# already-hashed execution input, and injected by the controller's single
# prompt-composition chokepoint. These primitives live here rather than in
# controller.py because all three consumers of the envelope — intake
# (start.py), the controller, and retained inspection — import this module and
# nothing here imports them, so one definition serves all of them without a
# cycle.
# ---------------------------------------------------------------------------

# Engine-reserved outcome name. A stage outcome spelled exactly HOLD_OUTCOME
# stops the task at waiting_user instead of continuing, so a spec can hand a
# genuinely irreducible decision back to the operator. The semantics live in
# the engine, not in the task's profile snapshot, which is why the name is
# reserved: see docs/decisions/propose-convergence-policy.md, "Engine reserved
# outcome names". A legacy task is unaffected bit-for-bit.
HOLD_OUTCOME = "needs_user_decision"
HOLD_STOP_REASON = "user_decision_required"

ENVELOPE_BEGIN = "<!--ORCH-ENVELOPE-BEGIN-->"
ENVELOPE_END = "<!--ORCH-ENVELOPE-END-->"
ENVELOPE_SCHEMA_VERSION = 1

#: The set-valued axes, in canonical order. Membership of the written path /
#: named behaviour / named item in the axis value is the containment test.
ENVELOPE_SET_AXES = (
    "semantic_change_surface",
    "task_owned_write_targets",
    "assurance_ceiling",
    "threat_model",
    "evidence_ceiling",
)
#: The one enum axis. `user_decision` is the only value the engine honours.
ENVELOPE_ENUM_AXIS = "scope_expansion_policy"
ENVELOPE_AXES = ENVELOPE_SET_AXES + (ENVELOPE_ENUM_AXIS,)
ENVELOPE_STATES = ("declared", "semantically_silent", "unresolved")
ENVELOPE_AXIS_KEYS = frozenset({"state", "value", "default_applied", "source"})
#: Provenance recorded against each member of a set-valued axis.
ENVELOPE_SOURCE_REQUIREMENT = "requirement_sources"
ENVELOPE_SOURCE_DEFAULT = "safe_default"
ENVELOPE_SOURCE_ENGINE = "engine_owned"
ENVELOPE_MEMBER_SOURCES = frozenset(
    {ENVELOPE_SOURCE_REQUIREMENT, ENVELOPE_SOURCE_DEFAULT, ENVELOPE_SOURCE_ENGINE}
)
#: The only scope_expansion_policy value the engine honours (spec section 1.3).
SCOPE_EXPANSION_USER_DECISION = "user_decision"


class EnvelopeError(ValueError):
    """Envelope framing or schema failure. Always fails closed: never legacy."""


def render_envelope_block(envelope: dict[str, Any]) -> str:
    """The canonical delimited block, markers on their own lines.

    Rendering validates first, so an envelope that could not be read back is
    never written in the first place.
    """
    validate_envelope(envelope)
    body = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True)
    return f"{ENVELOPE_BEGIN}\n{body}\n{ENVELOPE_END}"


def envelope_block_text(text: str) -> str | None:
    """The raw delimited block, or None for a legacy task.

    Fail-closed framing (E-20). Task input is untrusted and may contain either
    marker itself, so marker presence alone is never verification: exactly one
    begin and one end marker, each alone on its line, in that order, with the
    end marker in the writer-owned final position. Only the complete absence of
    both markers is legacy; every other shape raises.
    """
    lines = text.split("\n")
    begins = [index for index, line in enumerate(lines) if line.strip() == ENVELOPE_BEGIN]
    ends = [index for index, line in enumerate(lines) if line.strip() == ENVELOPE_END]
    loose_begin = text.count(ENVELOPE_BEGIN)
    loose_end = text.count(ENVELOPE_END)
    if not begins and not ends and not loose_begin and not loose_end:
        return None
    if loose_begin != 1 or loose_end != 1 or len(begins) != 1 or len(ends) != 1:
        raise EnvelopeError(
            f"envelope marker count is not exactly one pair "
            f"(begin={loose_begin}, end={loose_end})"
        )
    begin, end = begins[0], ends[0]
    if end <= begin:
        raise EnvelopeError("envelope end marker precedes its begin marker")
    if any(line.strip() for line in lines[end + 1:]):
        raise EnvelopeError("envelope block is not in the writer-owned final position")
    return "\n".join(lines[begin:end + 1])


def extract_envelope(text: str) -> dict[str, Any] | None:
    """The validated envelope carried by `text`, or None for a legacy task."""
    block = envelope_block_text(text)
    if block is None:
        return None
    body = "\n".join(block.split("\n")[1:-1])
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"envelope block is not valid JSON: {exc}") from exc
    return validate_envelope(payload)


def validate_envelope(payload: Any) -> dict[str, Any]:
    """Schema check for a rendered envelope. Returns it, or raises."""
    if not isinstance(payload, dict):
        raise EnvelopeError("envelope must be a JSON object")
    if payload.get("schema_version") != ENVELOPE_SCHEMA_VERSION:
        raise EnvelopeError(f"unsupported envelope schema_version: {payload.get('schema_version')!r}")
    expected = {"schema_version", *ENVELOPE_AXES}
    if set(payload) != expected:
        missing = sorted(expected - set(payload))
        extra = sorted(set(payload) - expected)
        raise EnvelopeError(f"envelope axes missing={missing} unexpected={extra}")
    for axis in ENVELOPE_AXES:
        _validate_axis(axis, payload[axis])
    return payload


def _validate_axis(axis: str, entry: Any) -> None:
    if not isinstance(entry, dict):
        raise EnvelopeError(f"envelope axis {axis} must be an object")
    if set(entry) != ENVELOPE_AXIS_KEYS:
        raise EnvelopeError(f"envelope axis {axis} keys must be exactly {sorted(ENVELOPE_AXIS_KEYS)}")
    state = entry["state"]
    if state not in ENVELOPE_STATES:
        raise EnvelopeError(f"envelope axis {axis} has unknown state {state!r}")
    if state == "unresolved":
        # An unresolved axis takes the intake stop; it is never emitted.
        raise EnvelopeError(f"envelope axis {axis} is unresolved and must not be emitted")
    default_applied = entry["default_applied"]
    if type(default_applied) is not bool:
        raise EnvelopeError(f"envelope axis {axis} default_applied must be a boolean")
    if default_applied and state != "semantically_silent":
        raise EnvelopeError(f"envelope axis {axis} applies a default without being semantically_silent")
    if state == "semantically_silent" and not default_applied:
        raise EnvelopeError(f"envelope axis {axis} is semantically_silent without default_applied")
    if axis == "task_owned_write_targets" and state != "declared":
        raise EnvelopeError("task_owned_write_targets has no default and must be declared")
    value = entry["value"]
    source = entry["source"]
    if axis == ENVELOPE_ENUM_AXIS:
        if value != SCOPE_EXPANSION_USER_DECISION:
            raise EnvelopeError(f"scope_expansion_policy must be {SCOPE_EXPANSION_USER_DECISION!r}")
        if source not in ENVELOPE_MEMBER_SOURCES:
            raise EnvelopeError(f"scope_expansion_policy has unknown source {source!r}")
        return
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise EnvelopeError(f"envelope axis {axis} value must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise EnvelopeError(f"envelope axis {axis} value repeats a member")
    if not isinstance(source, dict) or set(source) != set(value):
        raise EnvelopeError(f"envelope axis {axis} source must name exactly its value members")
    for member, provenance in source.items():
        if provenance not in ENVELOPE_MEMBER_SOURCES:
            raise EnvelopeError(f"envelope axis {axis} member {member!r} has unknown source {provenance!r}")
    if state == "declared" and all(
        provenance == ENVELOPE_SOURCE_DEFAULT for provenance in source.values()
    ) and source:
        raise EnvelopeError(f"envelope axis {axis} is declared but every member is a safe default")


def allowed_outcomes(stage_outcomes: Any, envelope_present: bool) -> list[str]:
    """The one derivation of allowed typed outcomes, shared by all consumers.

    Prompt footer, `classify_result` and `inspect_retained` used to each build
    this expression independently, which is how a footer, a classification and
    a retained candidate drift apart. For an envelope task the reserved hold
    outcome is allowed on every profile, including one that never declared it;
    for a legacy task the profile's own outcomes are returned in unchanged
    order, so the composed prompt bytes do not move.
    """
    names = list(stage_outcomes)
    if envelope_present and HOLD_OUTCOME not in names:
        names.append(HOLD_OUTCOME)
    return names


# ---------------------------------------------------------------------------
# Repeat-review convergence (spec section 1.7).
# ---------------------------------------------------------------------------

CONVERGENCE_BEGIN = "<!--ORCH-CONVERGENCE-BEGIN-->"
CONVERGENCE_END = "<!--ORCH-CONVERGENCE-END-->"
CONVERGENCE_FIRST_KEYS = frozenset({"live", "resolved"})
CONVERGENCE_REPEAT_KEYS = frozenset({"live", "resolved", "new", "repeated", "verdict"})
CONVERGENCE_VERDICTS = ("improved", "stalled", "oscillating")


class ConvergenceError(ValueError):
    """A convergence record that is missing, malformed or self-contradicting."""


def extract_convergence(output: str, *, repeat: bool | None = None) -> dict[str, Any]:
    """The convergence record a branching-stage run must carry in its output.

    `repeat` says which shape is required. `None` reads the record in whichever
    shape it was written in, which is what reading a *prior* run's record needs:
    whether that run was itself a repeat is not the current run's business.

    Raises rather than guessing: every failure here routes to the engine hold,
    so a prose, absent or self-describing record can never reach an ordinary
    profile outcome.
    """
    begins = output.count(CONVERGENCE_BEGIN)
    ends = output.count(CONVERGENCE_END)
    if begins == 0 and ends == 0:
        raise ConvergenceError("convergence record missing")
    if begins != 1 or ends != 1:
        raise ConvergenceError(f"convergence marker count is not exactly one pair (begin={begins}, end={ends})")
    start = output.index(CONVERGENCE_BEGIN) + len(CONVERGENCE_BEGIN)
    stop = output.index(CONVERGENCE_END)
    if stop < start:
        raise ConvergenceError("convergence end marker precedes its begin marker")
    try:
        record = json.loads(output[start:stop])
    except json.JSONDecodeError as exc:
        raise ConvergenceError(f"convergence record is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ConvergenceError("convergence record must be a JSON object")
    keys = set(record)
    if repeat is None:
        repeat = "verdict" in keys
    if repeat:
        if keys != CONVERGENCE_REPEAT_KEYS:
            raise ConvergenceError(
                f"repeat review convergence keys must be exactly {sorted(CONVERGENCE_REPEAT_KEYS)}, got {sorted(keys)}"
            )
        if record["verdict"] not in CONVERGENCE_VERDICTS:
            raise ConvergenceError(f"unknown convergence verdict {record['verdict']!r}")
        for name in ("live", "resolved", "new", "repeated"):
            _convergence_identities(record, name)
    else:
        if "verdict" in keys:
            raise ConvergenceError("a first branching run must not carry a verdict")
        if keys != CONVERGENCE_FIRST_KEYS:
            raise ConvergenceError(
                f"first branching run convergence keys must be exactly {sorted(CONVERGENCE_FIRST_KEYS)}, got {sorted(keys)}"
            )
        if _convergence_identities(record, "resolved"):
            raise ConvergenceError("a first branching run must record an empty resolved set")
        _convergence_identities(record, "live")
    return record


def _convergence_identities(record: dict[str, Any], name: str) -> set[str]:
    value = record.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ConvergenceError(f"convergence {name} must be a list of non-empty failure-scenario identities")
    return set(value)


def convergence_verdict(
    prior_live: set[str], historical_resolved: set[str], current: set[str]
) -> str:
    """The section 1.7 rule: total, mutually exclusive, evaluated in order.

    Total over every input including empty sets: an empty `prior_live` admits
    no strict subset, so the rule assigns `stalled` and the run fails closed to
    a user decision. That is the rule, not an exception.
    """
    if current & historical_resolved:
        return "oscillating"
    new = current - prior_live
    if current < prior_live and not new:
        return "improved"
    return "stalled"


def validate_convergence(
    record: dict[str, Any], prior_live: set[str], historical_resolved: set[str]
) -> str:
    """Validate a repeat review's declared partition and verdict; return the verdict.

    Runs before any verdict is accepted. Any inequality with the computed sets
    or the computed verdict is contradictory and raises.
    """
    current = set(record["live"])
    expected = {
        "resolved": prior_live - current,
        "repeated": prior_live & current,
        "new": current - prior_live,
    }
    for name, computed in expected.items():
        declared = set(record[name])
        if declared != computed:
            raise ConvergenceError(
                f"declared {name}={sorted(declared)} contradicts computed {name}={sorted(computed)}"
            )
    verdict = convergence_verdict(prior_live, historical_resolved, current)
    if record["verdict"] != verdict:
        raise ConvergenceError(
            f"declared verdict {record['verdict']!r} contradicts computed verdict {verdict!r}"
        )
    return verdict


def prepare_containment(workspace: Path, log_path: Path) -> dict[str, str]:
    """Prepare worktree + git containment for one stage run; return the child env.

    This function covers the git dimension only: work happens in the worktree,
    and a result cannot leave through git. Write confinement is L1 and L2 in
    containment.py; process and network isolation are not implemented at all.

    Push is blocked three ways, so that any one of them failing leaves two:
      1. no SSH agent or token in the environment (neither HTTPS nor SSH can
         find a credential)
      2. GIT_SSH_COMMAND / GIT_ASKPASS point at /usr/bin/false, so there is no
         interactive rescue
      3. a dedicated core.hooksPath whose pre-push rejects unconditionally

    Committing still works. The constraint is on letting a result leave the
    worktree through git, not on doing the job.
    """
    containment_root = log_path.with_suffix(".containment")
    hooks_dir = containment_root / "hooks"
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        pre_push = hooks_dir / "pre-push"
        pre_push.write_text(CONTAINMENT_PRE_PUSH_HOOK, encoding="utf-8")
        pre_push.chmod(0o755)
    except OSError as exc:
        # Same class as a failed sandbox profile write: the environment is
        # broken, not the policy. Raising the environment-specific error keeps
        # the stop reason pointing at the disk rather than at the settings.
        raise SandboxSetupError(f"cannot prepare containment artifacts: {exc}") from exc

    gitconfig = containment_root / "gitconfig"
    identity = _git_identity()
    try:
        (containment_root / "identity-source").write_text(identity["source"] + "\n", encoding="utf-8")
        gitconfig.write_text(
            "[user]\n"
            f"\tname = {identity['name']}\n"
            f"\temail = {identity['email']}\n"
            "[core]\n"
            f"\thooksPath = {hooks_dir}\n"
            "[credential]\n"
            "\thelper =\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise SandboxSetupError(f"cannot write containment gitconfig: {exc}") from exc

    env = {key: value for key, value in os.environ.items() if key not in CONTAINMENT_BLOCKED_ENV}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/usr/bin/false",
            "SSH_ASKPASS": "/usr/bin/false",
            "GIT_SSH_COMMAND": "/usr/bin/false",
            "ORCH_CONTAINMENT": "worktree+git",
            "ORCH_CONTAINMENT_WORKSPACE": str(workspace),
        }
    )
    return env


ALLOW_UNSANDBOXED_ENV = "ORCH_ALLOW_UNSANDBOXED"
ALLOW_UNATTENDED_ENV = "ORCH_ALLOW_UNATTENDED"

#: Provider CLI flags that disable the approval prompt, i.e. that hand a stage
#: the operator's authority with nobody watching. Known names only — see
#: `unattended_flags_in_use` for why this is a detector and not a guarantee.
UNATTENDED_FLAGS = (
    "--dangerously-skip-permissions",
    "--approve-for-me",
)
PROVIDER_COMMAND_ENV = ("ORCH_CLAUDE_COMMAND", "ORCH_CODEX_COMMAND")


def provider_command(owner: str) -> list[str]:
    """The configured CLI invocation for one provider owner.

    One derivation, so a stage run and the intake resolver cannot drift apart
    on which binary, model or flags the operator actually configured.
    """
    if owner == "claude":
        raw = os.environ.get("ORCH_CLAUDE_COMMAND", "claude -p")
    elif owner == "codex":
        raw = os.environ.get("ORCH_CODEX_COMMAND", "codex exec")
    else:
        raise ValueError(f"unsupported owner: {owner}")
    command = shlex.split(raw)
    if not command:
        raise ValueError(f"empty command for owner {owner}")
    return command


UNATTENDED_CONSENT_MESSAGE = """refusing to start.

The configured provider commands disable their approval prompts, so stages act
unattended with the full authority of this UNIX user: they can read anything
this account can read, and nothing isolates them at the process level.

Set ORCH_ALLOW_UNATTENDED=1 to confirm you intend that, then start again."""


class UnattendedConsentError(ValueError):
    """Unattended-capable provider commands without the operator's acknowledgement."""


def unattended_flags_in_use(env: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Which provider commands carry a known approval-disabling flag.

    Best-effort by construction: it recognises the flags of the CLIs this
    project actually drives, by name. A different CLI, a renamed flag, a wrapper
    script that adds one, or a config file that sets the same behaviour will not
    be seen — so a clean result means "no known flag was spelled out here", not
    "this deployment is attended". The launcher gate stays the first line
    precisely because it does not depend on recognising anything.
    """
    source = env if env is not None else os.environ
    found: list[tuple[str, str]] = []
    for variable in PROVIDER_COMMAND_ENV:
        command = source.get(variable, "")
        if not command:
            continue
        tokens = set(shlex.split(command)) if command.strip() else set()
        for flag in UNATTENDED_FLAGS:
            if flag in tokens or flag in command:
                found.append((variable, flag))
    return found


def require_unattended_consent(env: dict[str, str] | None = None) -> None:
    """Refuse to proceed when unattended execution was configured but not stated.

    The launcher performs the same check before exec, and this repeats it in the
    engine so that a deployment with its own launcher cannot skip it by accident.
    """
    source = env if env is not None else os.environ
    hits = unattended_flags_in_use(source)
    if not hits:
        return
    if source.get(ALLOW_UNATTENDED_ENV, "").strip() == "1":
        return
    detail = ", ".join(f"{variable} contains {flag}" for variable, flag in hits)
    raise UnattendedConsentError(f"{UNATTENDED_CONSENT_MESSAGE}\n\nDetected: {detail}")


def allow_unsandboxed_requested(env: dict[str, str] | None = None) -> bool:
    """True only when the operator explicitly accepted running without L1.

    Set by `--allow-unsandboxed` on the CLI, or directly in the environment for
    a service launcher. Anything other than an explicit truthy value means no:
    the whole point is that an unconfined mutating stage has to be asked for.
    """
    raw = (env if env is not None else os.environ).get(ALLOW_UNSANDBOXED_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


DEFAULT_GIT_IDENTITY = {"name": "agent-orch", "email": "orchestrator@orch.invalid"}
GIT_IDENTITY_ENV = "ORCH_GIT_IDENTITY"
_IDENTITY_LITERAL_RE = re.compile(r"^\s*(?P<name>[^<>]+?)\s*<(?P<email>[^<>@\s]+@[^<>\s]+)>\s*$")


def _git_identity(env: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve the commit identity for a contained stage.

    Containment replaces the global git config, and a commit with no identity
    fails outright, so one has to be written. The question is *whose*.

    Default: a fixed synthetic identity. The previous behaviour — read the
    machine's global `user.name`/`user.email` and use them — silently stamped
    the operator's real name and address onto every commit an agent made,
    including any evidence later published. See docs/decisions/0001.

    Opt in explicitly with ORCH_GIT_IDENTITY:
      * ``global``            resolve from `git config --global` (old behaviour)
      * ``Name <a@b.example>``  use this literal

    A malformed value fails the stage rather than falling back to `global`;
    silently using the real identity is the failure mode being designed out.
    """
    source = (env if env is not None else os.environ).get(GIT_IDENTITY_ENV, "").strip()
    if not source:
        return {**DEFAULT_GIT_IDENTITY, "source": "default"}
    if source == "global":
        identity = dict(DEFAULT_GIT_IDENTITY)
        for key in ("name", "email"):
            try:
                result = subprocess.run(
                    ["git", "config", "--global", f"user.{key}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            value = result.stdout.strip()
            if value:
                identity[key] = value
        # The resolved value is deliberately not recorded anywhere: only the
        # fact that a global identity was used travels into the stage record,
        # so evidence stays sanitizable by construction.
        return {**identity, "source": "global"}
    match = _IDENTITY_LITERAL_RE.match(source)
    if match is None:
        raise ContainmentError(
            f"{GIT_IDENTITY_ENV} must be 'global' or 'Name <address@host>'; refusing to guess"
        )
    return {"name": match.group("name"), "email": match.group("email"), "source": "env-literal"}


# ---------------------------------------------------------------------------
# Provider output boundary (docs/decisions/provider-output-boundary.md).
# ---------------------------------------------------------------------------
#
# A provider CLI's stdout is a *display* stream, not one utterance. A native
# `codex exec` run prints the composed prompt back under `User instructions:`,
# then reasoning and tool events, and only then the model's final message. The
# typed outcome and the convergence record are claims the model makes, so
# reading them from the merged stream lets the engine's own prompt — which
# names the outcome line and shows the convergence markers twice — or any file
# a tool event happened to `cat` supply or rescue an outcome.
#
# The fix is a dedicated channel, not a better parser: `codex exec` supports
# `--output-last-message <FILE>`, which writes exactly the final agent message
# and nothing else. Two properties matter and neither is available from the
# display stream: the CLI decides where the final message begins and ends, and
# no prompt echo or tool result can reach the file at all.
#
# `--json` was the alternative. It is rejected as the primary channel because
# it still shares one stream with everything else, and reading it means
# tracking an event-envelope shape that has changed across Codex releases — a
# version bump would silently reintroduce the parse this replaces.
# `--output-last-message` is orthogonal to `--json`, so an operator who sets
# `--json` for their own reasons keeps a correct final response either way.
#
# Which protocol a run uses is decided from the *owner and the resolved
# command* before the process starts, never from what the stream turns out to
# contain. Content-based selection would let unknown output choose how it is
# read, which is the same class of defect as reading the outcome from the
# prompt echo.

#: Selected when the command is not a recognised native Codex invocation: the
#: whole stream stays authoritative, exactly as it has always been. This is
#: what keeps a legacy, fake or custom provider command — anything that simply
#: prints its result to stdout — classified from byte-for-byte the same text.
WHOLE_STREAM_PROTOCOL = "whole_stream"
#: Selected for a recognised native `codex exec`: the final response comes
#: from the CLI's own final-message channel, bound to this run's file.
CODEX_LAST_MESSAGE_PROTOCOL = "codex_output_last_message"

#: The flag the engine appends, and the spellings that mean the operator is
#: already using the channel for their own purpose. The engine never competes
#: for it and never silently falls back either — see
#: `ProviderChannelConflictError`.
CODEX_LAST_MESSAGE_FLAG = "--output-last-message"
CODEX_LAST_MESSAGE_ALIASES = ("--output-last-message", "-o")
#: Executable names accepted as the native Codex CLI.
CODEX_EXECUTABLE_NAMES = frozenset({"codex", "codex.exe"})
#: The only Codex subcommand this engine ever runs a stage under.
CODEX_EXEC_SUBCOMMAND = "exec"

#: Name of the run-local side-channel file, inside the run's containment
#: artifact directory: the one directory a contained child is allowed to write
#: besides the workspace itself.
FINAL_RESPONSE_CAPTURE_NAME = "provider-final-response.txt"
#: Refuse rather than load an implausible final message into memory. A real
#: final message is a page of text; anything at this scale is a malfunction.
FINAL_RESPONSE_MAX_BYTES = 4 * 1024 * 1024

#: Fail-closed stop reasons for the native channel. Each is distinct because
#: the operator's next action differs: no file means the run never reached a
#: final message, an empty file means the model said nothing, and an
#: undecodable file means the channel itself is damaged.
FINAL_RESPONSE_MISSING = "provider_final_response_missing"
FINAL_RESPONSE_EMPTY = "provider_final_response_empty"
FINAL_RESPONSE_UNREADABLE = "provider_final_response_unreadable"
FINAL_RESPONSE_TOO_LARGE = "provider_final_response_too_large"

#: The complete set. A sealed manifest naming anything else is naming a reason
#: this engine did not produce, so both sealed readers refuse it rather than
#: treating an unknown string as some kind of failure they can interpret.
FINAL_RESPONSE_ERRORS = frozenset(
    {FINAL_RESPONSE_MISSING, FINAL_RESPONSE_EMPTY, FINAL_RESPONSE_UNREADABLE, FINAL_RESPONSE_TOO_LARGE}
)
#: The complete set of protocols, for the same reason.
FINAL_RESPONSE_PROTOCOLS = frozenset({WHOLE_STREAM_PROTOCOL, CODEX_LAST_MESSAGE_PROTOCOL})

#: Stop reason for a native command that already claims the channel.
PROVIDER_CHANNEL_CONFLICT = "provider_final_response_channel_conflict"

#: Sealed run manifest schema. 3 adds the provider output boundary: the
#: authoritative final response is named and hashed separately from the display
#: stream. Both sealed readers accept 1 and 2 as well, so history sealed before
#: the boundary stays verifiable without being rewritten.
RUN_MANIFEST_SCHEMA_VERSION = 3
SUPPORTED_MANIFEST_VERSIONS = frozenset({1, 2, 3})


class ProviderChannelConflictError(ValueError):
    """A recognised native Codex command already claims the final-message channel.

    Failing closed is the only safe answer. Two flags cannot both own one
    file, so the engine will not overwrite the operator's; and the previous
    behaviour — quietly treating such a command as whole-stream — reopened the
    exact contamination this boundary exists to close, by configuration, with
    nothing in the run to say it had happened.
    """


def final_response_protocol(owner: str, command: list[str]) -> str:
    """Which final-response protocol one owner and resolved command support.

    Decided from configuration alone, before the process starts, so no stream
    content can select how that stream will be read.

    Recognition is deliberately narrow — `codex exec ...` — because the
    consequence of a false positive is a stage that fails closed on every run.
    Anything unrecognised keeps the whole-stream protocol, which is the
    behaviour that shipped before this boundary existed.

    Raises `ProviderChannelConflictError` for the one case that is neither: a
    command this engine *does* recognise as native Codex, which already sets
    the final-message flag itself. That is a configuration the engine cannot
    serve, and it must be said out loud rather than downgraded.
    """
    if owner != "codex" or not command:
        return WHOLE_STREAM_PROTOCOL
    if Path(command[0]).name not in CODEX_EXECUTABLE_NAMES:
        return WHOLE_STREAM_PROTOCOL
    arguments = command[1:]
    if not arguments or arguments[0] != CODEX_EXEC_SUBCOMMAND:
        return WHOLE_STREAM_PROTOCOL
    for argument in arguments[1:]:
        if argument in CODEX_LAST_MESSAGE_ALIASES or argument.startswith(f"{CODEX_LAST_MESSAGE_FLAG}="):
            raise ProviderChannelConflictError(
                f"ORCH_CODEX_COMMAND is a native `codex exec` command that already sets "
                f"{argument.split('=')[0]}. The engine needs that flag to own this run's "
                "final-response channel and will not overwrite yours, and it will not read the "
                "typed outcome out of the display stream instead. Remove the flag from the "
                "configured command."
            )
    return CODEX_LAST_MESSAGE_PROTOCOL


class BoundaryMetadataError(ValueError):
    """Sealed boundary metadata that is malformed, unknown or self-contradicting.

    `token` names the field or rule that failed, so a caller can report a
    stable reason without parsing prose.
    """

    def __init__(self, token: str, message: str) -> None:
        super().__init__(f"{token}: {message}")
        self.token = token


@dataclass(frozen=True)
class SealedBoundary:
    """One sealed run's validated boundary metadata.

    `path` and `digest` name the artifact holding the authoritative text —
    which, for a run that produced no final response, is the display stream
    standing in the manifest's place and is *not* a final response. Ask
    `has_final_response` before reading it as one.
    """

    protocol: str
    separate: bool
    error: str | None
    path: str
    digest: str

    @property
    def has_final_response(self) -> bool:
        return self.error is None


def validate_sealed_boundary(manifest: Mapping[str, Any]) -> SealedBoundary:
    """The schema-3 boundary state matrix, enforced identically by both readers.

    Exactly three states are legal, and every one of them is reachable:

    | protocol      | separate | error          | named artifact     |
    |---------------|----------|----------------|--------------------|
    | whole_stream  | `False`  | `None`         | the display stream |
    | native        | `True`   | `None`         | its own file       |
    | native        | `False`  | a known reason | the display stream |

    Everything else is a contradiction, and one combination in particular is
    the reason this function exists: *native, not separate, no error* claims a
    run used the dedicated channel, did not produce a separate artifact, and
    did not fail. No such run exists. Accepting it made a reader fall back to
    the display stream and report that as the verified authoritative text —
    the contamination, laundered through the evidence reader.

    The artifact coupling is checked too: a non-separate row must name the
    display stream *itself*, by path and by hash, so a manifest cannot point
    the "display stream" at some third file.
    """

    def require_hex(field: str) -> str:
        value = manifest.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise BoundaryMetadataError(field, f"expected a sha256 hex digest, got {value!r}")
        return value

    def require_path(field: str) -> str:
        value = manifest.get(field)
        if not isinstance(value, str) or not value:
            raise BoundaryMetadataError(field, f"expected a non-empty path, got {value!r}")
        return value

    output_path = require_path("output_path")
    output_hash = require_hex("output_hash")
    path = require_path("final_response_path")
    digest = require_hex("final_response_hash")

    protocol = manifest.get("final_response_source")
    if protocol not in FINAL_RESPONSE_PROTOCOLS:
        raise BoundaryMetadataError(
            "final_response_source", f"unknown final-response protocol {protocol!r}"
        )
    separate = manifest.get("final_response_separate")
    if type(separate) is not bool:
        raise BoundaryMetadataError(
            "final_response_separate", f"expected a boolean, got {separate!r}"
        )
    error = manifest.get("final_response_error")
    if error is not None and error not in FINAL_RESPONSE_ERRORS:
        raise BoundaryMetadataError(
            "final_response_error", f"unknown final-response failure reason {error!r}"
        )

    if protocol == WHOLE_STREAM_PROTOCOL:
        if separate or error is not None:
            raise BoundaryMetadataError(
                "state_matrix",
                "the whole-stream protocol has no separate artifact and cannot fail closed, "
                f"but separate={separate!r} and error={error!r}",
            )
    elif separate == (error is not None):
        raise BoundaryMetadataError(
            "state_matrix",
            f"a native run has either a separate final response or a reason it has none, "
            f"never both and never neither: separate={separate!r}, error={error!r}",
        )

    if separate:
        if path == output_path:
            raise BoundaryMetadataError(
                "artifact_identity",
                "a separate final response cannot be the display stream artifact",
            )
    elif path != output_path or digest != output_hash:
        raise BoundaryMetadataError(
            "artifact_identity",
            "a non-separate row must name the display stream itself, by path and by hash",
        )
    return SealedBoundary(protocol, separate, error, path, digest)


@dataclass(frozen=True)
class FinalResponse:
    """One run's authoritative final response, or why it does not have one.

    `protocol` is always set. `text` is the authoritative text under the native
    protocol and None under the whole-stream protocol, where the run's own
    `output` is authoritative. `error` is a fail-closed stop reason and is set
    only under the native protocol; `text` and `error` are never both set.
    """

    protocol: str
    text: str | None = None
    error: str | None = None

    @property
    def native(self) -> bool:
        return self.protocol != WHOLE_STREAM_PROTOCOL


def read_final_response(path: Path, protocol: str) -> FinalResponse:
    """Read and strictly validate one native final-response capture.

    Every failure is a distinct fail-closed reason rather than a fallback: the
    one thing this must never do is answer with the display stream, because
    that is the contamination path the channel exists to close.

    A run killed at its timeout, or one that died before answering, leaves no
    file at all — the CLI writes this file once, at the end. `missing` is
    therefore also how truncation presents, and it is never a success.

    One descriptor does all the work, and the reasons are separate:

    *Liveness.* `open(2)` on a FIFO for reading blocks until a writer appears.
    A capture path that is a FIFO with no writer therefore hung the worker
    indefinitely — and it hung it *after* the child had already exited, which
    is past the point where the stage timeout can intervene. `O_NONBLOCK`
    makes the open return either way, and the file-type check below means a
    FIFO is never read from at all.

    *File type.* `O_NOFOLLOW` refuses to open a symlink, and `fstat` on the
    descriptor that was actually opened answers "what did I open" rather than
    "what was at this path a moment ago". Anything that is not a regular file
    is a damaged channel.

    This is file-type validation, and that is all it is. It is not a claim of
    immunity to active tampering by a process running as this same UID: such a
    process can replace a regular file's contents between any two operations,
    and nothing here prevents that. Same-UID tampering is outside this
    engine's threat model — the provider CLI already runs with this account's
    full authority (see `docs/threat-model.md`).
    """
    if protocol == WHOLE_STREAM_PROTOCOL:
        return FinalResponse(WHOLE_STREAM_PROTOCOL)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return FinalResponse(protocol, error=FINAL_RESPONSE_MISSING)
    except OSError:
        # Everything that is not "it is not there" is a damaged channel: ELOOP
        # or EMLINK because the path is a symlink and O_NOFOLLOW refused it,
        # ENOTDIR, EACCES, ENXIO — none of them is a final response.
        return FinalResponse(protocol, error=FINAL_RESPONSE_UNREADABLE)
    try:
        info = os.fstat(descriptor)
        if not stat_module.S_ISREG(info.st_mode):
            return FinalResponse(protocol, error=FINAL_RESPONSE_UNREADABLE)
        if info.st_size > FINAL_RESPONSE_MAX_BYTES:
            return FinalResponse(protocol, error=FINAL_RESPONSE_TOO_LARGE)
        # limit+1, so a file that grew past the cap between the fstat and the
        # read is still caught, and no more than that ever reaches memory.
        raw = _read_at_most(descriptor, FINAL_RESPONSE_MAX_BYTES + 1)
    except OSError:
        return FinalResponse(protocol, error=FINAL_RESPONSE_UNREADABLE)
    finally:
        # Unconditional: every return above passes through here.
        os.close(descriptor)
    if len(raw) > FINAL_RESPONSE_MAX_BYTES:
        return FinalResponse(protocol, error=FINAL_RESPONSE_TOO_LARGE)
    try:
        # Strict: a final response that is not valid UTF-8 is a damaged
        # channel, and replacement characters in the middle of an outcome line
        # would be a guess about what the model actually said.
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return FinalResponse(protocol, error=FINAL_RESPONSE_UNREADABLE)
    if not text.strip():
        return FinalResponse(protocol, error=FINAL_RESPONSE_EMPTY)
    return FinalResponse(protocol, text=text)


def _read_at_most(descriptor: int, limit: int) -> bytes:
    """Read up to `limit` bytes from one open descriptor, and never more.

    Loops because a short read is legal, and stops at `limit` rather than at
    EOF so the caller's cap is a property of this function and not a hope
    about the file it was pointed at.
    """
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, DRAIN_READ_BYTES))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def authoritative_text(result: "RunResult") -> str:
    """The one text a typed outcome and a convergence record may be read from.

    Bound to the run it came from, so the outcome and the convergence record
    are always read from the same verified final response rather than each
    re-deriving one. Under the whole-stream protocol — and for a result built
    by hand, which is every controller-side stop — this is the run's complete
    output, which is what it was always parsed from.
    """
    return result.output if result.final_response is None else result.final_response


@dataclass(frozen=True)
class ProviderPreflightResult:
    status: str
    reason: str
    output: str
    exit_code: int | None
    command: list[str]
    model: str | None
    started_at_ms: int
    ended_at_ms: int
    timed_out: bool = False


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None
    output: str
    outcome: str | None
    classification: str
    reason: str
    timed_out: bool = False
    #: Set when containment itself stopped the stage (sandbox unavailable,
    #: identity misconfigured, or a write outside the workspace was detected).
    #: It short-circuits classification so the stop reason stays specific
    #: instead of collapsing into a generic non-zero exit.
    containment_stop: str | None = None
    containment_violations: tuple[dict[str, str], ...] = ()
    started_at_ms: int | None = None
    ended_at_ms: int | None = None
    duration_ms: int | None = None
    model: str | None = None
    usage_input_tokens: int | None = None
    usage_output_tokens: int | None = None
    usage_total_tokens: int | None = None
    usage_unavailable_reason: str | None = None
    # Retained provider result for inspection, NEVER an accepted transition.
    candidate_outcome: str | None = None
    candidate_classification: str | None = None
    candidate_reason: str | None = None
    #: The provider output boundary. `output` above stays the complete display
    #: stream — the audit evidence — and is never narrowed or rewritten.
    #: `final_response` is the authoritative text a decision was read from, set
    #: only under the native protocol; None means the whole stream was
    #: authoritative. `final_response_error` is a fail-closed stop reason from
    #: the native channel. `final_response_source` names the protocol.
    final_response: str | None = None
    final_response_source: str | None = None
    final_response_error: str | None = None
    #: The run-owned side channel the native capture was read from, handed to
    #: the controller so it can remove the file *after* sealing the artifact
    #: that contains those bytes. None whenever there is no side channel.
    final_response_capture_path: str | None = None


def classify_result(
    exit_code: int | None,
    output: str,
    allowed_outcomes: set[str],
    timed_out: bool = False,
    *,
    source: RunResult | None = None,
) -> RunResult:
    telemetry = _telemetry_from(source)
    # Carried onto every result below, blocked ones included, so the sealed
    # manifest always records which protocol produced the text a decision was
    # — or would have been — read from. A caller with no source run keeps the
    # whole-stream protocol, which is what it was already getting.
    fields = {**telemetry, **_boundary_fields(source)}
    if source is not None and source.containment_stop is not None:
        # Containment outranks every other signal: a stage that escaped its
        # workspace, or never got a sandbox, must not be reported as a plain
        # non-zero exit — the operator needs to know which of the two happened.
        candidate = None
        if source.containment_stop in {"protected_root_drift", "workspace_escape"}:
            candidate = classify_result(
                exit_code, output, allowed_outcomes, timed_out,
                source=replace(source, containment_stop=None),
            )
        return RunResult(
            exit_code,
            output,
            None,
            "blocked",
            source.containment_stop,
            timed_out,
            containment_stop=source.containment_stop,
            containment_violations=source.containment_violations,
            candidate_outcome=candidate.outcome if candidate else None,
            candidate_classification=candidate.classification if candidate else None,
            candidate_reason=candidate.reason if candidate else None,
            **fields,
        )
    if timed_out:
        return RunResult(exit_code, output, None, "blocked", "timeout", True, **fields)
    if exit_code != 0:
        if any(pattern.search(output) for pattern in RATE_LIMIT_SIGNATURES):
            return RunResult(exit_code, output, None, "paused", "rate_limited", False, **fields)
        if any(pattern.search(output) for pattern in SOCKET_SIGNATURES):
            return RunResult(exit_code, output, None, "blocked", "provider_socket_error", False, **fields)
        return RunResult(exit_code, output, None, "blocked", "runner_nonzero", False, **fields)
    # Checked after timeout and after a non-zero exit, so those keep their own
    # specific stop reasons; a run that was killed or that failed has no final
    # response for reasons already reported.
    if source is not None and source.final_response_error is not None:
        return RunResult(exit_code, output, None, "blocked", source.final_response_error, False, **fields)
    authoritative = source.final_response if source is not None and source.final_response is not None else output
    matches = OUTCOME_RE.findall(authoritative)
    final_outcome = _final_outcome_marker(authoritative)
    if final_outcome is not None:
        if final_outcome not in allowed_outcomes:
            return RunResult(exit_code, output, final_outcome, "blocked", "unknown_outcome", False, **fields)
        return RunResult(exit_code, output, final_outcome, "success", "success", False, **fields)
    distinct = set(matches)
    if not matches:
        return RunResult(exit_code, output, None, "blocked", "missing_outcome", False, **fields)
    if len(distinct) > 1:
        # Only genuinely conflicting outcomes are ambiguous; a repeated identical
        # value is common from agents, so take the last one.
        return RunResult(exit_code, output, None, "blocked", "ambiguous_outcome", False, **fields)
    outcome = matches[-1]
    if outcome not in allowed_outcomes:
        return RunResult(exit_code, output, outcome, "blocked", "unknown_outcome", False, **fields)
    return RunResult(exit_code, output, outcome, "success", "success", False, **fields)


def _boundary_fields(source: RunResult | None) -> dict[str, str | None]:
    """The provider output boundary, carried from the raw run to its verdict."""
    if source is None:
        return {
            "final_response": None,
            "final_response_source": WHOLE_STREAM_PROTOCOL,
            "final_response_error": None,
            "final_response_capture_path": None,
        }
    return {
        "final_response": source.final_response,
        "final_response_source": source.final_response_source or WHOLE_STREAM_PROTOCOL,
        "final_response_error": source.final_response_error,
        "final_response_capture_path": source.final_response_capture_path,
    }


def _decode_authoritative(raw: bytes, encoding: str, errors: str) -> str:
    """Verbatim replica of subprocess.Popen._translate_newlines.

    This is the authoritative decode: whole buffer, strict errors, run once
    after the child is reaped. The live stream decodes the same bytes with
    replacement instead, which is exactly what makes it non-authoritative.
    """
    return raw.decode(encoding, errors).replace("\r\n", "\n").replace("\r", "\n")


_LIVE_TERMINAL = object()


class _LiveStream:
    """Non-authoritative live JSONL for one stage run.

    Live rendering is evidence. It cannot kill, retry, resume, classify,
    extend a deadline or mutate task state, and two properties make that true
    rather than merely intended:

    - Renderer failure cannot raise into the child's lifecycle. Every public
      method contains `Exception` only. Process-control `BaseException` is
      deliberately left on its legacy path, so a KeyboardInterrupt arriving
      inside `fragment()` still reaches `run()`'s handler and still terminates
      the child.
    - Renderer *slowness* cannot stall the lifecycle either. All filesystem
      I/O, file creation included, happens on one daemon writer thread. The
      lifecycle thread only ever does `put_nowait`, and the single blocking
      step - a bounded join in `close()` - is taken only once the runner has
      already reaped the child, so it cannot delay the paths where the runner
      returns immediately with a child still alive.
    """

    _THREAD_NAME = "orch-live-writer"

    def __init__(self, path: Path, owner: str, timeout_seconds: int) -> None:
        self._path = path
        self._owner = owner
        self._timeout_seconds = timeout_seconds
        # One slot beyond the optional-record depth. That slot is the terminal
        # record's, which is why put_nowait in close() cannot raise Full.
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=LIVE_QUEUE_MAX_RECORDS + 1)
        self._closing = threading.Event()
        # Set from either thread, read by the writer, so it is an Event rather
        # than a bool: "is this live evidence complete" is the one thing a
        # later reader must not get wrong.
        self._incomplete = threading.Event()
        self._thread: threading.Thread | None = None
        self._decoder: Any = None
        self._terminal: dict[str, Any] | None = None
        # Writer thread only, from here down.
        self._handle: Any = None
        self._written = 0
        self._seq = 0

    @classmethod
    def open(cls, log_path: Path, *, owner: str, timeout_seconds: int) -> "_LiveStream":
        """Start the writer thread. Total against renderer failure.

        Touches no path and enqueues nothing, and is called before
        `started = time.time()`, so no live filesystem I/O and no blocking wait
        happens inside the authoritative duration interval. Because it raises
        no `Exception`, one call binds the handle ahead of `run()`'s try, and a
        spawn `OSError` still reaches the convergence-point `close()` against a
        bound name.
        """
        try:
            stream = cls(log_path.with_suffix(".live.jsonl"), owner, timeout_seconds)
        except Exception:
            return cls._inert()
        try:
            thread = threading.Thread(target=stream._writer, name=cls._THREAD_NAME, daemon=True)
            thread.start()
        except Exception:
            # Same inert no-op state a failed file create leaves behind, not a
            # second code path.
            stream._incomplete.set()
            return stream
        stream._thread = thread
        return stream

    @classmethod
    def _inert(cls) -> "_LiveStream":
        stream = cls.__new__(cls)
        stream._path = None  # type: ignore[assignment]
        stream._owner = ""
        stream._timeout_seconds = 0
        stream._queue = queue.Queue(maxsize=1)
        stream._closing = threading.Event()
        stream._incomplete = threading.Event()
        stream._incomplete.set()
        stream._thread = None
        stream._decoder = None
        stream._terminal = None
        stream._handle = None
        stream._written = 0
        stream._seq = 0
        return stream

    # -- lifecycle-thread side: in-memory only, never blocking --------------
    def stage_start(self, *, child_pid: int, encoding: str) -> None:
        try:
            # Replacement errors, so invalid bytes render as U+FFFD in live
            # evidence while the authoritative decode still raises. That
            # divergence is the point of "non-authoritative".
            self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
            self._offer(
                {
                    "event": "stage_start",
                    "schema_version": LIVE_SCHEMA_VERSION,
                    "seq": None,
                    "ts_ms": _ms(time.time()),
                    "owner": self._owner,
                    "child_pid": child_pid,
                    "timeout_seconds": self._timeout_seconds,
                    "encoding": encoding,
                }
            )
        except Exception:
            self._decoder = None
            self._degrade()

    def fragment(self, chunk: bytes) -> None:
        try:
            if self._decoder is None:
                self._incomplete.set()
                return
            self._emit_text(self._decoder.decode(chunk))
        except Exception:
            self._degrade()

    def heartbeat(self) -> None:
        try:
            self._offer({"event": "heartbeat", "seq": None, "ts_ms": _ms(time.time())})
        except Exception:
            self._degrade()

    def eof(self) -> None:
        try:
            if self._decoder is None:
                return
            self._emit_text(self._decoder.decode(b"", final=True))
        except Exception:
            self._degrade()

    def close(self, *, process: subprocess.Popen[str] | None, timed_out: bool) -> None:
        """Admit the terminal record always; wait for it only when free to.

        Takes the `Popen` rather than a pre-computed exit code so the reaped
        test, the `returncode` read and the join decision all happen inside
        this method's exception wrapper - the call site at the convergence
        point stays as exception-free as it was before H2.
        """
        try:
            exit_code: int | None = None
            reaped = False
            if process is not None and process.poll() is not None:
                reaped = True
                exit_code = process.returncode
            self._terminal = {
                "event": "stage_end",
                "seq": None,
                "ts_ms": _ms(time.time()),
                # Explicitly non-authoritative: on the decode-failure paths the
                # authoritative exit_code is None while poll() returns the
                # child's real code, and with no reaped child this is null.
                "exit_code": exit_code,
                "timed_out": bool(timed_out),
                "live_complete": None,
            }
            self._closing.set()
            try:
                self._queue.put_nowait(_LIVE_TERMINAL)
            except queue.Full:
                self._incomplete.set()
            thread = self._thread
            # Only a path where the runner already reaped the child may wait.
            # On a spawn OSError, a mid-drain OSError, or the unreaped variant
            # of the interrupt path, the runner returns to the controller
            # immediately - in the mid-drain case with an unowned child still
            # running - and a slow writer must not add latency to exactly
            # those. There, live evidence is best effort: the daemon writer may
            # still persist stage_end, but nothing waits for it.
            if reaped and thread is not None and thread.is_alive():
                thread.join(LIVE_CLOSE_JOIN_SECONDS)
        except Exception:
            self._degrade()

    def _emit_text(self, text: str) -> None:
        if not text:
            return
        # Bounded per-record size. Not newline-normalized: normalization is
        # stateful across chunk boundaries and belongs to the authoritative
        # path only.
        for start in range(0, len(text), LIVE_FRAGMENT_MAX_CHARS):
            self._offer(
                {
                    "event": "output_fragment",
                    "seq": None,
                    "ts_ms": _ms(time.time()),
                    "text": text[start : start + LIVE_FRAGMENT_MAX_CHARS],
                }
            )

    def _offer(self, record: dict[str, Any]) -> None:
        if self._thread is None:
            self._incomplete.set()
            return
        if self._closing.is_set():
            self._incomplete.set()
            return
        # The lifecycle thread is the sole producer, so admission needs no
        # lock. Optional records stop one short of the queue's capacity, which
        # keeps the last slot for the terminal record - the same
        # terminal-reserve arithmetic the byte budget uses, applied to depth.
        if self._queue.qsize() >= LIVE_QUEUE_MAX_RECORDS:
            self._incomplete.set()
            return
        self._queue.put_nowait(record)

    def _degrade(self) -> None:
        self._incomplete.set()

    # -- writer thread side: the only place that touches the filesystem -----
    def _writer(self) -> None:
        try:
            try:
                self._handle = self._create_handle(self._path)
            except BaseException:
                self._handle = None
                self._incomplete.set()
            while True:
                item = self._queue.get()
                if item is _LIVE_TERMINAL:
                    # The lifecycle thread queues the sentinel after every
                    # admitted optional record. Preserve that order even when
                    # close() wins the race with this thread; the caller still
                    # waits no longer than LIVE_CLOSE_JOIN_SECONDS.
                    if not self._queue.empty():
                        self._incomplete.set()
                    self._finalize()
                    return
                self._write_record(item)
        except BaseException:
            # This thread has no lifecycle thread to propagate into, so it
            # contains everything, including process-control exceptions.
            self._incomplete.set()
            self._close_handle()

    def _write_record(self, record: dict[str, Any]) -> None:
        if self._handle is None:
            self._incomplete.set()
            return
        try:
            record["seq"] = self._seq
            line = json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
        except Exception:
            self._incomplete.set()
            return
        if self._written + len(line) + LIVE_TERMINAL_RESERVE_BYTES > LIVE_MAX_BYTES:
            # An optional record may never eat into the terminal reserve. A
            # continuous writer that exhausts the budget therefore also stops
            # producing heartbeats and the stream looks stalled - which is what
            # stage_end.live_complete=false tells the reader.
            self._incomplete.set()
            return
        try:
            self._handle.write(line)
            self._handle.flush()
        except Exception:
            self._incomplete.set()
            self._close_handle()
            return
        self._written += len(line)
        self._seq += 1

    def _finalize(self) -> None:
        record = self._terminal or {
            "event": "stage_end",
            "seq": None,
            "ts_ms": _ms(time.time()),
            "exit_code": None,
            "timed_out": False,
            "live_complete": None,
        }
        try:
            record["seq"] = self._seq
            record["live_complete"] = not self._incomplete.is_set()
            line = json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
        except Exception:
            self._close_handle()
            return
        # stage_end may consume the reserve. Because the reserve is larger than
        # any stage_end line, rejection by byte-budget exhaustion is impossible
        # here by arithmetic rather than by a runtime check.
        if self._handle is not None and self._written + len(line) <= LIVE_MAX_BYTES:
            try:
                self._handle.write(line)
                self._handle.flush()
                self._written += len(line)
                self._seq += 1
            except Exception:
                pass
        self._close_handle()

    @staticmethod
    def _create_handle(path: Path) -> Any:
        path.parent.mkdir(parents=True, exist_ok=True)
        # No fsync: a same-host reader sees flushed bytes, and a per-record
        # fsync would add I/O latency for no gain.
        return path.open("ab")

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except Exception:
            pass


class SubprocessRunner:
    def preflight(self, owner: str, timeout: int = 5) -> ProviderPreflightResult:
        command = self._command(owner)
        started = time.time()
        started_ms = _ms(started)
        model = self._model_from_command(command)
        env_issue = self._environment_issue(owner)
        if env_issue is not None:
            ended_ms = _ms(time.time())
            return ProviderPreflightResult(
                "blocked",
                env_issue,
                f"{env_issue}: provider environment/config check failed\n",
                None,
                command,
                model,
                started_ms,
                ended_ms,
            )

        executable = command[0]
        resolved = _resolve_executable(executable)
        if resolved is None:
            ended_ms = _ms(time.time())
            return ProviderPreflightResult(
                "blocked",
                "provider_cli_unavailable",
                f"provider CLI not found or not executable: {executable}\n",
                127,
                command,
                model,
                started_ms,
                ended_ms,
            )

        probe = [resolved, "--version"]
        exit_code: int | None = None
        output = ""
        timed_out = False
        try:
            completed = subprocess.run(
                probe,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            exit_code = completed.returncode
            output = completed.stdout
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            output = (exc.stdout or "") + (exc.stderr or "")
        except OSError as exc:
            output = f"provider preflight spawn failed: {exc}\n"
            exit_code = 127
        ended_ms = _ms(time.time())
        if timed_out:
            return ProviderPreflightResult(
                "blocked", "provider_preflight_timeout", output, exit_code, command, model, started_ms, ended_ms, True
            )
        if any(pattern.search(output) for pattern in RATE_LIMIT_SIGNATURES):
            return ProviderPreflightResult(
                "paused", "rate_limited", output, exit_code, command, model, started_ms, ended_ms
            )
        if any(pattern.search(output) for pattern in SOCKET_SIGNATURES):
            return ProviderPreflightResult(
                "blocked", "provider_socket_error", output, exit_code, command, model, started_ms, ended_ms
            )
        if exit_code != 0:
            return ProviderPreflightResult(
                "blocked", "provider_preflight_failed", output, exit_code, command, model, started_ms, ended_ms
            )
        return ProviderPreflightResult("pass", "provider_preflight_pass", output, exit_code, command, model, started_ms, ended_ms)

    def run(
        self,
        owner: str,
        prompt: str,
        timeout: int,
        log_path: Path,
        *,
        workspace: Path | None = None,
        protected_roots: tuple[Path, ...] | None = None,
        reports_dir: Path | None = None,
    ) -> RunResult:
        provider_argv = self._command(owner)
        try:
            protocol = final_response_protocol(owner, provider_argv)
        except ProviderChannelConflictError as exc:
            # A configuration this engine cannot serve. `_containment_stop` is
            # the existing "this stage was never allowed to start, and here is
            # why" path; the reason travels verbatim to the operator.
            return self._containment_stop(
                log_path, owner, provider_argv, PROVIDER_CHANNEL_CONFLICT, str(exc)
            )
        capture_path: Path | None = None
        if protocol == CODEX_LAST_MESSAGE_PROTOCOL:
            # Run-local and inside the containment artifact directory, which
            # is the one place besides the workspace a contained child may
            # write. Deriving it from log_path binds the capture to this run,
            # and clearing it before the spawn means a leftover file can never
            # be read as this run's answer.
            capture_path = log_path.with_suffix(".containment") / FINAL_RESPONSE_CAPTURE_NAME
            try:
                capture_path.parent.mkdir(parents=True, exist_ok=True)
                capture_path.unlink(missing_ok=True)
            except OSError as exc:
                return self._containment_stop(
                    log_path, owner, provider_argv, "sandbox_setup_failed",
                    f"cannot prepare the provider final-response channel: {exc}",
                )
            provider_argv = provider_argv + [CODEX_LAST_MESSAGE_FLAG, str(capture_path)]
        command = provider_argv + [prompt]
        model_command = command
        containment_env = None
        if workspace is not None:
            try:
                containment_env = prepare_containment(workspace, log_path)
                if reports_dir is not None:
                    # Reports live in the task's artifact area, not the
                    # workspace: the sandbox must allow the directory (an
                    # allowlist entry only exists for paths that exist), and
                    # the stage gets its location in the environment too.
                    reports_dir.mkdir(parents=True, exist_ok=True)
                    containment_env["ORCH_REPORTS_DIR"] = str(reports_dir)
            except SandboxSetupError as exc:
                return self._containment_stop(
                    log_path, owner, command, "sandbox_setup_failed", str(exc)
                )
            except ContainmentError as exc:
                return self._containment_stop(
                    log_path, owner, command, "containment_identity_invalid", str(exc)
                )
            except OSError as exc:
                return self._containment_stop(
                    log_path, owner, command, "sandbox_setup_failed", f"cannot create reports directory: {exc}"
                )
            try:
                decision = prepare_sandbox(
                    workspace,
                    log_path.with_suffix(".containment"),
                    allow_unsandboxed=allow_unsandboxed_requested(),
                    extra_allow=(reports_dir,) if reports_dir is not None else (),
                    protected_roots=protected_roots,
                )
            except ContainmentConfigError as exc:
                # A declared write root that overlaps a protected root is a
                # contradiction, not a preference. Refuse the stage and say so.
                return self._containment_stop(
                    log_path, owner, command, "containment_config_conflict", str(exc)
                )
            except SandboxSetupError as exc:
                # A full disk or an unwritable artifact directory is an
                # environment problem. Reporting it as a config conflict would
                # send the operator to edit settings that are already correct.
                return self._containment_stop(
                    log_path, owner, command, "sandbox_setup_failed", str(exc)
                )
            if decision.blocks_run:
                return self._containment_stop(
                    log_path,
                    owner,
                    command,
                    "sandbox_unavailable",
                    "L1 sandbox is unavailable on this host and --allow-unsandboxed was not given; "
                    "refusing to run a mutating stage unconfined",
                )
            containment_env["ORCH_CONTAINMENT_SANDBOX"] = decision.mode
            model_command = command
            command = decision.wrap(command)
        # Started before `started = time.time()` so the writer thread's
        # creation cost can reach neither the deadline window nor the
        # authoritative duration interval. open() raises no Exception, so the
        # handle is bound on every path that reaches the convergence point -
        # including a spawn OSError.
        live = _LiveStream.open(log_path, owner=owner, timeout_seconds=timeout)
        started = time.time()
        exit_code: int | None = None
        output = ""
        timed_out = False
        error: str | None = None
        child_pid: int | None = None
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,  # without this, claude -p waits on stdin EOF until the timeout
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                cwd=str(workspace) if workspace is not None else None,
                env=containment_env,
            )
            child_pid = process.pid
            containment_line = f"containment_workspace={workspace}\n" if workspace is not None else ""
            self._append_live_status(
                log_path, f"{containment_line}provider_child_pid={child_pid}\nstage_status=running\n"
            )
            stdout_encoding = process.stdout.encoding
            stdout_errors = process.stdout.errors
            # communicate() started its clock here; keeping the origin in the
            # same statement position keeps the deadline the child runs under
            # byte-for-byte the one it ran under before.
            deadline = time.monotonic() + timeout
            live.stage_start(child_pid=child_pid, encoding=stdout_encoding)
            chunks: list[bytes] = []
            try:
                self._drain_pipe(process, chunks, live, deadline, timeout)
                process.wait(timeout=deadline - time.monotonic())
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_group(process)
                # The final drain has no deadline, exactly as the second
                # communicate() call had none. A grandchild still holding the
                # pipe blocks the worker here as it always did.
                self._drain_pipe(process, chunks, live, None, timeout)
                process.wait()
            live.eof()
            # One whole-buffer strict decode, after the child is reaped and
            # before exit_code is read: a UnicodeDecodeError therefore leaves
            # exit_code at None, which is what the sealed log has always shown.
            output = _decode_authoritative(b"".join(chunks), stdout_encoding, stdout_errors)
            exit_code = process.returncode
        except OSError as exc:
            error = f"failed to spawn runner: {exc}"
            output = error + "\n"
        except BaseException as exc:
            if process is not None and process.poll() is None:
                self._terminate_group(process)
                exit_code = process.returncode
            error = f"runner interrupted: {type(exc).__name__}: {exc}"
            output += error + "\n"
        ended = time.time()
        live.close(process=process, timed_out=timed_out)
        # Read after the child is reaped, so a file still being written cannot
        # be read half-way. The capture is deliberately *left in place*: the
        # runner has no way to know whether sealing will succeed, and deleting
        # here once cost the only durable copy of the authoritative text
        # whenever anything between here and the seal failed. The controller
        # removes it after it has written the sealed artifact that contains
        # those exact bytes — a handoff, not a hope.
        final = read_final_response(capture_path, protocol) if capture_path else FinalResponse(protocol)
        self._write_log(log_path, owner, command, started, ended, exit_code, timed_out, output, error, child_pid)
        usage = _extract_usage(output)
        return RunResult(
            exit_code,
            output,
            None,
            "raw",
            "raw",
            timed_out,
            started_at_ms=_ms(started),
            ended_at_ms=_ms(ended),
            duration_ms=max(0, _ms(ended) - _ms(started)),
            model=SubprocessRunner._model_from_command(model_command) or "unspecified",
            usage_input_tokens=usage["input_tokens"],
            usage_output_tokens=usage["output_tokens"],
            usage_total_tokens=usage["total_tokens"],
            usage_unavailable_reason=usage["unavailable_reason"],
            final_response=final.text,
            final_response_source=final.protocol,
            final_response_error=final.error,
            final_response_capture_path=str(capture_path) if capture_path is not None else None,
        )

    def _containment_stop(
        self, log_path: Path, owner: str, command: list[str], reason: str, message: str
    ) -> RunResult:
        """Report a stage that was never allowed to start, with its own reason.

        The log still gets written, because "why did nothing run" is exactly
        the question an operator asks next.
        """
        now = time.time()
        text = f"{reason}: {message}\n"
        try:
            self._write_log(log_path, owner, command, now, now, None, False, text, message, None)
        except OSError:
            # The reason this stage stopped may *be* that the directory is
            # unwritable. Reporting must not depend on the thing that failed;
            # the stop reason travels in the result either way.
            pass
        return RunResult(
            None,
            text,
            None,
            "raw",
            "raw",
            False,
            containment_stop=reason,
            started_at_ms=_ms(now),
            ended_at_ms=_ms(now),
            duration_ms=0,
            model=SubprocessRunner._model_from_command(command) or "unspecified",
        )

    @staticmethod
    def _command(owner: str) -> list[str]:
        return provider_command(owner)

    @staticmethod
    def _drain_pipe(
        process: subprocess.Popen[str],
        chunks: list[bytes],
        live: "_LiveStream",
        deadline: float | None,
        orig_timeout: float,
    ) -> None:
        """Read the provider pipe to EOF, or until the deadline expires.

        Structured after subprocess.Popen._communicate on purpose: the parity
        argument for the provider's lifecycle has to be readable line by line
        against the loop this replaces. The only differences are that raw bytes
        are accumulated for one whole-buffer decode later, that select() is
        bounded so a silent child still produces evidence, and that each chunk
        is offered to the live stream - an in-memory step that cannot raise or
        block into this loop.
        """
        stdout = process.stdout
        if stdout is None or stdout.closed:
            return  # EOF was already reached in the first phase
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ)
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(process.args, orig_timeout)
                    wait_for = min(remaining, LIVE_POLL_SECONDS)
                else:
                    wait_for = LIVE_POLL_SECONDS
                ready = selector.select(wait_for)
                # Checked again before any ready event is consumed, exactly
                # where _check_timeout sits in _communicate. min() above is
                # also what keeps the poll bound from extending the deadline.
                if deadline is not None and time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(process.args, orig_timeout)
                if not ready:
                    live.heartbeat()
                    continue
                key = ready[0][0]
                # An OSError here lands in run()'s OSError handler, which
                # reports "failed to spawn runner", does not terminate, and
                # returns while the child keeps running unowned. Misleading,
                # and current behaviour.
                data = os.read(key.fd, DRAIN_READ_BYTES)
                if not data:
                    selector.unregister(stdout)
                    stdout.close()
                    return
                chunks.append(data)
                live.fragment(data)

    @staticmethod
    def _terminate_group(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @staticmethod
    def _write_log(
        path: Path,
        owner: str,
        command: list[str],
        started: float,
        ended: float,
        exit_code: int | None,
        timed_out: bool,
        output: str,
        error: str | None,
        child_pid: int | None,
    ) -> None:
        usage = _extract_usage(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = [
            f"started_at={started:.6f}",
            f"ended_at={ended:.6f}",
            f"duration_seconds={ended - started:.3f}",
            f"owner={owner}",
            f"model={SubprocessRunner._model_from_command(command) or 'unspecified'}",
            f"command={shlex.join(command)}",
            f"child_pid={_format_unavailable(child_pid)}",
            f"exit_code={exit_code}",
            f"timed_out={str(timed_out).lower()}",
            f"usage_input_tokens={_format_unavailable(usage['input_tokens'])}",
            f"usage_output_tokens={_format_unavailable(usage['output_tokens'])}",
            f"usage_total_tokens={_format_unavailable(usage['total_tokens'])}",
            f"usage_unavailable_reason={usage['unavailable_reason'] or 'none'}",
        ]
        if error:
            header.append(f"controller_error={error}")
        path.write_text("\n".join(header) + "\n\n--- output ---\n" + output, encoding="utf-8")

    @staticmethod
    def _append_live_status(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _model_from_command(command: list[str]) -> str | None:
        for index, value in enumerate(command):
            if value in {"--model", "-m"} and index + 1 < len(command):
                return command[index + 1]
            if value.startswith("--model="):
                return value.split("=", 1)[1]
        return None

    @staticmethod
    def _environment_issue(owner: str) -> str | None:
        if owner == "claude":
            base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
            if base_url and LOCALHOST_SIGNATURE.search(base_url):
                return "provider_socket_misconfigured"
        if owner == "codex":
            try:
                final_response_protocol(owner, provider_command(owner))
            except ProviderChannelConflictError:
                return PROVIDER_CHANNEL_CONFLICT
            except ValueError:
                # An unusable command is reported by the executable check in
                # `preflight`, which names the actual binary.
                pass
            return _codex_config_issue()
        return None


def _allowed_codex_service_tiers() -> frozenset[str]:
    """Codex service tiers accepted by provider preflight.

    Defaults to the tiers real Codex configs use ("fast", "priority"). This
    check validates whether the current `~/.codex/config.toml` is usable, so
    the default must not reject a normal Codex setup. Override with
    ORCH_CODEX_SERVICE_TIERS (comma-separated) to enforce a narrower cost
    guardrail without editing `~/.codex/config.toml`.
    """
    raw = os.environ.get("ORCH_CODEX_SERVICE_TIERS")
    if raw:
        return frozenset(part.strip() for part in raw.split(",") if part.strip())
    return DEFAULT_CODEX_SERVICE_TIERS


def _codex_config_issue() -> str | None:
    config_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config_path = config_home / "config.toml"
    if not config_path.exists():
        return None
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "provider_config_invalid"
    service_tier = data.get("service_tier")
    if service_tier is not None and service_tier not in _allowed_codex_service_tiers():
        return "provider_config_invalid"
    return None


def _resolve_executable(value: str) -> str | None:
    path = Path(value)
    if path.parent != Path("."):
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(value)


def _extract_usage(output: str) -> dict[str, int | str | None]:
    found: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped[:1] not in "{[":
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        _merge_usage(found, _usage_from_json(parsed))

    regex_pairs = {
        "input_tokens": (
            re.compile(r"\binput[_ -]?tokens?\b\s*[:=]\s*(\d+)", re.IGNORECASE),
            re.compile(r"\bprompt[_ -]?tokens?\b\s*[:=]\s*(\d+)", re.IGNORECASE),
        ),
        "output_tokens": (
            re.compile(r"\boutput[_ -]?tokens?\b\s*[:=]\s*(\d+)", re.IGNORECASE),
            re.compile(r"\bcompletion[_ -]?tokens?\b\s*[:=]\s*(\d+)", re.IGNORECASE),
        ),
        "total_tokens": (re.compile(r"\btotal[_ -]?tokens?\b\s*[:=]\s*(\d+)", re.IGNORECASE),),
    }
    for key, patterns in regex_pairs.items():
        if found[key] is not None:
            continue
        for pattern in patterns:
            match = pattern.search(output)
            if match:
                found[key] = int(match.group(1))
                break
    if found["total_tokens"] is None and found["input_tokens"] is not None and found["output_tokens"] is not None:
        found["total_tokens"] = int(found["input_tokens"]) + int(found["output_tokens"])
    unavailable = None if any(value is not None for value in found.values()) else "provider_cli_usage_not_reported"
    return {**found, "unavailable_reason": unavailable}


def _usage_from_json(value: Any) -> dict[str, int | None]:
    found: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    if isinstance(value, list):
        for item in value:
            _merge_usage(found, _usage_from_json(item))
        return found
    if not isinstance(value, dict):
        return found
    candidates = [value]
    for key in ("usage", "token_usage", "tokens"):
        child = value.get(key)
        if isinstance(child, dict):
            candidates.append(child)
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens", "input", "prompt"),
        "output_tokens": ("output_tokens", "completion_tokens", "output", "completion"),
        "total_tokens": ("total_tokens", "total"),
    }
    for candidate in candidates:
        for normalized, keys in aliases.items():
            if found[normalized] is not None:
                continue
            for key in keys:
                token_value = candidate.get(key)
                if isinstance(token_value, int):
                    found[normalized] = token_value
                    break
                if isinstance(token_value, str) and token_value.isdigit():
                    found[normalized] = int(token_value)
                    break
    return found


def _merge_usage(target: dict[str, int | None], source: dict[str, int | None]) -> None:
    for key, value in source.items():
        if target.get(key) is None and value is not None:
            target[key] = value


def _telemetry_from(source: RunResult | None) -> dict[str, int | str | None]:
    if source is None:
        usage = _extract_usage("")
        return {
            "started_at_ms": None,
            "ended_at_ms": None,
            "duration_ms": None,
            "model": None,
            "usage_input_tokens": usage["input_tokens"],
            "usage_output_tokens": usage["output_tokens"],
            "usage_total_tokens": usage["total_tokens"],
            "usage_unavailable_reason": usage["unavailable_reason"],
        }
    usage = {
        "input_tokens": source.usage_input_tokens,
        "output_tokens": source.usage_output_tokens,
        "total_tokens": source.usage_total_tokens,
        "unavailable_reason": source.usage_unavailable_reason,
    }
    if not any(usage[key] is not None for key in ("input_tokens", "output_tokens", "total_tokens")) and not usage[
        "unavailable_reason"
    ]:
        usage = _extract_usage(source.output)
    return {
        "started_at_ms": source.started_at_ms,
        "ended_at_ms": source.ended_at_ms,
        "duration_ms": source.duration_ms,
        "model": source.model,
        "usage_input_tokens": usage["input_tokens"],
        "usage_output_tokens": usage["output_tokens"],
        "usage_total_tokens": usage["total_tokens"],
        "usage_unavailable_reason": usage["unavailable_reason"],
    }


def _format_unavailable(value: int | None) -> str:
    return str(value) if value is not None else "unavailable"


def _ms(value: float) -> int:
    return int(value * 1000)


def _final_outcome_marker(output: str) -> str | None:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        match = OUTCOME_RE.match(stripped)
        return match.group(1) if match else None
    return None
