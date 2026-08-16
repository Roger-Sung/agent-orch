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

# Headless credentials, if any. Keep this file outside version control.
# [ -f "<YOUR_ENV_FILE>" ] && { set -a; . "<YOUR_ENV_FILE>"; set +a; }

# Force the official endpoints and drop inherited overrides, which otherwise
# conflict with a long-lived token.
unset ANTHROPIC_BASE_URL ANTHROPIC_API_KEY

# Non-interactive permission flags, and explicit model pins so that run
# provenance is auditable instead of following whatever the CLI defaults to.
export ORCH_CLAUDE_MODEL="${ORCH_CLAUDE_MODEL:-claude-opus-5}"
export ORCH_CODEX_MODEL="${ORCH_CODEX_MODEL:-gpt-5.5}"
export ORCH_CLAUDE_COMMAND="${ORCH_CLAUDE_COMMAND:-$(command -v claude) -p --dangerously-skip-permissions --model $ORCH_CLAUDE_MODEL}"
export ORCH_CODEX_COMMAND="${ORCH_CODEX_COMMAND:-$(command -v codex) exec --approve-for-me --model $ORCH_CODEX_MODEL}"

exec python3 -m orchestrator daemon
