"""A per-stage runner; never rewrites global provider commands or env.

Reviewers receive an evidence bundle and have no tools. Safe mode excludes
hooks/plugins/MCP, so read-only is an execution restriction, not a prompt wish.
Provider JSON is the authoritative channel, not display-stream marker parsing.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from contextlib import nullcontext

from .execution import ExecutionChoice, ExecutionConfigError, _unique_object
from .review_contract import review_prompt, validate_review
from . import review_session
from .runner import (
    CLAUDE_JSON_PROTOCOL, FINAL_RESPONSE_UNREADABLE, RunResult, SubprocessRunner,
    _codex_config_issue, provider_command,
)


def provider_json(output: str) -> dict:
    """A native result object, optionally surrounded by CLI diagnostic lines.

    Never choose the last of several result objects. Model content is nested
    in JSON's result string and is not interpreted as a second output channel.
    """
    try:
        payload = json.loads(output, object_pairs_hook=_unique_object)
    except ValueError:
        results = []
        for line in output.splitlines():
            try:
                item = json.loads(line, object_pairs_hook=_unique_object)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("type") == "result":
                results.append(item)
        if len(results) != 1:
            raise ExecutionConfigError("missing or ambiguous native result JSON")
        payload = results[0]
    if not isinstance(payload, dict) or payload.get("type") != "result":
        raise ExecutionConfigError("not a provider result")
    return payload


def configured_command(base: list[str], choice: ExecutionChoice) -> list[str]:
    """Accept the known native command shape, reject wrappers/ambiguous flags.

    Existing single model/effort choices are replaced, not appended. Reviewer
    bypass flags are deliberately removed by building a fresh safe argv. No
    unrecognised argument survives the opt-in boundary.
    """
    native = "codex" if choice.provider == "codex" else "claude"
    if not base or Path(base[0]).name not in {native, native + ".exe"}:
        raise ExecutionConfigError("execution config requires a native provider executable")
    expected = "exec" if native == "codex" else "-p"
    if len(base) < 2 or base[1] not in ({"exec"} if native == "codex" else {"-p", "--print"}):
        raise ExecutionConfigError(f"execution config requires {native} {expected}")
    seen: set[str] = set()
    i = 2
    while i < len(base):
        item = base[i]
        key, equal, value = item.partition("=")
        if key in {"--model", "-m", "--effort"}:
            canonical = "model" if key in {"--model", "-m"} else "effort"
            if canonical in seen:
                raise ExecutionConfigError(f"duplicate configured {canonical}")
            seen.add(canonical)
            if not equal:
                i += 1
                if i >= len(base) or base[i].startswith("-"):
                    raise ExecutionConfigError(f"missing configured {canonical}")
            elif not value:
                raise ExecutionConfigError(f"empty configured {canonical}")
        elif item in ({"--approve-for-me"} if native == "codex" else {"--dangerously-skip-permissions"}):
            if item in seen:
                raise ExecutionConfigError("duplicate execution flag")
            seen.add(item)
        else:
            raise ExecutionConfigError(f"unsupported configured argument: {item}")
        i += 1
    if choice.role == "reviewer":
        return [base[0], "-p", "--model", choice.model, "--effort", choice.effort,
                "--safe-mode", "--tools", "", "--output-format", "json"]
    return [base[0], "exec", "--model", choice.model,
            "-c", f'model_reasoning_effort="{choice.effort}"',
            "--sandbox", "danger-full-access", "-c", 'approval_policy="never"']


class ConfiguredRunner(SubprocessRunner):
    def __init__(self, choice: ExecutionChoice, plan_digest: str, base: list[str] | None = None):
        self.choice = choice
        # User-approved single sandbox: the runner must enforce orch L1 before
        # spawning an executor whose own nested sandbox is disabled.
        self.require_outer_sandbox = choice.role == "executor"
        self.plan_digest = plan_digest
        self.command = configured_command(base if base is not None else provider_command(choice.provider), choice)
        self.review_packet: dict | None = None
        self.session_binding: dict | None = None
        self.session_home: Path | None = None

    def bind_session(self, home: Path, series: str, expected: dict) -> None:
        record = review_session.inspect(home, series, expected)
        self.session_home = home
        self.session_binding = expected
        self.session_state = record["state"]
        self.working_directory = record["cwd"]
        if record.get("context_rehydrated"):
            from .review_contract import digest
            if self.review_packet is None:
                raise ExecutionConfigError("rehydration requires an evidence packet")
            self.review_candidate_base_sha = self.review_packet["candidate_sha256"]
            self.review_packet["evidence"]["historical_context"] = record["checkpoint"]
            self.review_packet["candidate_sha256"] = digest(self.review_packet["evidence"])
        self.command += ["--session-id" if record["state"] == "new" else "--resume", record["session_id"]]

    def _command(self, owner: str) -> list[str]:
        if owner != self.choice.provider:
            raise ExecutionConfigError("configured runner provider mismatch")
        return list(self.command)

    @staticmethod
    def _environment_issue(owner: str) -> str | None:
        # Native argv has already been validated. Do not inspect the *global*
        # ORCH_CODEX_COMMAND again, which is not this stage's command.
        if owner == "codex":
            return _codex_config_issue()
        return SubprocessRunner._environment_issue(owner)

    def run(self, owner: str, prompt: str, timeout: int, log_path: Path, *,
            workspace: Path | None = None, protected_roots: tuple[Path, ...] | None = None,
            reports_dir: Path | None = None) -> RunResult:
        if self.review_packet is not None:
            prompt += review_prompt(self.review_packet)
        session = self.session_binding
        lease = review_session.call(self.session_home, session["spec_series_id"], session, log_path, self.session_state) if session else nullcontext()
        with lease:
            # Tool-less reviewer runs at its fixed session cwd, not the RD
            # worktree. It can only inspect the supplied immutable packet.
            raw = super().run(owner, prompt, timeout, log_path, workspace=None if session else workspace,
                              protected_roots=protected_roots, reports_dir=reports_dir)
        receipt = {
            "schema_version": 1, "plan_digest": self.plan_digest,
            **self.choice.to_dict(), "invoked_argv": list(self.command),
            "invocation_verified": raw.containment_stop is None and raw.started_at_ms is not None,
            "provider_reported_model": None,
            "provider_effort_unreported": True, "provider_session_id": None,
            "session_binding": session,
            "sandbox_policy": "orch-l1-required-v1" if self.require_outer_sandbox else "tool-less-review-v1",
        }
        if raw.final_response_capture_path is not None:
            receipt["invoked_argv"] += ["--output-last-message", raw.final_response_capture_path]
        if self.choice.role != "reviewer":
            return replace(raw, execution_receipt=receipt)
        error = FINAL_RESPONSE_UNREADABLE
        text = None
        try:
            payload = provider_json(raw.output)
            if not isinstance(payload, dict) or payload.get("type") != "result":
                raise ValueError("not a provider result")
            if payload.get("is_error") is not False or payload.get("subtype") != "success":
                raise ValueError("provider did not succeed")
            usage = payload.get("modelUsage")
            if not isinstance(usage, dict) or self.choice.model not in usage:
                raise ValueError("requested reviewer model was not reported")
            model_entry = usage[self.choice.model]
            if not isinstance(model_entry, dict) or model_entry.get("canonicalModel") != self.choice.model:
                raise ValueError("reviewer model identity mismatch")
            text = payload.get("result")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty reviewer result")
            if session and payload.get("session_id") != session["session_id"]:
                raise ValueError("resume_failed: provider session mismatch")
            receipt["provider_reported_model"] = self.choice.model
            receipt["provider_session_id"] = payload.get("session_id")
            if self.review_packet is not None:
                receipt["review"] = validate_review(text, self.review_packet)
                receipt["candidate_sha256"] = self.review_packet["candidate_sha256"]
            error = None
        except (ValueError, TypeError, KeyError) as exc:
            receipt["verification_error"] = str(exc)
            text = None
        return replace(raw, final_response=text, final_response_source=CLAUDE_JSON_PROTOCOL,
                       final_response_error=error, execution_receipt=receipt)
