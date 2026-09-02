# Propose convergence policy

Status: adopted
Scope: the `propose` profile only. The apply profiles
(`claude_apply_codex_review.yaml`, `codex_implement_claude_review.yaml`) and the
stop-gate profiles are unchanged.

## 1. The problem

A propose lifecycle has one edge back from `review` to `draft`. Everything a
reviewer can block on travels down it, and the drafter's instruction on arrival is
"address every item in the notes". Addressing an item means adding something.

When the items are *risks in the surface being specified*, that is the right
behaviour. When the items are *contradictions among mechanisms the spec itself
introduced*, it is a positive feedback loop: the spec grows, the new material
contradicts itself in new ways, and the next round's findings are mostly about the
previous round's additions.

Three independent samples of this were recorded before this policy existed:

- An anonymized reference trace, review v11: five of six High
  findings were about mechanisms that spec had added (an optional probe, an
  artifact index, a window choreography, an invented disposition predicate, a
  completion-evidence equation). One - a write-mode `sqlite3.connect` with create
  semantics - was the production risk the work started from. The draft reached
  268,524 bytes across four tasks.
- A second anonymized trace, three review rounds: 5 of 8, then 3
  of 4, then 3 of 3 High findings had origin `newly_added_spec_mechanism`.
- In that same task, the round that actually converged did so by **removing the
  requirement that had created the contradiction**, not by adding a mechanism to
  reconcile it.

The fix is to stop routing those two kinds of finding onto the same edge.

## 2. Burden of proof

Every new actor, state, artifact, protocol, probe, counter, scheduler, daemon,
recovery branch or orchestration task must answer five questions:

1. Which specific and unacceptable failure does it prevent?
2. Is that failure sourced from a user requirement, runtime evidence, or an
   established invariant?
3. Without it, which acceptance item cannot be proven?
4. Can something that already exists achieve the same effect?
5. Is it more complex than the risk it controls?

An item that cannot answer all five is not included. This policy applies the rule
to itself: its whole engine footprint is one constant pair and two `elif` branches.

## 3. Routing contract

`review` has four outcomes:

| Outcome | Target | Cap | Meaning |
|---|---|---|---|
| `ready` | `done` | 1 | acceptance met (section 6) |
| `needs_correction` | `draft` | 2 | a risk in the specified surface is unaddressed |
| `needs_simplification` | `simplify` | 2 | the spec's own mechanisms contradict each other |
| `needs_user_decision` | `draft` | 1 | an irreducible decision is required (section 7) |

`needs_convergence` is removed. Existing tasks are unaffected: each task reads its
own `profile.snapshot.json`.

`simplify` is a new stage (`owner: claude`) with the single outcome
`simplified -> review`, cap 2. It exists because `prompt` is a per-stage field:
there is no place in the schema for a per-outcome prompt, and "delete, merge or
restructure" is a different instruction from "address every item".

`max_transitions` rises from 10 to 14. The caps sum to 5+2+2+2+1+1 = 13.
Divergence is bounded by the edge caps; `max_transitions` only exists so that a
run does not stop with the uninformative reason `transition_cap` while caps are
still unspent. `transitions_count` is incremented in exactly one place in the
engine - the CAS inside the success branch of `commit_run` - so the comparison
"13 crossings fits under 14" is exact and needs no margin.

Existing `owner`, `attempt_cap` and `timeout` values are unchanged. The engine
profile keeps `timeout: 600`; the deployment profile keeps its own `timeout: 900`.

## 4. Finding schema

Every **blocking** finding written to `review-notes.md` carries these fields. This
is a prompt contract. There is no parser, and none should be added.

```
Source: user_requirement | runtime_evidence | established_invariant
Source reference: <a resolvable file, artifact or policy reference>
Failure scenario: <a concrete counterexample>
Material consequence: <data, security, permission, routing or recoverability impact>
Why spec must decide now: <why apply / review / stop-gate cannot hold it>
Simplest sufficient correction: <prefer deletion, reuse, direct observation>
Finding origin: original_surface | newly_added_spec_mechanism | regression
```

Legal sources: the task's immutable input, a named runtime artifact, repository
policy, a decision the user has already made, or an invariant listed in the brief.
A finding with no `Source reference`, or with one that does not resolve, is
advisory and cannot block.

A generic demand to rewrite the whole document is not a legal blocking finding.
Every blocking finding must first be classified as correction or simplification and
must fit the schema above.

## 5. Outcome decision rules

Applied in order:

1. Any blocking finding with `Finding origin: newly_added_spec_mechanism` ->
   `needs_simplification`, even when corrections are present in the same round.
2. Otherwise, any blocking finding with origin `original_surface` or `regression`
   -> `needs_correction`. `regression` routes identically to `original_surface`;
   the label exists only so the origin distribution stays observable.
3. Otherwise, a missing decision that would change scope, architecture, persistent
   state, routing, rollback or how the work is verified -> `needs_user_decision`.
4. Otherwise -> `ready`, subject to section 6.

### The boundary of rule 3

Rule 3 holds only when **every** design that satisfies the brief must make that
decision. If a design exists that avoids the decision and passes the burden of
proof in section 2, the reviewer requires that design - routing through rule 1 or
2 - instead of holding.

Escalating a decision that a design could have removed parks the lifecycle on a
question nobody needs answered. Fixture `F-A4b` pins this boundary, distilled from
how the reserved-outcome-name design in section 7 was actually chosen over a
profile schema change.

### Corrections are never lost to simplification

When rule 1 fires while corrections are outstanding, `review-notes.md` still
records all of that round's blocking corrections in full, and the simplify output
must account for each of them in its simplification delta (section 9).

## 6. `ready` contract

- R1 - every original major risk (`original_surface` / `regression`) has a
  minimally sufficient treatment that points at a specific section.
- R2 - every retained mechanism traces to its burden-of-proof answers.
- R3 - every unexecuted check sits in the Deferred evidence section with a risk, an
  observation target and a **named owner/gate**. An item with no owner/gate does
  not belong in that section; it belongs in the out-of-program backlog and does not
  block `ready`.
- R4 - no undecided scope, persistent state, routing, rollback or verification
  decision remains, judged by the rule 3 boundary above.
- R5 - none of the three axes (Product / Spec alignment, platform constraints,
  verification evidence) is FAIL or UNKNOWN.

## 7. `needs_user_decision`: the engine hold

### Why an engine change is unavoidable

- An edge cap never stops the first crossing: the counter starts at 0 and the
  minimum cap is 1, so `count >= cap` is false the first time.
- Only edge caps, the transition/attempt caps and the provider preflight currently
  produce `waiting_user`. No stage outcome can.
- A terminal stage can only be `done` or `failed`, and a queued task pointing at a
  terminal stage is rejected outright, so a "pause stage" cannot work.

### The mechanism

`orchestrator/controller.py` defines two module constants:

```python
HOLD_OUTCOME = "needs_user_decision"
HOLD_STOP_REASON = "user_decision_required"
```

In the success branch of `commit_run`, immediately after the existing
`if edge_cap_hit:` block, one `elif` sets `next_status = "waiting_user"` and
`stop_reason = HOLD_STOP_REASON`. Everything downstream reads only those two
variables, so the edge counter increment, the `stage_runs` update, the transition
record and the task CAS are untouched. One matching `elif` is added to the
notification chain.

Because the CAS is unchanged, a held task lands with `current_stage` already
advanced to `draft` and `owner` already `claude`. Resume needs no `--rerun-stage`.

Edge cap wins over the hold, because `elif` follows `if edge_cap_hit`. This
preserves existing behaviour exactly.

Total footprint: one constant pair, one `elif` in `commit_run`, one `elif` in the
notify chain. No profile field, no task status, no table, no CLI verb, no artifact
type, no snapshot format change.

### Engine reserved outcome names

`needs_user_decision` is an **engine-reserved outcome name**. Its stop semantics
come from the engine version, not from the task's `profile.snapshot.json`.

Current reserved names:

| Name | Effect |
|---|---|
| `needs_user_decision` | on commit, stop at `waiting_user` with `stop_reason=user_decision_required`, unless an edge cap already fired |

Consequences and controls:

- A future profile cannot use this outcome name to mean something else.
- A snapshot does not record the semantics, so replaying an old snapshot on a newer
  engine would apply them. This is acceptable only while no other profile declares
  the name.
- Before cutover, the whole task store must be scanned:
  `grep -rl needs_user_decision "$ORCH_HOME"/tasks/*/profile.snapshot.json`. Any
  hit is fail-closed and goes to the user.
- `grep -rn needs_user_decision orchestrator/profiles/` must match only
  `propose.yaml`.

### Why not a profile field

A per-stage `holds` field would require widening the exact-key set shared by every
profile, emitting the field from `to_dict()` (or the semantics vanish from the
snapshot), and proving backward compatibility for existing snapshots - and the
field would be visible to every profile that should not use it. The reserved name
costs none of those: outcome names are already in the snapshot, `validate_profile`
is untouched, and only the profile that declares the name is affected. Both engine
and deployment profile sets were checked and neither used the name.

### Task-local answer and resume

- The drafter records the single highest-priority question, with a recommended
  answer and its trade-off, in a fixed section of `spec-draft.md`. The reviewer
  cites that section from the blocking finding. **No separate question file.**
- The user writes the decision to `<artifact_dir>/decision-answer.md` and runs
  `orch resume <task_id>`.
- The draft prompt states: if `decision-answer.md` exists, treat its contents as a
  settled decision and apply it; do not rewrite it, do not supply decisions it does
  not contain, and **do not create the file**.
- What actually enforces this, stated honestly: for stages with a workspace, the
  sandbox write allowlist does not include the artifact directory root - only the
  `reports/` subdirectory is allowed. **The propose stage has no containment at
  all** (containment is only set up when a workspace exists, and real propose runs
  produce no `.containment` sidecar and no `containment_workspace=` log header).
  So a propose agent can create the file. The protection that does hold is
  procedural: a held task stays at `waiting_user` until an operator runs
  `orch resume`. The operator must read `decision-answer.md` and confirm it matches
  their actual decision before resuming. The CLI neither displays nor validates it.
  No enforcement mechanism is added here; the check is a named stop-gate item.
- The immutable input never changes. `tasks.input_hash` is identical before the
  hold, during it, and after resume, and can be compared with `orch status`.
- No general decision ledger. The answer lives only in that task's artifact
  directory.

## 8. `review-notes.md` lifecycle

Each round **overwrites** `propose-scratch/review-notes.md` with three sections:

- `## Blocking findings` - only those still live this round, with all schema fields.
- `## Resolved this round` - title plus a one-line reason. A finding invalidated
  because its mechanism was deleted is noted `resolved_by: mechanism_deleted`.
- `## Advisory` - non-blocking.

No cross-round finding ids, no stable numbering, no accumulated history. History is
already carried by `runs/*.log` and `evidence.json`.

## 9. Simplify contract

The `simplify` stage uses the current draft as its base and makes the smallest
coherent edit that resolves the current `Blocking findings`:

1. Only affected mechanisms and necessary cross-references change. Unrelated
   sections, evidence, decisions, tasks and wording stay in place.
2. One concise **Simplification delta** table records one row per current blocking
   finding: its reference, the delete / merge / reuse /
   remove-draft-only-requirement / necessary-minimal-mechanism action, and the
   existing spec section carrying any affected sourced risk. It points to existing
   sections instead of copying every historical risk.
3. Every current `original_surface` or `regression` correction remains accounted
   for, and every sourced original risk affected by a deletion remains traceable.
4. The document does not grow unless item 3 requires new text. Each such addition
   is named in the simplification delta.

Simplify does not rewrite the whole document, restate background, reorder unaffected
material or expand for completeness. It may remove a requirement only when the
current draft invented it and it is not traceable to the immutable task input, a
settled user decision, established policy or sourced runtime evidence. It must never
remove, weaken, defer, reinterpret or relabel a requirement or acceptance outcome
traceable to one of those sources merely to eliminate a contradiction.

Adding machinery is forbidden by default. One narrow necessity exception exists:
when a current blocking finding supplies a resolvable source reference, a concrete
failure scenario and a named acceptance target, and no delete / merge / reuse design
can preserve the sourced requirement, simplify may add exactly one smallest
necessary mechanism for that finding. The delta must explain why existing primitives
are insufficient and exclude adjacent capability. If preserving the requirement
instead needs a material user choice, simplify leaves the finding unresolved for
`needs_user_decision` rather than inventing the choice.

## 10. Invariants

- IA-1 - every `review` outcome has exactly one entry in `edge_caps`.
- IA-2 - `review-notes.md` keeps only currently live blocking findings.
- IA-3 - the immutable input is never appended to or overwritten; `input_hash` is
  constant across the whole lifecycle including hold and resume.
- IA-4 - a reviewer does not invent a blocking invariant on the spot. A new
  invariant claim that would change the design goes through `needs_user_decision`.
- IA-5 - simplify edits the current draft in place, checked against the immutable
  input, settled decisions and runtime evidence; unrelated content is preserved.
- IA-6 - the meaning of `waiting_user` and the retention of the latest draft are
  unchanged; edge cap and transition cap do not become terminal failures.
- IA-7 - **zero change to the profile schema and the snapshot format.** Every
  existing profile file and every existing `profile.snapshot.json` validates and
  round-trips to byte-identical output.
- IA-8 - `needs_user_decision` is engine-reserved. A profile that does not declare
  it behaves bit-identically. The task store is scanned for it before cutover.
- IA-9 - propose profile changes are confined to routing: stages, outcomes,
  `edge_caps`, `max_transitions` and prompts. Existing `owner`, `attempt_cap` and
  `timeout` values do not move.
- IA-10 - source authority survives convergence. Simplify may remove draft-invented
  requirements, but cannot weaken immutable input, settled decisions, established
  policy, sourced runtime requirements or their acceptance outcomes.

## 11. Observability, not gating

Spec byte size, stage duration, finding-origin distribution, and the count of
retained versus deleted mechanisms are all worth reading. None of them becomes a
quality gate, a counter state machine or an automatic semantic threshold. No
collector, store or schedule is built for them: they are read by a human from the
existing `evidence.json` and `runs/*.log` at review or stop-gate time.

## 12. Fixtures

`orchestrator/fixtures/convergence/` holds F-A1 through F-A10 (including F-A4b) plus
the BP-1 blueprint. `orchestrator/tests/test_propose_convergence.py` verifies only
route-level facts and never calls a provider. Whether a provider judges a case
correctly is human / dogfood verification owned by the stop-gate. See that
directory's `README.md`.
