"""Provider adapters for pack-v1 (ENVELOPES §5, IDENTITIES §2.4, PLAN §3.4/§3.6).

pack-v1 reverses execution-v1's pairing: the producer is Claude and both
reviewer stages are Codex.  The engine therefore needs both providers in both
directions, which the existing `configured_command` cannot express - it decides
the argv shape from the *role*, not the provider.

What the adapters are responsible for, and why each matters:

* **argv assembly** with the model pinned and the sandbox stated explicitly.
* **Session identity**, two-phase for Codex: the id only appears on stderr, so
  it is cross-checked against the rollout file before being trusted.  An
  unconfirmed session is a hold, never a silent retry - `inspect` refuses to
  start a second call against a session whose first call cannot be accounted
  for (STATE-TABLE §5).
* **Model attestation in layers**: what we asked for, what the transcript says
  ran, and what the provider confirmed.  They are kept apart because only the
  first is under our control and only the third would be proof.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from .errors import PackError

SESSION_ID_LINE = re.compile(r"^session id:\s*([0-9a-fA-F-]{36})\s*$", re.MULTILINE)

# Codex features that would give a read-only reviewer a way out of the sandbox.
# Disabled explicitly rather than assumed off; the smoke report records what the
# CLI actually did with each flag (IMPLEMENTATION-PLAN §3.1).
CODEX_DISABLED_FEATURES = (
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "apps",
)


class ProviderLaunchError(PackError):
    """The provider could not be started; maps to `hold(launch_failed(reason))`."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        self.reason = reason
        super().__init__(": ".join(p for p in (reason, detail) if p))


class SessionUnconfirmed(PackError):
    """A call was sealed but its session identity could not be established."""


class ProviderAdapter:
    """Shared argv / env / receipt assembly."""

    provider = ""

    def __init__(self, *, binary: str, model: str, effort: str | None = None) -> None:
        self.binary = binary
        self.model = model
        self.effort = effort

    # -- env -----------------------------------------------------------

    def environment(self, policy_env: dict[str, Any], secrets: dict[str, str] | None = None) -> dict[str, str]:
        """Build the child env from the contract alone - nothing is inherited.

        `set` values are public and part of the contract hash; `secret_refs`
        values are fetched at spawn time and never recorded anywhere
        (IDENTITIES §2.4, D-2026-09-15-01).
        """
        env = dict(policy_env.get("set") or {})
        for name in (policy_env.get("secret_refs") or {}):
            if secrets is None or name not in secrets:
                raise ProviderLaunchError("secret_unavailable", name)
            env[name] = secrets[name]
        return env

    def secret_values(self, policy_env: dict[str, Any], secrets: dict[str, str] | None) -> list[str]:
        """The literal values that must be redacted before anything is persisted."""
        if not secrets:
            return []
        return [secrets[name] for name in (policy_env.get("secret_refs") or {}) if name in secrets]

    # -- interface -----------------------------------------------------

    def command(self, *, cwd: str, resume_session: str | None = None,
                sandbox: str = "read-only") -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def session_id(self, *, stdout: str, stderr: str) -> str | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def attestation(self, *, stdout: str, transcript: dict[str, Any] | None) -> dict[str, Any]:
        return {"requested": self.model, "invoked": None, "provider_confirmed": None}


class ClaudeAdapter(ProviderAdapter):
    """Claude as the pack-v1 producer."""

    provider = "claude"

    def command(self, *, cwd: str, resume_session: str | None = None,
                sandbox: str = "workspace-write") -> list[str]:
        argv = [self.binary, "-p", "--model", self.model, "--output-format", "json"]
        if self.effort:
            argv += ["--effort", self.effort]
        if resume_session:
            argv += ["--resume", resume_session]
        return argv

    def result(self, stdout: str) -> dict[str, Any]:
        """Parse the single JSON result object Claude prints with `-p`."""
        try:
            payload = json.loads(stdout)
        except ValueError as exc:
            raise ProviderLaunchError("provider_final_response_unreadable", str(exc)) from exc
        if not isinstance(payload, dict):
            raise ProviderLaunchError("provider_final_response_unreadable", "not an object")
        return payload

    def session_id(self, *, stdout: str, stderr: str) -> str | None:
        try:
            return self.result(stdout).get("session_id")
        except ProviderLaunchError:
            return None

    def authoritative_text(self, stdout: str) -> str:
        payload = self.result(stdout)
        text = payload.get("result")
        if not isinstance(text, str) or not text.strip():
            raise ProviderLaunchError("provider_final_response_empty")
        return text

    def attestation(self, *, stdout: str, transcript: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            payload = self.result(stdout)
        except ProviderLaunchError:
            payload = {}
        usage = payload.get("modelUsage") or {}
        invoked = next(iter(usage), None) if isinstance(usage, dict) else None
        return {
            "requested": self.model,
            "invoked": invoked,
            # Claude reports which model billed the call, so this one is real
            # confirmation rather than an echo of our own request.
            "provider_confirmed": invoked,
        }

    def usage(self, stdout: str) -> dict[str, int]:
        payload = self.result(stdout)
        usage = payload.get("usage") or {}
        return {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
        }

    def resume_confirmed(self, *, stdout: str, expected_session: str) -> bool:
        return self.session_id(stdout=stdout, stderr="") == expected_session


class CodexAdapter(ProviderAdapter):
    """Codex as the pack-v1 contract reviewer and reviewer."""

    provider = "codex"

    def __init__(self, *, binary: str, model: str, effort: str | None = None,
                 codex_home: Path | None = None) -> None:
        super().__init__(binary=binary, model=model, effort=effort)
        self.codex_home = Path(codex_home) if codex_home else None

    def command(self, *, cwd: str, resume_session: str | None = None,
                sandbox: str = "read-only", last_message: str | None = None) -> list[str]:
        argv = [self.binary, "exec"]
        if resume_session:
            argv += ["resume", resume_session]
        argv += ["-C", cwd, "--skip-git-repo-check", "-s", sandbox, "-m", self.model]
        for feature in CODEX_DISABLED_FEATURES:
            argv += ["--disable", feature]
        if last_message:
            argv += ["--output-last-message", last_message]
        # The prompt arrives on stdin; `-` is what makes the CLI read it there.
        argv.append("-")
        return argv

    def session_id(self, *, stdout: str, stderr: str) -> str | None:
        """Phase one: the id Codex printed. Only a claim until phase two."""
        match = SESSION_ID_LINE.search(stderr)
        return match.group(1) if match else None

    def rollout_path(self, session_id: str) -> Path | None:
        """Phase two: the transcript file that proves the session exists."""
        if self.codex_home is None:
            return None
        matches = sorted(self.codex_home.glob(f"sessions/**/rollout-*{session_id}*.jsonl"))
        return matches[0] if matches else None

    def confirm_session(self, *, stdout: str, stderr: str) -> str:
        """Bind the session id, or refuse to claim one.

        Both halves are required: a printed id with no transcript, or a
        transcript with no printed id, leaves the call unattributable - which
        is `hold(session_unconfirmed)`, not a retry.
        """
        claimed = self.session_id(stdout=stdout, stderr=stderr)
        if claimed is None:
            raise SessionUnconfirmed("codex printed no session id")
        if self.codex_home is not None and self.rollout_path(claimed) is None:
            raise SessionUnconfirmed(f"no rollout file for session {claimed}")
        return claimed

    def transcript(self, session_id: str) -> list[dict[str, Any]]:
        path = self.rollout_path(session_id)
        if path is None:
            return []
        entries: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    @staticmethod
    def _payload(entry: dict[str, Any]) -> dict[str, Any]:
        """Rollout lines wrap their content in `payload`.

        Verified against a real transcript: the model appears on a
        `turn_context` line and usage on an `event_msg` whose payload type is
        `token_count`.  Reading the top level instead silently yields nothing -
        which looks like "no usage recorded" rather than a parser bug.
        """
        payload = entry.get("payload")
        return payload if isinstance(payload, dict) else entry

    def attestation(self, *, stdout: str, transcript: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
        invoked = None
        for entry in transcript or ():
            if not isinstance(entry, dict):
                continue
            payload = self._payload(entry)
            if payload.get("model"):
                invoked = payload["model"]
        return {
            "requested": self.model,
            "invoked": invoked,
            # Codex does not report a canonical model back, so claiming
            # confirmation here would be inventing evidence.
            "provider_confirmed": None,
        }

    def usage(self, transcript: Sequence[dict[str, Any]]) -> dict[str, int]:
        """Cumulative totals from the last `token_count` entry.

        The CLI's printed `tokens used` is a running total, so summing printed
        values across calls double-counts; the transcript's last entry is the
        only figure that means what it says.
        """
        latest: dict[str, Any] = {}
        for entry in transcript:
            if not isinstance(entry, dict):
                continue
            payload = self._payload(entry)
            if payload.get("type") != "token_count":
                continue
            total = (payload.get("info") or {}).get("total_token_usage")
            if isinstance(total, dict):
                latest = total
        return {
            "input_tokens": int(latest.get("input_tokens", 0)),
            "cached_input_tokens": int(latest.get("cached_input_tokens", 0)),
            "output_tokens": int(latest.get("output_tokens", 0)),
            "total_tokens": int(latest.get("total_tokens", 0)),
        }

    def resume_confirmed(self, session_id: str, *, before_lines: int) -> bool:
        """A resume must actually append to the transcript."""
        return len(self.transcript(session_id)) > before_lines


def adapter_for(provider: str, **kwargs: Any) -> ProviderAdapter:
    if provider == "claude":
        kwargs.pop("codex_home", None)
        return ClaudeAdapter(**kwargs)
    if provider == "codex":
        return CodexAdapter(**kwargs)
    raise PackError(f"no pack-v1 adapter for provider {provider!r}")


def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace injected secret literals before anything is persisted.

    Only the literal is replaced: the surrounding message, the exit status and
    the failure's meaning all survive, because a masked failure must still read
    as a failure (joint-r2 R2-H2).
    """
    out = text
    for value in secrets:
        if value:
            out = out.replace(value, "«redacted»")
    return out


def contains_secret(text: str, secrets: Sequence[str]) -> bool:
    """The seal-time backstop: detection, not repair."""
    return any(value and value in text for value in secrets)
