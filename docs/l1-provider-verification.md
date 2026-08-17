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

## Claude — pass

First attempted while the CLI's OAuth token was revoked; it failed to
authenticate *identically inside and outside* the sandbox, which established
only that L1 introduced no delta. Re-run after the operator re-authenticated:

| Check | Result |
|---|---|
| `claude -p` completes under the sandbox | yes, exit 0 (v2.1.226) |
| Model work unaffected | task "write ok into result.txt" produced the file in the workspace with the right content |
| Authentication under the sandbox | succeeded — credentials were read and the session authenticated normally |
| Provider state writes succeed | 4 files: `~/.claude/policy-limits.json`, `~/.claude/remote-settings.json`, a session transcript under `~/.claude/projects/<cwd-slug>/`, and an MCP log under `~/Library/Caches/claude-cli-nodejs/<cwd-slug>/` |
| Write outside the workspace, same profile | refused, exit 1, target file unchanged |

**No allowlist changes were needed.** Two details worth recording:

*The cwd-derived slug directories were the right call.* Both
`~/.claude/projects/` and `~/Library/Caches/claude-cli-nodejs/` get a
subdirectory named after the working directory of the run. Allowlisting
specific subdirectories would have failed for every new workspace; allowing
the parent wholesale is what makes this work at all.

*Credential handling is unaffected because L1 restricts writes only.* The
profile is `(allow default)` with `file-write*` denied and re-allowed for the
allowlist, so keychain access, mach services, and network egress are
untouched. A token refresh writing into `~/.claude` lands inside the
allowlist; a refresh going through the system keychain is not a file write at
all. The run above did not itself trigger a refresh — the token was fresh — so
that specific path remains inferred rather than observed, but it is inferred
from the profile's shape rather than from a directory walk.

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
