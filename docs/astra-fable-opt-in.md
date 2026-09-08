# Astra / Fable opt-in execution

Status: implemented and deployed in the reference installation; still opt-in.
Other installations require their own deployment verification. This is not a
supported-product or universal deployment claim.

The original Astra conversation owns exploration, the spec, RD assignment,
progress and arbitration. The controller does not create another Astra planner.
One Fable session reviews a spec series. Technical PASS is not user approval.
Legacy requests without an execution plan retain their existing route.

## External draft review

Register a session under the same runtime owner that will execute it. The
canonical meeting directory must exist and remain available. Registration and
status inspection do not call a model. To import a meeting session, pass a saved
successful native Claude JSON receipt with `--receipt`; local session metadata
for this exact canonical directory is also required.

```sh
python3 -m orchestrator review-session-register series-a --cwd "$REVIEW_CWD"
python3 -m orchestrator start "Review the supplied external draft" \
  --task-type review --draft-spec draft.md --execution-config review.json --dry-run
```

`review.json`:

```json
{
  "schema_version": 1,
  "logical_work_id": "work-a",
  "spec_series_id": "series-a",
  "stages": {
    "review": {
      "role": "reviewer",
      "provider": "claude",
      "model": "claude-fable-5-1",
      "effort": "high"
    }
  }
}
```

Remove `--dry-run` for intake, then explicitly approve `start-go` if requested.
The existing interpretation-envelope resolver still performs its bounded
intake classification; it is not a proposal author or an extra spec reviewer.
The external draft route executes only `external_spec_review`. Deployments
overriding `ORCH_PROFILES_DIR` must install that profile before enabling it.

At `start-go`, this route validates that its frozen plan has exactly one
tool-less reviewer stage. The external draft remains the reviewer's immutable
evidence, but is not a source of authority for this invocation's intake resolver.
Only the caller's task and scope are resolved. Read-only references to paths do
not imply repository writes: the reviewer write set must be empty, while the
engine still adds its own report artifacts. Proposed writes, scope that the
resolver marks unresolved, malformed output and other failed axes still stop
intake. Mixed natural-language intent is judged by the resolver, not a lexical
deny-list; this route never gains implementation capability. Combining
`--draft-spec` with `--approved-spec` or `--executor` is rejected. Apply
and legacy routes retain their source-grounded write-target checks.

Each enqueue attempt retains a private, content-free resolver receipt referenced
by `execution.resolver_receipt`: resolver status (not enqueue success), elapsed milliseconds, reply hash/length
when available, and structural missing-key/unknown-key counts. Unknown key names,
values and raw replies are not retained there. Model and usage are explicitly
unavailable with the current resolver text transport. A receipt cannot recover
the semantic content of an old malformed reply; failure to save it blocks enqueue.

For apply, use the existing approved-spec/worktree entry with execution config.
The first version supports only `codex_implement_claude_review`: configure
`implement`, `review`, `repair`, `delta_review`. RD stages use role `executor`,
provider `codex`, and Astra's explicit model/effort; reviewer stages use role
`reviewer`, provider `claude`, model `claude-fable-5-1`. Every nonterminal stage
must be covered, including repair stages even though repair is not automatic
after an opt-in review stop. `--effort` fills omitted executor effort only;
conflicting explicit effort is rejected. No environment fallback is used.

## Authority and evidence

The intake stores the resolved plan and digest, draft/spec bytes and hash, and
session binding. Modifying the source JSON afterwards does not reroute an
existing task. Native argv is rebuilt per stage, not by changing global env.
Unsupported wrappers, duplicate flags and tool/permission overrides fail closed.
After explicit user approval of the native nested-sandbox failure, opt-in Codex
uses `--sandbox danger-full-access` and `approval_policy="never"` inside mandatory
orch L1. Missing workspace, unavailable L1 or `ORCH_ALLOW_UNSANDBOXED` blocks
before executor spawn. L1 preserves its existing write allowlist, including CLI
state/temp directories; reads and network are unrestricted. This is not network
isolation or a claim that only the worktree is writable. Receipts name policy
`orch-l1-required-v1`. Legacy executor argv and tool-less reviewer flags are unchanged.

The reviewer runs with `--safe-mode --tools ''` at its canonical session cwd.
It receives a frozen spec/diff/untracked-file/report packet, not filesystem
tools. Historical meeting instructions cannot grant tools. A reviewer must not
claim it executed a test: executor reports remain claims, and sufficient actual
evidence must be supplied. Missing verification stays UNKNOWN with an owner.
Implementation candidate hashes are checked again after review.

Each sealed run includes `execution_receipt`: requested/resolved model/effort,
source, plan digest, argv, provider-reported identity when available, and session
binding. An invoked effort is not proof of provider-reported effort; the latter
is explicitly unavailable. Claude result JSON, not display text, supplies the
authoritative review. Session/model mismatch cannot yield PASS.

The three axes are product/spec, constraints, verification. A blocking finding,
FAIL or UNKNOWN cannot coexist with ready. Findings include stable ID, severity,
evidence, minimal correction and evidence that would reverse the finding.

## Stops and manual arbitration

Drafting and review follow the shared minimum-safe-scope instruction composed
by `Controller._build_prompt`, including legacy profiles. First establish
necessity, then the smallest sufficient safe solution, then consider optional
improvements. Keep optional benefit/cost/activation notes in existing Advisory
prose, out of required dependencies, blocking evidence and convergence live
sets until the user selects them. Required safety/data-integrity/verification
gaps still block; they cannot be relabelled optional. Prefer deletion/reuse
over completing unsupported new machinery. Once required acceptance passes,
optional ideas do not justify another round or a user-decision hold.

Use the existing evidence fields to justify blockers and the existing user
decision/spec update/new-intake path for selected scope changes; frozen input
is never edited in place. The coordinator arbitrates technical disputes within
scope; only the user authorizes expansion. No new schema, stage, provider call
or gate is introduced. This is a prompt-level semantic rule, not a deterministic
classifier of necessity; existing structural checks and evidence gates remain.

An opt-in non-ready review stops for Astra, without dispatching repair. Existing
timeouts, infrastructure failure handling and legacy convergence stay in place;
no new round budget is added. A transport failure is not semantic disagreement.

Before authorizing continuation, Astra records the logical work/spec/candidate
IDs, sealed review reference, live and resolved findings, executor rebuttal,
Fable's response, and the exact decision/new evidence. Fable-maintained findings
go to the original Astra. Scope or acceptance changes needing user judgment
stay waiting for the user. Do not hide an unresolved finding by changing task ID.
No-progress, repeated disagreement without evidence, or oscillating corrections
are stop conditions, not invitations to add another reviewer automatically.

## Session recovery

Only one call may use a registered series at a time in its owning runtime.
Do not import the same provider session into separate runtime homes: this is
not a cross-home or cross-host session service. A pending record is written
before spawn and cleared only after the controller commits its sealed receipt.
Releasing an OS lock after a crash is not evidence that the provider failed.

```sh
python3 -m orchestrator review-session-status series-a
python3 -m orchestrator review-session-reconcile TASK_ID
```

Reconciliation accepts only the database-committed matching seal, never a loose
receipt or old PASS. Without a confirmed session/model result, it refuses replay.
If explicit recovery is necessary, stop the old worker first, record the unknown
operation and retain its artifacts. Astra/user may then request rehydration:

```sh
python3 -m orchestrator review-session-rehydrate series-a \
  --expected-session OLD_SESSION_ID --cwd "$REVIEW_CWD" \
  --reason "Explicit operator decision after stopping the old worker" \
  --checkpoint checkpoint.json
```

The checkpoint requires `spec_series_id`, nonempty `current_spec`, and explicit
lists `decisions`, `live_findings`, `resolved_findings`. The new session receives
this context but not old approvals. The entire predecessor record, including
unknown pending work, remains retained. Old task bindings fail closed; intake
must explicitly bind a new task after the operator resolves the old task's
disposition. This is `context_rehydrated`, not continuity of the old session.

## Deployment verification requirements

Local tests and reviewer approval alone do not authorize deployment. Record
native isolated-daemon model/session/permission evidence, recovery/serialization
evidence, final candidate regression, and independent stop-gate separately.
The main Fable session cannot serve as its own independent stop-gate.

For `external_spec_review` with a required stop-gate, the executor stays null.
`gate-run` selects the opposite provider family from the original reviewer
(Claude review → Codex gate; Codex review → Claude gate if such a route is
supported). This does not add a new external-review provider: the current
external-review route remains Claude. Apply routes still select against their
executor. Unknown families, missing executors on other routes and inconsistent
reviewer provenance fail closed. The gate execution record retains
`subject_role` / `subject_provider` separately from `executor`.

The independent gate checks the draft and review evidence, not an unrequested
implementation. `start-sync` → `gate-run` → `gate-sync` only records a
recommendation; an explicit `gate-allow` / `gate-block` decision is still needed.
A successful reviewer/controller result alone is not full lifecycle completion.
Do not push, restart production or claim cache savings based on session reuse.
