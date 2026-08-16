from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProfileError(ValueError):
    pass


TERMINAL_STATUSES = {"done", "failed"}
OWNERS = {"claude", "codex"}
IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class Stage:
    name: str
    owner: str | None
    attempt_cap: int | None
    timeout: int | None
    prompt: str | None
    outcomes: dict[str, str]
    terminal: str | None


@dataclass(frozen=True)
class Profile:
    version: int
    type: str
    initial_stage: str
    max_transitions: int
    stages: dict[str, Stage]
    edge_caps: dict[str, int]

    def stage(self, name: str) -> Stage:
        try:
            return self.stages[name]
        except KeyError as exc:
            raise ProfileError(f"unknown stage in snapshot: {name}") from exc

    def edge_id(self, stage: str, outcome: str) -> str:
        return f"{stage}.{outcome}"

    def to_dict(self) -> dict[str, Any]:
        stages: dict[str, Any] = {}
        for name, stage in self.stages.items():
            if stage.terminal:
                stages[name] = {"terminal": stage.terminal}
            else:
                stages[name] = {
                    "owner": stage.owner,
                    "attempt_cap": stage.attempt_cap,
                    "timeout": stage.timeout,
                    "prompt": stage.prompt,
                    "outcomes": dict(stage.outcomes),
                }
        return {
            "version": self.version,
            "type": self.type,
            "initial_stage": self.initial_stage,
            "max_transitions": self.max_transitions,
            "stages": stages,
            "edge_caps": dict(self.edge_caps),
        }


def canonical_json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_profile(path: Path) -> Profile:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"cannot read profile {path}: {exc}") from exc
    data = parse_yaml_mapping(text)
    return validate_profile(data)


def profile_from_snapshot(path: Path, expected_hash: str) -> Profile:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProfileError(f"cannot read profile snapshot {path}: {exc}") from exc
    actual = sha256_bytes(raw)
    if actual != expected_hash:
        raise ProfileError(f"profile snapshot hash mismatch: expected {expected_hash}, got {actual}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProfileError(f"invalid profile snapshot JSON: {exc}") from exc
    return validate_profile(data)


def validate_profile(data: Any) -> Profile:
    if not isinstance(data, dict):
        raise ProfileError("profile root must be a mapping")
    _exact_keys(data, {"version", "type", "initial_stage", "max_transitions", "stages", "edge_caps"}, "profile")
    version = _positive_int(data.get("version"), "version")
    task_type = _identifier(data.get("type"), "type")
    initial_stage = _identifier(data.get("initial_stage"), "initial_stage")
    max_transitions = _positive_int(data.get("max_transitions"), "max_transitions")

    raw_stages = data.get("stages")
    if not isinstance(raw_stages, dict) or not raw_stages:
        raise ProfileError("stages must be a non-empty mapping")
    stages: dict[str, Stage] = {}
    terminal_count = 0
    for raw_name, raw_stage in raw_stages.items():
        name = _identifier(raw_name, "stage name")
        if name in stages:
            raise ProfileError(f"duplicate stage name: {name}")
        if not isinstance(raw_stage, dict):
            raise ProfileError(f"stage {name} must be a mapping")
        if "terminal" in raw_stage:
            _exact_keys(raw_stage, {"terminal"}, f"stage {name}")
            terminal = _nonempty_string(raw_stage["terminal"], f"stage {name}.terminal")
            if terminal not in TERMINAL_STATUSES:
                raise ProfileError(f"stage {name}.terminal must be one of {sorted(TERMINAL_STATUSES)}")
            terminal_count += 1
            stages[name] = Stage(name, None, None, None, None, {}, terminal)
            continue

        _exact_keys(raw_stage, {"owner", "attempt_cap", "timeout", "prompt", "outcomes"}, f"stage {name}")
        owner = _nonempty_string(raw_stage.get("owner"), f"stage {name}.owner")
        if owner not in OWNERS:
            raise ProfileError(f"stage {name}.owner must be one of {sorted(OWNERS)}")
        attempt_cap = _positive_int(raw_stage.get("attempt_cap"), f"stage {name}.attempt_cap")
        timeout = _positive_int(raw_stage.get("timeout"), f"stage {name}.timeout")
        prompt = _nonempty_string(raw_stage.get("prompt"), f"stage {name}.prompt")
        raw_outcomes = raw_stage.get("outcomes")
        if not isinstance(raw_outcomes, dict) or not raw_outcomes:
            raise ProfileError(f"stage {name}.outcomes must be a non-empty mapping")
        outcomes: dict[str, str] = {}
        for raw_outcome, raw_target in raw_outcomes.items():
            outcome = _identifier(raw_outcome, f"stage {name} outcome")
            target = _nonempty_string(raw_target, f"stage {name}.{outcome} target")
            outcomes[outcome] = target
        stages[name] = Stage(name, owner, attempt_cap, timeout, prompt, outcomes, None)

    if terminal_count < 1:
        raise ProfileError("profile must define at least one terminal stage")
    if initial_stage not in stages:
        raise ProfileError(f"initial_stage does not exist: {initial_stage}")
    if stages[initial_stage].terminal:
        raise ProfileError("initial_stage cannot be terminal")

    raw_caps = data.get("edge_caps")
    if not isinstance(raw_caps, dict):
        raise ProfileError("edge_caps must be a mapping")
    expected_edges: set[str] = set()
    for stage in stages.values():
        for outcome, target in stage.outcomes.items():
            if target not in stages:
                raise ProfileError(f"edge {stage.name}.{outcome} targets missing stage: {target}")
            expected_edges.add(f"{stage.name}.{outcome}")
    actual_edges = set(raw_caps)
    missing = sorted(expected_edges - actual_edges)
    extra = sorted(actual_edges - expected_edges)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing caps: {', '.join(missing)}")
        if extra:
            details.append(f"unknown caps: {', '.join(extra)}")
        raise ProfileError("edge_caps must cover every edge exactly (" + "; ".join(details) + ")")
    edge_caps = {edge: _positive_int(value, f"edge cap {edge}") for edge, value in raw_caps.items()}
    return Profile(version, task_type, initial_stage, max_transitions, stages, edge_caps)


def parse_yaml_mapping(text: str) -> dict[str, Any]:
    """Parse the strict mapping-only YAML subset used by MVP profiles.

    This intentionally excludes sequences, aliases, tags, and block scalars. It keeps
    the runtime dependency-free while still accepting normal nested YAML mappings.
    Quoted strings, integers, booleans, and null are supported.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-2, root)]
    for line_number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ProfileError(f"line {line_number}: tabs are not allowed for indentation")
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2:
            raise ProfileError(f"line {line_number}: indentation must use multiples of two spaces")
        content = raw.strip()
        if content.startswith("-"):
            raise ProfileError(f"line {line_number}: sequences are not supported in MVP profiles")
        if ":" not in content:
            raise ProfileError(f"line {line_number}: expected key: value")
        key_text, value_text = content.split(":", 1)
        key = _parse_key(key_text.strip(), line_number)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            raise ProfileError(f"line {line_number}: invalid indentation")
        parent_indent, parent = stack[-1]
        if indent > parent_indent + 2:
            raise ProfileError(f"line {line_number}: indentation jumped more than one level")
        if key in parent:
            raise ProfileError(f"line {line_number}: duplicate key {key!r}")
        value_text = value_text.strip()
        if not value_text:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(value_text, line_number)
    return root


def _parse_key(text: str, line_number: int) -> str:
    if not text:
        raise ProfileError(f"line {line_number}: empty key")
    value = _parse_scalar(text, line_number) if text[:1] in {"'", '"'} else text
    if not isinstance(value, str) or not value:
        raise ProfileError(f"line {line_number}: key must be a non-empty string")
    return value


def _parse_scalar(text: str, line_number: int) -> Any:
    if text[:1] in {"'", '"'}:
        try:
            value = ast.literal_eval(text)
        except (SyntaxError, ValueError) as exc:
            raise ProfileError(f"line {line_number}: invalid quoted string") from exc
        if not isinstance(value, str):
            raise ProfileError(f"line {line_number}: only quoted strings are supported")
        return value
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    if text.startswith(("[", "{", "&", "*", "!", "|", ">")):
        raise ProfileError(f"line {line_number}: unsupported YAML feature")
    return text


def _exact_keys(data: dict[str, Any], expected: set[str], location: str) -> None:
    actual = set(data)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if extra:
            parts.append(f"unknown {', '.join(extra)}")
        raise ProfileError(f"{location}: " + "; ".join(parts))


def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProfileError(f"{location} must be a positive integer")
    return value


def _nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{location} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, location: str) -> str:
    text = _nonempty_string(value, location)
    if not IDENTIFIER_RE.fullmatch(text):
        raise ProfileError(f"{location} must match {IDENTIFIER_RE.pattern}")
    return text
