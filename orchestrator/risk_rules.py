"""Deployment-supplied risk vocabulary.

Intake classifies a task by looking for words in its description. Which words
matter is not a property of the engine — it is a property of the system the
engine is dispatching for: its private file names, its stores, its subsystems.
Those literals cannot ship here, so they are loaded from a file the deployment
owns.

Contract (see `risk-rules.yaml` at the repository root for a worked example):

    version: 1
    defaults:
      risk: low
      require_stop_gate: false
    rules:
      <rule_id>:
        keyword: <literal or regex>
        category: high_risk | medium_risk | stop_gate | target_hint
        action: mark_high_risk | mark_medium_risk | require_stop_gate
                | require_human_ack | hint_target
        match: substring | word | regex      # optional, default substring
        target: <name>                       # optional, for hint_target
        note: <one line>                     # optional

Failure behaviour, which matters more than the happy path:

* No file configured    → empty ruleset, silent. The engine has no built-in
                          vocabulary beyond its own generic defaults, and that
                          is a legitimate configuration.
* Configured but absent → empty ruleset plus a warning. A path that points at
                          nothing is a mistake worth saying out loud, but it
                          cannot silently *add* risk, so it does not stop work.
* Malformed             → raise. A half-parsed risk file would silently
                          under-classify tasks, which is the one outcome worse
                          than not starting: the operator would believe gating
                          was in force while it was not. Fail closed.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .profile import ProfileError, parse_yaml_mapping


RISK_RULES_ENV = "ORCH_RISK_RULES"

VALID_CATEGORIES = {"high_risk", "medium_risk", "stop_gate", "target_hint"}
VALID_ACTIONS = {
    "mark_high_risk",
    "mark_medium_risk",
    "require_stop_gate",
    "require_human_ack",
    "hint_target",
}
VALID_MATCH_MODES = {"substring", "word", "regex"}


class RiskRulesError(ValueError):
    """The risk rules file exists but cannot be trusted. Never degrade to empty."""


@dataclass(frozen=True)
class RiskRule:
    rule_id: str
    keyword: str
    category: str
    action: str
    match: str = "substring"
    target: str | None = None
    note: str | None = None

    def matches(self, lowered_text: str) -> bool:
        if self.match == "substring":
            return self.keyword.lower() in lowered_text
        if self.match == "word":
            return re.search(rf"(?<![\w-]){re.escape(self.keyword.lower())}(?![\w-])", lowered_text) is not None
        return re.search(self.keyword.lower(), lowered_text) is not None


@dataclass(frozen=True)
class RiskRules:
    rules: tuple[RiskRule, ...] = ()
    default_risk: str = "low"
    default_require_stop_gate: bool = False
    source: Path | None = None
    warnings: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:
        return bool(self.rules)

    def evaluate(self, text: str) -> dict[str, Any]:
        """Collect every matching rule; highest severity wins, actions union."""
        lowered = text.lower()
        matched = [rule for rule in self.rules if rule.matches(lowered)]
        risk = self.default_risk
        if any(rule.category == "high_risk" or rule.action == "mark_high_risk" for rule in matched):
            risk = "high"
        elif any(rule.category == "medium_risk" or rule.action == "mark_medium_risk" for rule in matched):
            risk = "medium"
        return {
            "risk": risk,
            "high": risk == "high",
            "medium": risk == "medium",
            "require_stop_gate": self.default_require_stop_gate
            or any(rule.action == "require_stop_gate" for rule in matched),
            "require_human_ack": any(rule.action == "require_human_ack" for rule in matched),
            "targets": tuple(
                dict.fromkeys(rule.target for rule in matched if rule.action == "hint_target" and rule.target)
            ),
            "matched": tuple(rule.rule_id for rule in matched),
        }


EMPTY_RULES = RiskRules()


def _require_mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RiskRulesError(f"{location}: expected a mapping")
    return value


def parse_risk_rules(text: str, source: Path | None = None) -> RiskRules:
    try:
        data = parse_yaml_mapping(text)
    except ProfileError as exc:
        raise RiskRulesError(f"{source or '<text>'}: {exc}") from exc

    unexpected = set(data) - {"version", "defaults", "rules"}
    if unexpected:
        raise RiskRulesError(f"{source or '<text>'}: unexpected top-level keys: {sorted(unexpected)}")
    if data.get("version") != 1:
        raise RiskRulesError(f"{source or '<text>'}: unsupported version {data.get('version')!r}, expected 1")

    defaults = _require_mapping(data.get("defaults", {}) or {}, "defaults")
    default_risk = defaults.get("risk", "low")
    if default_risk not in {"low", "medium", "high"}:
        raise RiskRulesError(f"defaults.risk must be low, medium, or high; got {default_risk!r}")
    default_gate = defaults.get("require_stop_gate", False)
    if not isinstance(default_gate, bool):
        raise RiskRulesError("defaults.require_stop_gate must be a boolean")

    raw_rules = _require_mapping(data.get("rules", {}) or {}, "rules")
    rules: list[RiskRule] = []
    for rule_id, body in raw_rules.items():
        location = f"rules.{rule_id}"
        entry = _require_mapping(body, location)
        unexpected = set(entry) - {"keyword", "category", "action", "match", "target", "note"}
        if unexpected:
            raise RiskRulesError(f"{location}: unexpected keys: {sorted(unexpected)}")
        keyword = entry.get("keyword", rule_id)
        if not isinstance(keyword, str) or not keyword.strip():
            raise RiskRulesError(f"{location}.keyword must be a non-empty string")
        category = entry.get("category")
        if category not in VALID_CATEGORIES:
            raise RiskRulesError(f"{location}.category must be one of {sorted(VALID_CATEGORIES)}; got {category!r}")
        action = entry.get("action")
        if action not in VALID_ACTIONS:
            raise RiskRulesError(f"{location}.action must be one of {sorted(VALID_ACTIONS)}; got {action!r}")
        match_mode = entry.get("match", "substring")
        if match_mode not in VALID_MATCH_MODES:
            raise RiskRulesError(f"{location}.match must be one of {sorted(VALID_MATCH_MODES)}; got {match_mode!r}")
        if match_mode == "regex":
            try:
                re.compile(keyword.lower())
            except re.error as exc:
                raise RiskRulesError(f"{location}.keyword is not a valid regex: {exc}") from exc
        target = entry.get("target")
        if target is not None and not isinstance(target, str):
            raise RiskRulesError(f"{location}.target must be a string")
        note = entry.get("note")
        if note is not None and not isinstance(note, str):
            raise RiskRulesError(f"{location}.note must be a string")
        rules.append(
            RiskRule(
                rule_id=str(rule_id),
                keyword=keyword,
                category=category,
                action=action,
                match=match_mode,
                target=target,
                note=note,
            )
        )
    return RiskRules(tuple(rules), default_risk, default_gate, source)


def load_risk_rules(path: Path | str | None = None, *, env: dict[str, str] | None = None) -> RiskRules:
    configured = path if path is not None else (env if env is not None else os.environ).get(RISK_RULES_ENV)
    if not configured:
        return EMPTY_RULES
    rules_path = Path(os.path.expanduser(str(configured)))
    if not rules_path.is_file():
        warning = f"risk rules file not found: {rules_path} (continuing with an empty ruleset)"
        print(f"agent-orch: {warning}", file=sys.stderr)
        return RiskRules(source=rules_path, warnings=(warning,))
    try:
        text = rules_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RiskRulesError(f"cannot read risk rules file {rules_path}: {exc}") from exc
    return parse_risk_rules(text, rules_path)
