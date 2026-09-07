"""Opt-in, request-scoped execution choices; no environment or provider calls.

The profile remains the authority for stage names and provider owners. A plan
adds role/model/effort, never changes the graph or grants tool permissions.
Absent plans leave legacy routing untouched. These are invocation choices,
not evidence that the account can access a model (the runner must verify that).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .profile import Profile, canonical_json


SCHEMA_VERSION = 1
EFFORTS = frozenset({"low", "medium", "high"})
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
ROLE_PROVIDERS = {"executor": "codex", "reviewer": "claude"}
PLAN_BEGIN = "<!-- orch-execution-plan:v1 -->"
PLAN_END = "<!-- /orch-execution-plan -->"


class ExecutionConfigError(ValueError):
    """An opt-in request cannot be represented without guessing."""


def _keys(value: Any, required: set[str], optional: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise ExecutionConfigError(f"{label}: expected object")
    missing, unknown = required - value.keys(), value.keys() - required - optional
    if missing or unknown:
        raise ExecutionConfigError(f"{label}: missing={sorted(missing)}, unknown={sorted(unknown)}")
    return value


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise ExecutionConfigError(f"{label}: expected a non-option identifier without whitespace")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionConfigError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_request(text: str) -> dict:
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, TypeError) as exc:
        raise ExecutionConfigError(f"invalid execution JSON: {exc}") from exc
    request = _keys(value, {"schema_version", "logical_work_id", "spec_series_id", "stages"}, set(), "execution")
    if not isinstance(request["stages"], dict) or any(not isinstance(v, dict) for v in request["stages"].values()):
        raise ExecutionConfigError("execution stages must be objects")
    return request


@dataclass(frozen=True)
class ExecutionChoice:
    role: str
    provider: str
    requested_model: str | None
    requested_effort: str | None
    model: str
    effort: str

    def to_dict(self) -> dict:
        return {
            "role": self.role, "provider": self.provider,
            "requested_model": self.requested_model, "requested_effort": self.requested_effort,
            "model": self.model, "effort": self.effort,
            "model_source": "explicit" if self.requested_model is not None else "role_default",
            "effort_source": "explicit" if self.requested_effort is not None else "role_default",
        }


@dataclass(frozen=True)
class ExecutionPlan:
    logical_work_id: str
    spec_series_id: str
    stages: Mapping[str, ExecutionChoice]
    defaults_digest: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "logical_work_id": self.logical_work_id,
            "spec_series_id": self.spec_series_id,
            "defaults_digest": self.defaults_digest,
            "stages": {key: value.to_dict() for key, value in self.stages.items()},
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()


def resolve_request(
    request: dict, profile: Profile, *, defaults: Mapping[str, Mapping[str, str]] | None = None,
) -> ExecutionPlan:
    """Resolve explicit > role defaults, never consult mutable process env.

    Require coverage of every nonterminal stage. This prevents an omitted
    repair/review silently falling back to a different global model.
    """
    _keys(request, {"schema_version", "logical_work_id", "spec_series_id", "stages"}, set(), "execution")
    if type(request["schema_version"]) is not int or request["schema_version"] != SCHEMA_VERSION:
        raise ExecutionConfigError("unsupported execution schema_version")
    work = _token(request["logical_work_id"], "logical_work_id")
    series = _token(request["spec_series_id"], "spec_series_id")
    if IDENTITY.fullmatch(work) is None or IDENTITY.fullmatch(series) is None:
        raise ExecutionConfigError("work and spec identities must be path-safe identifiers")
    stages = request["stages"]
    expected = {name for name, stage in profile.stages.items() if not stage.terminal}
    _keys(stages, expected, set(), "stages")
    resolved: dict[str, ExecutionChoice] = {}
    for name, config in stages.items():
        _keys(config, {"role", "provider"}, {"model", "effort"}, f"stage {name}")
        role, provider = config["role"], config["provider"]
        if not isinstance(role, str) or role not in ROLE_PROVIDERS:
            raise ExecutionConfigError(f"stage {name}: unsupported role")
        if provider != ROLE_PROVIDERS[role] or provider != profile.stage(name).owner:
            raise ExecutionConfigError(f"stage {name}: role/provider disagrees with supported pairing or profile")
        fallback = (defaults or {}).get(role, {})
        # null is not omission: it is an invalid explicit value.
        model = _token(config.get("model", fallback.get("model")), f"stage {name}.model")
        effort = config.get("effort", fallback.get("effort"))
        if not isinstance(effort, str) or effort not in EFFORTS:
            raise ExecutionConfigError(f"stage {name}: unsupported effort {effort!r}")
        if role == "reviewer" and model != "claude-fable-5-1":
            raise ExecutionConfigError(f"stage {name}: opt-in reviewer must be claude-fable-5-1")
        resolved[name] = ExecutionChoice(role, provider, config.get("model"), config.get("effort"), model, effort)
    used_default = any(c.requested_model is None or c.requested_effort is None for c in resolved.values())
    defaults_digest = hashlib.sha256(canonical_json(dict(defaults or {}))).hexdigest() if used_default else None
    return ExecutionPlan(work, series, MappingProxyType(resolved), defaults_digest)


def restore_plan(data: Any, profile: Profile, expected_digest: str) -> ExecutionPlan:
    """Read a sealed plan without re-resolving defaults or current environment."""
    _keys(data, {"schema_version", "logical_work_id", "spec_series_id", "stages", "defaults_digest"}, set(), "snapshot")
    if not isinstance(data["stages"], dict):
        raise ExecutionConfigError("snapshot stages must be an object")
    request = {key: value for key, value in data.items() if key not in {"stages", "defaults_digest"}}
    request["stages"] = {}
    for name, item in data["stages"].items():
        _keys(item, {"role", "provider", "requested_model", "requested_effort", "model", "effort", "model_source", "effort_source"}, set(), name)
        for field in ("model", "effort"):
            requested = item[f"requested_{field}"]
            source = "explicit" if requested is not None else "role_default"
            if item[f"{field}_source"] != source or (requested is not None and requested != item[field]):
                raise ExecutionConfigError(f"snapshot {name}: inconsistent {field} provenance")
        request["stages"][name] = {key: item[key] for key in ("role", "provider", "model", "effort")}
    checked = resolve_request(request, profile)
    restored = {
        name: ExecutionChoice(choice.role, choice.provider, data["stages"][name]["requested_model"],
                              data["stages"][name]["requested_effort"], choice.model, choice.effort)
        for name, choice in checked.stages.items()
    }
    digest = data["defaults_digest"]
    used_default = any(c.requested_model is None or c.requested_effort is None for c in restored.values())
    if (used_default and (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None)) or (not used_default and digest is not None):
        raise ExecutionConfigError("inconsistent defaults digest")
    plan = ExecutionPlan(checked.logical_work_id, checked.spec_series_id, MappingProxyType(restored), digest)
    if plan.digest != expected_digest:
        raise ExecutionConfigError("execution snapshot hash mismatch")
    return plan


def render_plan(plan: ExecutionPlan, *, review: dict | None = None, session: dict | None = None) -> str:
    data = {"plan": plan.to_dict(), "digest": plan.digest}
    if review is not None:
        data["review"] = review
    if session is not None:
        data["session"] = session
    return PLAN_BEGIN + "\n" + json.dumps(data, sort_keys=True) + "\n" + PLAN_END + "\n"


def extract_plan(text: str, profile: Profile) -> ExecutionPlan | None:
    """Writer-owned prefix, then ordinary task text (including its envelope).

    The controller hashes the entire input snapshot. Nested or repeated marker
    text is refused rather than used to switch a legacy task's authority.
    """
    if PLAN_BEGIN not in text and PLAN_END not in text:
        return None
    if not text.startswith(PLAN_BEGIN + "\n") or text.count(PLAN_BEGIN) != 1 or text.count(PLAN_END) != 1:
        raise ExecutionConfigError("execution plan must be a unique input prefix")
    raw, _, tail = text[len(PLAN_BEGIN) + 1:].partition("\n" + PLAN_END + "\n")
    if not tail and not text.endswith("\n" + PLAN_END + "\n"):
        raise ExecutionConfigError("malformed execution plan framing")
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError) as exc:
        raise ExecutionConfigError("invalid execution snapshot JSON") from exc
    _keys(data, {"plan", "digest"}, {"review", "session"}, "execution frame")
    return restore_plan(data["plan"], profile, data["digest"])


def review_context(text: str, field: str = "review") -> dict | None:
    """Only call after extract_plan validated the writer-owned frame."""
    if not text.startswith(PLAN_BEGIN + "\n"):
        return None
    raw = text[len(PLAN_BEGIN) + 1:].split("\n" + PLAN_END + "\n", 1)[0]
    return json.loads(raw, object_pairs_hook=_unique_object).get(field)
