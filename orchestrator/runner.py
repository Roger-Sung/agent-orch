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

# Worktree + Git containment：把 stage 的工作目錄固定在 worktree，並拿掉所有推送憑證。
# 這是強制力，不是 prompt 裡的約定——agent 就算想 push 也沒有憑證、hook 也會擋。
#
# 明確不是 sandbox：agent 仍以同一個 UNIX user 執行，可以讀寫 worktree 以外的檔案、
# 也可以對外連網。真正的隔離需要獨立 executor identity（見 docs/execution-containment.md）。
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
    """替一次 stage run 準備 worktree + Git containment，回傳要給子行程的 env。

    這不是 process sandbox：同一個 UNIX user、同一個檔案系統、同樣可以連外網。
    它只保證「工作發生在 worktree 裡」與「結果不能經由 Git 離開」。

    三層阻斷 push，任何一層失效另外兩層仍在：
      1. 環境沒有 SSH agent / token（HTTPS 與 SSH 都拿不到憑證）
      2. GIT_SSH_COMMAND / GIT_ASKPASS 指向 /usr/bin/false，不會有互動式補救
      3. 專用 core.hooksPath 裡的 pre-push 一律 reject

    commit 仍然可以——containment 限制的是「讓結果經由 Git 離開 worktree」，不是「不准做事」。
    """
    containment_root = log_path.parent / "containment"
    hooks_dir = containment_root / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    pre_push = hooks_dir / "pre-push"
    pre_push.write_text(CONTAINMENT_PRE_PUSH_HOOK, encoding="utf-8")
    pre_push.chmod(0o755)

    gitconfig = containment_root / "gitconfig"
    identity = _git_identity()
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


def _git_identity() -> dict[str, str]:
    """保留 commit 身分：containment gitconfig 取代 global config，沒有它 commit 會直接失敗。"""
    identity = {"name": "agent-orch", "email": "orchestrator@orch.invalid"}
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
    return identity


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
        # 真的吐出互相矛盾的 outcome 才算模糊；重複同一個值（agent 常見）取最後一個。
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
    ) -> RunResult:
        command = self._command(owner) + [prompt]
        containment_env = prepare_containment(workspace, log_path) if workspace is not None else None
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
                stdin=subprocess.DEVNULL,  # 否則 claude -p 等 stdin EOF → 卡死直到 timeout
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
            model=SubprocessRunner._model_from_command(command) or "unspecified",
            usage_input_tokens=usage["input_tokens"],
            usage_output_tokens=usage["output_tokens"],
            usage_total_tokens=usage["total_tokens"],
            usage_unavailable_reason=usage["unavailable_reason"],
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
