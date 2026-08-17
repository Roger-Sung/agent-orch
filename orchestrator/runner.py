from __future__ import annotations

import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    containment_root = log_path.parent / "containment"
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


def classify_result(
    exit_code: int | None,
    output: str,
    allowed_outcomes: set[str],
    timed_out: bool = False,
    *,
    source: RunResult | None = None,
) -> RunResult:
    telemetry = _telemetry_from(source)
    if source is not None and source.containment_stop is not None:
        # Containment outranks every other signal: a stage that escaped its
        # workspace, or never got a sandbox, must not be reported as a plain
        # non-zero exit — the operator needs to know which of the two happened.
        return RunResult(
            exit_code,
            output,
            None,
            "blocked",
            source.containment_stop,
            timed_out,
            containment_stop=source.containment_stop,
            containment_violations=source.containment_violations,
            **telemetry,
        )
    if timed_out:
        return RunResult(exit_code, output, None, "blocked", "timeout", True, **telemetry)
    if exit_code != 0:
        if any(pattern.search(output) for pattern in RATE_LIMIT_SIGNATURES):
            return RunResult(exit_code, output, None, "paused", "rate_limited", False, **telemetry)
        if any(pattern.search(output) for pattern in SOCKET_SIGNATURES):
            return RunResult(exit_code, output, None, "blocked", "provider_socket_error", False, **telemetry)
        return RunResult(exit_code, output, None, "blocked", "runner_nonzero", False, **telemetry)
    matches = OUTCOME_RE.findall(output)
    final_outcome = _final_outcome_marker(output)
    if final_outcome is not None:
        if final_outcome not in allowed_outcomes:
            return RunResult(exit_code, output, final_outcome, "blocked", "unknown_outcome", False, **telemetry)
        return RunResult(exit_code, output, final_outcome, "success", "success", False, **telemetry)
    distinct = set(matches)
    if not matches:
        return RunResult(exit_code, output, None, "blocked", "missing_outcome", False, **telemetry)
    if len(distinct) > 1:
        # Only genuinely conflicting outcomes are ambiguous; a repeated identical
        # value is common from agents, so take the last one.
        return RunResult(exit_code, output, None, "blocked", "ambiguous_outcome", False, **telemetry)
    outcome = matches[-1]
    if outcome not in allowed_outcomes:
        return RunResult(exit_code, output, outcome, "blocked", "unknown_outcome", False, **telemetry)
    return RunResult(exit_code, output, outcome, "success", "success", False, **telemetry)


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
        command = self._command(owner) + [prompt]
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
                    log_path.parent / "containment",
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
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_group(process)
                output, _ = process.communicate()
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
