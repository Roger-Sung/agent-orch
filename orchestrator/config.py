"""Optional config file: one place for the ORCH_* variables.

The engine is configured entirely through environment variables, which is the
right interface for a service manager and the wrong one for a human: a working
deployment needs half a dozen of them, and forgetting one — `ORCH_HOME` in
particular — does not fail, it silently lands state in a different directory.

This module lets a deployment write them down once, in a TOML file, and load
them at CLI startup. Deliberate properties:

* **The environment always wins.** A variable already set is never overridden,
  so launchd/CI overrides and one-off shell experiments keep working, and the
  file can never surprise a caller who set the variable on purpose.
* **Flat, not clever.** The file carries the same `ORCH_*` names the README
  and the engine use — no second vocabulary to learn or to drift.
* **The acknowledgement gates stay out.** `ORCH_ALLOW_UNATTENDED` and
  `ORCH_ALLOW_UNSANDBOXED` are refused in the file: both exist to record a
  deliberate per-deployment decision, and a value inherited from a config file
  nobody re-reads is exactly the failure they were designed against.

Search order: `$ORCH_CONFIG` if set (missing file is then an error, not a
skip), otherwise `~/.config/agent-orch/orch.toml` if present, otherwise no
file at all — the engine works with plain environment variables exactly as
before.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path


CONFIG_ENV = "ORCH_CONFIG"
DEFAULT_CONFIG_PATH = "~/.config/agent-orch/orch.toml"

#: Refused in the file, on purpose. See the module docstring.
ACKNOWLEDGEMENT_KEYS = frozenset({"ORCH_ALLOW_UNATTENDED", "ORCH_ALLOW_UNSANDBOXED"})


class ConfigFileError(ValueError):
    """The config file exists but cannot be used as written."""


def config_path(env: dict[str, str] | None = None) -> Path | None:
    """The config file to load, or None when the deployment has none."""
    source = env if env is not None else os.environ
    configured = source.get(CONFIG_ENV, "").strip()
    if configured:
        path = Path(os.path.expanduser(configured))
        if not path.is_file():
            raise ConfigFileError(f"{CONFIG_ENV} points at a missing file: {path}")
        return path
    default = Path(os.path.expanduser(DEFAULT_CONFIG_PATH))
    return default if default.is_file() else None


def _render(key: str, value: object) -> str:
    """One environment string per value; lists join with os.pathsep.

    Booleans are refused rather than guessed into "1"/"true"/"yes": every
    boolean-shaped ORCH_* variable is an acknowledgement gate that does not
    belong in the file anyway.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise ConfigFileError(f"{key}: booleans are not accepted in the config file")
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise ConfigFileError(f"{key}: lists may contain only strings")
        return os.pathsep.join(value)
    raise ConfigFileError(f"{key}: unsupported value type {type(value).__name__}")


def load_config_into_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Fill unset ORCH_* variables from the config file; return what was applied.

    Malformed content fails loudly instead of half-applying: a config file that
    silently loaded only some of its keys would be harder to debug than no
    config file at all.
    """
    target = env if env is not None else os.environ
    path = config_path(target)
    if path is None:
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigFileError(f"cannot read config file {path}: {exc}") from exc

    table = data.get("env", data)
    if not isinstance(table, dict):
        raise ConfigFileError(f"{path}: expected a table of ORCH_* keys")

    # Validate the whole table before touching the environment: applying keys
    # one at a time would leave a half-applied environment behind a raised
    # ConfigFileError, which is exactly the "silently loaded only some of its
    # keys" failure the docstring rules out.
    staged: dict[str, str] = {}
    for key, value in table.items():
        if not isinstance(key, str) or not key.startswith("ORCH_"):
            raise ConfigFileError(
                f"{path}: key {key!r} is not an ORCH_* variable; the file carries "
                "engine variables only, under their real names"
            )
        if key in ACKNOWLEDGEMENT_KEYS:
            raise ConfigFileError(
                f"{path}: {key} is refused in the config file. It is an acknowledgement "
                "of a per-deployment decision and must be set in the environment or on "
                "the command line, where whoever sets it is the one deciding."
            )
        staged[key] = _render(key, value)

    applied = {key: value for key, value in staged.items() if key not in target}
    target.update(applied)  # the environment always wins
    return applied
