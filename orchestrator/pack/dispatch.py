"""Spawning one pack-v1 provider call through the engine's own runner.

This is the join the adapters were missing.  `provider_adapters` decides what
to run; `SubprocessRunner` decides what the process is *allowed* to do - the L1
write allowlist, the protected roots, the sandbox decision, the live log.
Calling the adapter's argv directly with `subprocess.run`, as a harness might,
skips all of the second half: the producer would write wherever the ambient
process happened to be permitted, and nothing would be contained.

Per-role boundaries (IMPLEMENTATION-PLAN §3.1):

* **producer** writes the worktree plus its own operation temp and home;
* **reviewer / contract_review** write nothing - their sandbox is the provider's
  own read-only mode, and no extra root is granted here;
* **verify** writes the worktree, the artifacts root and its tool home.

The role is the only thing that decides which of these applies, so a call
cannot acquire a boundary by being dispatched from the wrong place.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence

from ..runner import RunResult, SubprocessRunner
from .errors import PackError
from .provider_adapters import ProviderAdapter, redact

# Roles that may write the candidate tree. A reviewer that could write it would
# be able to change the thing it is judging.
WRITERS = frozenset({"producer", "verify"})

# Where each provider keeps its own state - credentials, session transcripts.
# Engine-owned and separate from `{op_home}` on purpose (PLAN §3.4): the
# operation home is wiped between calls so no tool state carries over, but
# provider auth has to survive, and it is not something a contract should be
# able to point at.
PROVIDER_STATE_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}


class PackRunner(SubprocessRunner):
    """A runner whose command comes from a pack-v1 adapter.

    Subclassed rather than parameterised: `_command` is the runner's single
    decision about what to execute, and overriding it keeps every other
    behaviour - containment, the live stream, classification - exactly as the
    legacy path has it.
    """

    def __init__(self, argv: Sequence[str]) -> None:
        self._argv = list(argv)

    def _command(self, owner: str) -> list[str]:
        return list(self._argv)


class OperationPaths:
    """The per-operation directories an engine grants and then removes."""

    def __init__(self, root: Path, op_id: str) -> None:
        self.base = Path(root) / op_id
        self.tmp = self.base / "tmp"
        self.home = self.base / "home"
        self.artifacts = self.base / "artifacts"
        self.log = self.base / "run.log"
        self.stderr = self.base / "run.stderr"

    def create(self, *, home_seeds: Sequence[dict[str, str]] = (),
               package_root: Path | None = None) -> "OperationPaths":
        """Fresh directories per operation, so no state crosses between calls."""
        for path in (self.tmp, self.home, self.artifacts):
            path.mkdir(parents=True, exist_ok=True)
        for seed in home_seeds:
            if package_root is None:
                raise PackError("home_seeds declared without a package root")
            source = Path(package_root) / seed["package_relpath"]
            target = self.home / seed["relpath"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        return self


def build_env(policy_env: dict[str, Any], paths: OperationPaths,
              secrets: dict[str, str] | None = None,
              provider: str | None = None,
              provider_state: Path | None = None) -> dict[str, str]:
    """The child's whole environment, with placeholders resolved.

    Zero inheritance is the point (IDENTITIES §2.4): the child sees the
    contract's `set` values and nothing else, so an operator's shell cannot
    change what a sealed contract runs.  Placeholders are resolved to this
    operation's own directories, and those real paths stay out of every hash.
    """
    env = {}
    for name, value in (policy_env.get("set") or {}).items():
        rendered = str(value)
        rendered = rendered.replace("{op_tmp}", str(paths.tmp))
        rendered = rendered.replace("{op_home}", str(paths.home))
        rendered = rendered.replace("{artifacts_root}", str(paths.artifacts))
        env[name] = rendered
    for name in (policy_env.get("secret_refs") or {}):
        if not secrets or name not in secrets:
            raise PackError(f"secret {name} was declared but not supplied")
        env[name] = secrets[name]
    if provider is not None and provider_state is not None:
        key = PROVIDER_STATE_ENV.get(provider)
        if key is None:
            raise PackError(f"no provider state variable known for {provider!r}")
        # Injected by the engine, never by the contract: a contract that could
        # choose the credential directory could choose whose account runs.
        env[key] = str(Path(provider_state))
    return env


def write_roots(role: str, paths: OperationPaths) -> tuple[Path, ...]:
    """Extra writable roots for this role, beyond the workspace itself."""
    if role == "producer":
        return (paths.tmp, paths.home)
    if role == "verify":
        return (paths.tmp, paths.home, paths.artifacts)
    # Reviewer roles get none: the read-only sandbox is the provider's, and
    # granting a root here would quietly undo it.
    return ()


def dispatch(adapter: ProviderAdapter, *, role: str,
             prompt: str, workspace: Path, paths: OperationPaths,
             policy_env: dict[str, Any], secrets: dict[str, str] | None = None,
             timeout: int = 900, protected_roots: tuple[Path, ...] = (),
             resume_session: str | None = None,
             sandbox: str | None = None,
             provider_state: Path | None = None) -> RunResult:
    """Run one provider call under the engine's containment."""
    if role not in WRITERS and role not in {"reviewer", "contract_review"}:
        raise PackError(f"unknown pack role {role!r}")

    kwargs: dict[str, Any] = {"cwd": str(workspace)}
    if resume_session:
        kwargs["resume_session"] = resume_session
    if sandbox is not None:
        kwargs["sandbox"] = sandbox
    argv = adapter.command(**kwargs)

    env = build_env(policy_env, paths, secrets,
                    provider=adapter.provider, provider_state=provider_state)
    secret_values = adapter.secret_values(policy_env, secrets)

    runner = PackRunner(argv)
    return runner.run(
        adapter.provider,
        prompt,
        timeout,
        paths.log,
        workspace=workspace,
        protected_roots=protected_roots or None,
        stdin_payload=prompt,
        stderr_path=paths.stderr,
        env_override=env or None,
        redact_values=tuple(secret_values),
        # Provider state is writable too: the provider writes its own session
        # transcript there, which is how the engine later confirms the session.
        extra_write_roots=(write_roots(role, paths)
                           + ((Path(provider_state),) if provider_state else ())),
    )


def masked(text: str, policy_env: dict[str, Any], secrets: dict[str, str] | None) -> str:
    """Mask a string for anything the engine is about to persist itself."""
    values = [secrets[name] for name in (policy_env.get("secret_refs") or {})
              if secrets and name in secrets]
    return redact(text, values)
