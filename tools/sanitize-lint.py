#!/usr/bin/env python3
"""Forbidden-pattern scanner for pre-publication sanitization.

Rules live in the repository (``risk-patterns.yaml``); concrete sensitive
values never do. Site-specific literals (operator name, home directory,
company domain, ...) are supplied at scan time via ``--secrets-file``, which
is expected to stay outside version control.

Exit codes:
    0  no violations
    1  at least one violation
    2  usage / configuration error (unreadable rules, bad regex, or a
       `requires_secrets` rule left uncovered)

Design notes:
    * stdlib only, no third-party dependency.
    * Fail-closed by default: a rule that can only match with site-local
      literals must be covered by --secrets-file, otherwise the run exits 2
      instead of reporting a clean tree it never actually checked.
      --no-strict-secrets downgrades that to a warning; it is for exploratory
      runs, never for a release gate.
    * Directory walks honour the exclude lists in the rules file; a file
      passed explicitly as an argument is always scanned, so the golden
      fixture can be linted on purpose without disabling the excludes.
    * Reported snippets are masked. The tool never prints a value that came
      from the secrets file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RULES = "risk-patterns.yaml"
MAX_FILE_BYTES = 5 * 1024 * 1024


# --------------------------------------------------------------------------
# Minimal YAML subset parser (mappings, lists, scalars, nesting by indent).
# Deliberately strict: unsupported syntax raises instead of guessing.
# --------------------------------------------------------------------------


class ConfigError(Exception):
    pass


def _strip_comment(raw: str) -> str:
    text = raw.strip()
    if not text or text.startswith("#"):
        return ""
    if text[0] in "'\"":
        quote = text[0]
        end = text.find(quote, 1)
        while end != -1 and text[end - 1] == "\\":
            end = text.find(quote, end + 1)
        if end == -1:
            raise ConfigError(f"unterminated quote: {raw!r}")
        tail = text[end + 1 :].strip()
        if tail and not tail.startswith("#"):
            raise ConfigError(f"trailing content after quoted scalar: {raw!r}")
        return text[: end + 1]
    idx = text.find(" #")
    if idx != -1:
        text = text[:idx].rstrip()
    return text


def _scalar(text: str):
    if not text:
        return None
    if text[0] in "'\"" and text[-1] == text[0] and len(text) >= 2:
        return text[1:-1].replace("\\'", "'").replace('\\"', '"')
    lowered = text.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _tokenize(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ConfigError(f"line {lineno}: tab indentation is not supported")
        indent = len(raw) - len(raw.lstrip())
        out.append((indent, raw.strip()))
    return out


def _parse_block(tokens: list[tuple[int, str]], pos: int, indent: int):
    if pos >= len(tokens):
        return None, pos
    if tokens[pos][1].startswith("- "):
        return _parse_list(tokens, pos, indent)
    return _parse_map(tokens, pos, indent)


def _parse_list(tokens, pos, indent):
    items = []
    while pos < len(tokens):
        cur_indent, cur = tokens[pos]
        if cur_indent < indent or not cur.startswith("- "):
            break
        body = _strip_comment(cur[2:])
        pos += 1
        if ":" in body and not (body[:1] in "'\"") and re.match(r"^[A-Za-z_][\w.-]*:", body):
            key, _, rest = body.partition(":")
            item = {}
            rest = _strip_comment(rest)
            if rest:
                item[key.strip()] = _scalar(rest)
            else:
                child, pos = _parse_block(tokens, pos, cur_indent + 2 + 1)
                item[key.strip()] = child
            while pos < len(tokens) and tokens[pos][0] > cur_indent and not tokens[pos][1].startswith("- "):
                sub, pos = _parse_map_entry(tokens, pos, tokens[pos][0])
                item.update(sub)
            items.append(item)
        else:
            items.append(_scalar(body))
    return items, pos


def _parse_map_entry(tokens, pos, indent):
    cur_indent, cur = tokens[pos]
    if ":" not in cur:
        raise ConfigError(f"expected 'key: value', got {cur!r}")
    key, _, rest = cur.partition(":")
    key = key.strip()
    rest = _strip_comment(rest)
    pos += 1
    if rest:
        return {key: _scalar(rest)}, pos
    if pos < len(tokens) and tokens[pos][0] > cur_indent:
        child, pos = _parse_block(tokens, pos, tokens[pos][0])
        return {key: child}, pos
    if pos < len(tokens) and tokens[pos][0] == cur_indent and tokens[pos][1].startswith("- "):
        child, pos = _parse_list(tokens, pos, cur_indent)
        return {key: child}, pos
    return {key: None}, pos


def _parse_map(tokens, pos, indent):
    result: dict = {}
    while pos < len(tokens):
        cur_indent = tokens[pos][0]
        if cur_indent < indent:
            break
        if tokens[pos][1].startswith("- "):
            break
        entry, pos = _parse_map_entry(tokens, pos, cur_indent)
        result.update(entry)
    return result, pos


def parse_yaml_subset(text: str) -> dict:
    tokens = _tokenize(text)
    if not tokens:
        return {}
    data, pos = _parse_block(tokens, 0, tokens[0][0])
    if pos != len(tokens):
        raise ConfigError(f"unparsed content starting at {tokens[pos][1]!r}")
    if not isinstance(data, dict):
        raise ConfigError("rules file must be a mapping at top level")
    return data


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    severity: str
    description: str
    regex: re.Pattern | None
    allow: tuple[re.Pattern, ...]
    mask: str
    requires_secrets: bool
    from_secrets: bool = False


@dataclass(frozen=True)
class Violation:
    path: str
    lineno: int
    rule_id: str
    category: str
    severity: str
    masked: str


def _compile(expr: str, where: str) -> re.Pattern:
    try:
        return re.compile(expr)
    except re.error as exc:
        raise ConfigError(f"{where}: invalid regex {expr!r}: {exc}") from exc


def load_rules(path: Path) -> tuple[list[Rule], dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read rules file {path}: {exc}") from exc
    data = parse_yaml_subset(raw)
    entries = data.get("patterns") or []
    if not isinstance(entries, list) or not entries:
        raise ConfigError("rules file defines no patterns")
    rules: list[Rule] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError(f"pattern entry must be a mapping: {entry!r}")
        rule_id = entry.get("id")
        if not rule_id:
            raise ConfigError(f"pattern entry without id: {entry!r}")
        if rule_id in seen:
            raise ConfigError(f"duplicate pattern id: {rule_id}")
        seen.add(rule_id)
        requires_secrets = bool(entry.get("requires_secrets"))
        expr = entry.get("regex")
        if expr is None and not requires_secrets:
            raise ConfigError(f"pattern {rule_id} has neither regex nor requires_secrets")
        allow_list = entry.get("allow") or []
        if isinstance(allow_list, str):
            allow_list = [allow_list]
        rules.append(
            Rule(
                rule_id=str(rule_id),
                category=str(entry.get("category") or "uncategorized"),
                severity=str(entry.get("severity") or "high"),
                description=str(entry.get("description") or ""),
                regex=_compile(str(expr), f"pattern {rule_id}") if expr else None,
                allow=tuple(_compile(str(a), f"pattern {rule_id} allow") for a in allow_list),
                mask=str(entry.get("mask") or ""),
                requires_secrets=requires_secrets,
            )
        )
    return rules, data


def load_secret_rules(path: Path) -> list[Rule]:
    """Site-local literals. Values are never echoed; matches mask to '***'."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read secrets file {path}: {exc}") from exc
    rules: list[Rule] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "~=" in text:
            key, _, value = text.partition("~=")
            expr = value.strip()
        elif "=" in text:
            key, _, value = text.partition("=")
            expr = re.escape(value.strip())
        else:
            raise ConfigError(f"secrets file line {lineno}: expected 'id=value' or 'id~=regex'")
        key = key.strip()
        if not key or not expr:
            raise ConfigError(f"secrets file line {lineno}: empty id or value")
        rules.append(
            Rule(
                rule_id=key,
                category="site-secret",
                severity="high",
                description="site-local secret literal",
                regex=_compile(expr, f"secrets file line {lineno}"),
                allow=(),
                mask="***",
                requires_secrets=False,
                from_secrets=True,
            )
        )
    return rules


def check_secret_coverage(rules: list[Rule], secret_rules: list[Rule]) -> list[str]:
    provided = {r.rule_id for r in secret_rules}
    missing = []
    for rule in rules:
        if not rule.requires_secrets:
            continue
        covered = any(pid == rule.rule_id or pid.startswith(rule.rule_id + ".") for pid in provided)
        if not covered:
            missing.append(rule.rule_id)
    return missing


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


def mask_match(rule: Rule, text: str) -> str:
    if rule.mask:
        return rule.mask
    if len(text) <= 6:
        return "***"
    return f"{text[:3]}***{text[-2:]}"


def scan_text(rel: str, text: str, rules: list[Rule]) -> list[Violation]:
    found: list[Violation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule in rules:
            if rule.regex is None:
                continue
            for match in rule.regex.finditer(line):
                value = match.group(0)
                if any(a.search(value) for a in rule.allow):
                    continue
                found.append(
                    Violation(
                        path=rel,
                        lineno=lineno,
                        rule_id=rule.rule_id,
                        category=rule.category,
                        severity=rule.severity,
                        masked=mask_match(rule, value),
                    )
                )
    return found


def iter_files(target: Path, config: dict, root: Path) -> list[Path]:
    if target.is_file():
        return [target]
    exclude_dirs = set(config.get("exclude_dirs") or [])
    exclude_suffixes = tuple(config.get("exclude_suffixes") or [])
    exclude_paths = [str(p) for p in (config.get("exclude_paths") or [])]
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if exclude_suffixes and path.name.endswith(exclude_suffixes):
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = path.as_posix()
            if any(_glob_match(rel, pattern) for pattern in exclude_paths):
                continue
            files.append(path)
    return files


def _glob_match(rel: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(rel, pattern) or fnmatch(rel, pattern.rstrip("/") + "/*")


def scan(targets: list[Path], rules: list[Rule], config: dict, root: Path) -> tuple[list[Violation], list[str]]:
    violations: list[Violation] = []
    skipped: list[str] = []
    for target in targets:
        for path in iter_files(target, config, root):
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = path.as_posix()
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    skipped.append(f"{rel} (too large)")
                    continue
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                skipped.append(f"{rel} (binary)")
                continue
            except OSError as exc:
                skipped.append(f"{rel} ({exc.strerror})")
                continue
            violations.extend(scan_text(rel, text, rules))
    return violations, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for forbidden patterns before publication.")
    parser.add_argument("targets", nargs="*", default=None, help="files or directories (default: repo root)")
    parser.add_argument("--rules", default=None, help=f"rules file (default: <repo>/{DEFAULT_RULES})")
    parser.add_argument("--secrets-file", default=None, help="site-local literals, never committed")
    parser.add_argument("--root", default=None, help="path root used for relative reporting")
    parser.add_argument(
        "--strict-secrets",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="fail (exit 2) when a requires_secrets rule has no --secrets-file coverage; "
        "on by default, --no-strict-secrets downgrades it to a warning",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--summary-only", action="store_true", help="print per-rule counts only")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    rules_path = Path(args.rules) if args.rules else repo_root / DEFAULT_RULES
    targets = [Path(t).resolve() for t in args.targets] if args.targets else [repo_root]
    root = Path(args.root).resolve() if args.root else (targets[0] if len(targets) == 1 and targets[0].is_dir() else repo_root)

    try:
        rules, config = load_rules(rules_path)
        secret_rules = load_secret_rules(Path(args.secrets_file)) if args.secrets_file else []
        missing = check_secret_coverage(rules, secret_rules)
    except ConfigError as exc:
        print(f"sanitize-lint: config error: {exc}", file=sys.stderr)
        return 2

    if missing:
        note = "uncovered secret-backed rules: " + ", ".join(sorted(missing))
        if args.strict_secrets:
            print(
                f"sanitize-lint: {note}\n"
                "sanitize-lint: refusing to report a result for rules that were never checked; "
                "supply --secrets-file, or pass --no-strict-secrets to accept a partial scan",
                file=sys.stderr,
            )
            return 2
        print(f"sanitize-lint: WARNING partial scan, {note}", file=sys.stderr)

    violations, skipped = scan(targets, rules + secret_rules, config, root)

    counts: dict[str, int] = {}
    for v in violations:
        counts[v.rule_id] = counts.get(v.rule_id, 0) + 1

    if args.json:
        print(
            json.dumps(
                {
                    "violations": [v.__dict__ for v in violations],
                    "counts": counts,
                    "skipped": skipped,
                    "uncovered_secret_rules": sorted(missing),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        if not args.summary_only:
            for v in violations:
                print(f"{v.path}:{v.lineno}: [{v.severity}] {v.rule_id} ({v.category}): {v.masked}")
        if violations:
            print("-- summary --")
            for rule_id in sorted(counts, key=lambda k: (-counts[k], k)):
                print(f"{rule_id}: {counts[rule_id]}")
            print(f"total: {len(violations)} violation(s) in {len({v.path for v in violations})} file(s)")
        else:
            print("sanitize-lint: clean")

    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
