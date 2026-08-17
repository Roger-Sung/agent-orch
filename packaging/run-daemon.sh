#!/usr/bin/env bash
# Daemon launcher: runs the orchestrator daemon in a clean, non-interactive
# environment. Intended to be started by a service manager at login, where a
# keychain and network are available; clients only enqueue, this process
# executes.
#
# This is a template. Copy it next to your own configuration and adjust the
# marked lines — it is deliberately free of machine-specific paths.
set -euo pipefail
cd "$(dirname "$0")/.."   # repository root

# A service manager's default PATH is usually minimal. Provider CLIs are often
# installed under a package manager prefix and may themselves shell out to a
# runtime (e.g. a `#!/usr/bin/env node` shebang), so the interpreter has to be
# findable too.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Shared ORCH_* definitions — the same file packaging/orch sources, so the CLI
# and the daemon cannot resolve different homes. Copy env.sh.template there
# and keep it outside version control.
ENV_FILE="${AGENT_ORCH_ENV:-$HOME/.config/agent-orch/env.sh}"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# POLICY, stated rather than silent: clear the Anthropic API overrides, so the
# Claude CLI runs on its own login state — an inherited override conflicts
# with a long-lived login token, and a daemon silently inheriting an API key
# runs unattended work on pay-per-token billing nobody decided on. Scope is
# exactly these two variables: the Codex CLI's endpoint and billing live in
# its own config files, which this launcher does not police. If your
# deployment authenticates with an API key or a custom endpoint on purpose,
# delete the next line and own that choice in your copy of this launcher.
unset ANTHROPIC_BASE_URL ANTHROPIC_API_KEY

# Unattended operation is opt-in, and deliberately noisy to enable.
#
# The commands below pass the provider CLIs' non-interactive permission flags,
# which is what makes a daemon possible at all: nobody is at the keyboard to
# approve each action. The consequence is that a stage acts with the full
# authority of this UNIX user - it can read anything you can read, and there is
# no process isolation layer (see docs/threat-model.md, L3). That is a decision
# for an operator to make on purpose, not something a launcher should assume, so
# this script refuses to start until it is stated.
if [ "${ORCH_ALLOW_UNATTENDED:-}" != "1" ]; then
    cat >&2 <<'MESSAGE'
run-daemon.sh: refusing to start.

This launcher runs provider CLIs with their approval prompts disabled, so
stages act unattended with the full authority of this UNIX user: they can read
anything this account can read, and nothing isolates them at the process level.

Set ORCH_ALLOW_UNATTENDED=1 to confirm you intend that, then start again.
MESSAGE
    exit 78  # EX_CONFIG
fi

# Model pins are required, not defaulted. A default here would keep working
# after a provider changes what its own default resolves to, and the run
# records would say one thing while a different model did the work. Fail
# instead, so the drift is visible at startup rather than in an audit later.
: "${ORCH_CLAUDE_MODEL:?must be set explicitly, e.g. ORCH_CLAUDE_MODEL=claude-opus-5}"
: "${ORCH_CODEX_MODEL:?must be set explicitly, e.g. ORCH_CODEX_MODEL=gpt-5.5}"

export ORCH_CLAUDE_MODEL ORCH_CODEX_MODEL
export ORCH_CLAUDE_COMMAND="${ORCH_CLAUDE_COMMAND:-$(command -v claude) -p --dangerously-skip-permissions --model $ORCH_CLAUDE_MODEL}"
export ORCH_CODEX_COMMAND="${ORCH_CODEX_COMMAND:-$(command -v codex) exec --approve-for-me --model $ORCH_CODEX_MODEL}"

exec python3 -m orchestrator daemon
