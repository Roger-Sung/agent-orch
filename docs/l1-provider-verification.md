# L1 verification with real provider CLIs

Date: 2026-08-17. Host: macOS (Darwin 25.5), `sandbox-exec` present.

Until now the L1 write allowlist came from an *observed-write probe* — walking
the provider CLIs' state directories and inferring what they touch. That is a
guess, and the failure mode of a wrong guess is nasty: a stage that fails
deep inside a provider CLI for a reason the operator cannot see. This is the
record of running the real CLIs inside the generated sandbox profile.

## Method

For each provider: create a workspace, an artifact directory, and a sibling
`protected/` directory containing a file; generate the profile with
`build_sandbox_profile(workspace, artifacts)`; run the CLI under
`sandbox-exec -f <profile>` with the workspace as the working directory;
then, using the *same* profile, attempt a write into `protected/`.

Fixtures live under the repository, not `/tmp` — `/tmp` and `$TMPDIR` are on
the allowlist by necessity, so a protected directory there would prove
nothing.

## Codex — pass

| Check | Result |
|---|---|
| `codex exec` completes under the sandbox | yes, exit 0 (v0.147.0, two runs) |
| Model work unaffected | task "write ok into result.txt" produced the file in the workspace with the right content |
| Provider state writes succeed | 18 files under `~/.codex` written during the run |
| SQLite family | `logs_2.sqlite`, `logs_2.sqlite-wal`, `goals_1.sqlite`, `memories_1.sqlite` all written — the `-wal` sibling matters, denying it corrupts rather than blocks |
| Caches | `~/.codex/cache/codex_apps_server_info/`, `codex_apps_tools/`, `remote_plugin_catalog/` all written |
| Write outside the workspace, same profile | refused, exit 1, target file unchanged |

**No allowlist changes were needed.** The probe-derived list was complete for
this CLI. Worth noting: Codex applies its own `workspace-write` sandbox on top
(it reports `sandbox: workspace-write [workdir, /tmp, $TMPDIR]`), so the two
layers nest without conflict.

## Claude — inconclusive, for a reason unrelated to the sandbox

`claude -p` exits 1 with:

```
Failed to authenticate. API Error: 401 OAuth access token has been revoked.
```

The same command fails identically **outside** the sandbox, so this is a
pre-existing authentication problem on the host, not something L1 caused. What
the run does establish is that the CLI started, read its configuration, and
reached the network — file reads and egress are unaffected by the profile —
and that the sandbox introduced no observable difference in behaviour.

What it does **not** establish is the part that most needs establishing: the
write paths a *successful* Claude session touches, in particular anything the
CLI writes while refreshing a token. That is exactly the class of write an
observed-write probe cannot predict, since it only happens on a live auth
path.

**Open item for M2:** re-run this check once the CLI is authenticated again.
Until then, the Claude row of the allowlist remains probe-derived. The
practical risk is bounded — a missing path shows up as a stage failure, not as
silent damage, and L2 still watches independently — but it is not verified.

## Reproducing

```sh
python3 - <<'PY'
import subprocess, tempfile
from pathlib import Path
from orchestrator.containment import build_sandbox_profile, SANDBOX_EXEC
base = Path(tempfile.mkdtemp(prefix=".containment-test-", dir=Path.cwd()))
ws = base / "workspace"; ws.mkdir()
art = base / "artifacts"; art.mkdir()
prof = art / "sandbox.sb"; prof.write_text(build_sandbox_profile(ws, art))
print(subprocess.run([SANDBOX_EXEC, "-f", str(prof), "codex", "exec",
                      "--skip-git-repo-check", "Reply with the single word: ok"],
                     cwd=str(ws), capture_output=True, text=True).returncode)
PY
```
