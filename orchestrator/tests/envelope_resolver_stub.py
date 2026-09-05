"""An offline stand-in for the intake resolver's provider process boundary.

`orch start` resolves the Interpretation Envelope by asking one fixed provider
(`orchestrator.start.RESOLVER_OWNER`) one bounded question about the task's own
natural-language sources. That question is a subprocess call, so an offline
test has to replace something — and the one thing it may replace is the process
boundary itself: `orchestrator.start._invoke_resolver`. Everything above the
boundary, including every validation the engine performs on the untrusted
reply, still runs.

What must **not** happen is a test rewriting the user's scope to suit the
engine. The fixtures in this repository stay in the natural language an
operator actually types; this module answers them the way a resolver plausibly
would, and the engine judges that answer on its merits.

The resolver is the system's single semantic interpreter: polarity, negation,
read-only references and ambiguity are read there and nowhere else. So this
stand-in has to carry that responsibility too, and its rule is stated here
rather than hidden:

- `task_owned_write_targets` is `declared` from the path-like tokens the
  sources themselves carry, each quoted from the line it appears on — except a
  line that names the path only to forbid writing it, which grants nothing and
  is skipped. Sources that carry no path at all describe a task that writes
  nothing but its own report, so the axis is `declared` empty and only the
  engine-owned outputs end up inside it; the engine, not this stub, decides
  whether that claim is supportable.
- `semantic_change_surface` is `semantically_silent`, so the engine's own
  section 1.3 default applies: the behaviours the task's own scope names,
  quoted verbatim from it and not expanded.
- Every other axis is `semantically_silent`, which is what these fixtures are.

`DENIAL_MARKERS` below is part of that model stand-in, not of the engine: it is
how this fake reads polarity. The engine has no such list, and a test that
wants to prove the engine's behaviour under a careless reply passes that reply
in explicitly instead of relying on this default.
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

from orchestrator.start import RESOLVER_BEGIN, RESOLVER_END, ENVELOPE_SCHEMA_VERSION


PATH_TOKEN = re.compile(r"(?:[\w.~-]*/)+[\w.-]+")

#: A line that names a path only to put it out of bounds. A resolver that reads
#: such a line as authorisation is proposing exactly the widening the engine
#: refuses, so the stub does not pretend the model is that careless by default;
#: the adversarial tests pass that reply in explicitly.
DENIAL_MARKERS = (
    "read-only", "read only", "do not write", "must not write", "do not modify",
    "read-only background", "off limits", "untouched", "do not touch",
    "唯讀", "只讀", "不可寫", "不修改",
)


def _sources(prompt: str) -> dict[str, str]:
    """The labelled sources the engine put in the prompt, and nothing else."""
    body = prompt.split("### SOURCE: ", 1)[1] if "### SOURCE: " in prompt else ""
    body = body.split("\n\nReport six axes:", 1)[0]
    found: dict[str, str] = {}
    for index, chunk in enumerate(("### SOURCE: " + body).split("### SOURCE: ")):
        if index == 0 or not chunk.strip():
            continue
        label, _, text = chunk.partition("\n")
        found[label.strip()] = text.strip()
    return found


def _axis(state: str, value, evidence=(), detail: str = "") -> dict:
    return {"state": state, "value": value, "evidence": list(evidence), "detail": detail}


def _write_targets(sources: dict[str, str]) -> dict:
    targets: list[str] = []
    quotes: list[str] = []
    for text in sources.values():
        for line in text.split("\n"):
            lowered = line.casefold()
            if any(marker in lowered for marker in DENIAL_MARKERS):
                continue
            for match in PATH_TOKEN.finditer(line):
                token = match.group(0)
                if token not in targets:
                    targets.append(token)
                    quotes.append(line.strip())
    return _axis("declared", targets, quotes)


def _semantic_surface(sources: dict[str, str]) -> dict:
    scope = sources.get("scope") or sources.get("task text") or ""
    first = next((line.strip() for line in scope.split("\n") if line.strip()), "")
    behaviour = first.rstrip(".").strip()
    return _axis("semantically_silent", [behaviour] if behaviour else [])


def scripted_reply(prompt: str) -> str:
    """One well-formed proposal, grounded in the prompt's own sources."""
    sources = _sources(prompt)
    proposal = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "semantic_change_surface": _semantic_surface(sources),
        "task_owned_write_targets": _write_targets(sources),
        "assurance_ceiling": _axis("semantically_silent", []),
        "threat_model": _axis("semantically_silent", []),
        "evidence_ceiling": _axis("semantically_silent", []),
        "scope_expansion_policy": _axis("semantically_silent", "user_decision"),
    }
    return f"{RESOLVER_BEGIN}\n{json.dumps(proposal, ensure_ascii=False)}\n{RESOLVER_END}\n"


def patch_resolver(reply=None):
    """Patch the provider process boundary, and only it."""
    answer = reply or (lambda prompt: scripted_reply(prompt))
    return patch("orchestrator.start._invoke_resolver", side_effect=answer)


def accepted_resolution(*, write_targets=(), surface=()):
    """An envelope-internal seam: a resolution the engine has already accepted.

    Some tests are about what happens *after* intake resolves — routing, the
    enqueued request, the stop-gate lifecycle — on fixtures whose natural
    language names no writable path. Those sources are the operator's own text
    and a test may not edit them to suit the engine, so the seam moves inward:
    `_resolve_envelope` is replaced by an already-accepted result, exactly as
    if the operator had said what the axis needs.

    This grants nothing at runtime. It is a test double for one function, it
    carries no authority of its own, and every test that is about resolution
    itself still goes through `_invoke_resolver` and the full validation above
    it.
    """
    from orchestrator.start import (
        ENVELOPE_AXES,
        ENVELOPE_ENUM_AXIS,
        ENVELOPE_SOURCE_DEFAULT,
        ENVELOPE_SOURCE_REQUIREMENT,
        ENVELOPE_SEMANTIC_AXIS,
        ENVELOPE_WRITE_AXIS,
        SCOPE_EXPANSION_USER_DECISION,
        EnvelopeAxis,
        EnvelopeResolution,
    )

    def declared(members):
        return EnvelopeAxis(
            "declared",
            list(members),
            False,
            {member: ENVELOPE_SOURCE_REQUIREMENT for member in members},
        )

    axes = {}
    for axis in ENVELOPE_AXES:
        if axis == ENVELOPE_WRITE_AXIS:
            axes[axis] = declared(write_targets)
        elif axis == ENVELOPE_SEMANTIC_AXIS:
            axes[axis] = declared(surface) if surface else EnvelopeAxis(
                "semantically_silent", [], True, {}
            )
        elif axis == ENVELOPE_ENUM_AXIS:
            axes[axis] = EnvelopeAxis(
                "semantically_silent", SCOPE_EXPANSION_USER_DECISION, True, ENVELOPE_SOURCE_DEFAULT
            )
        else:
            axes[axis] = EnvelopeAxis("semantically_silent", [], True, {})
    return EnvelopeResolution(axes, None)


def patch_envelope(*, write_targets=(), surface=()):
    """Patch `_resolve_envelope` with the accepted resolution above."""
    resolution = accepted_resolution(write_targets=write_targets, surface=surface)
    return patch("orchestrator.start._resolve_envelope", return_value=resolution)
