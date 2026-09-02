# F-A8 — distilled from a real propose trace

## Source artifacts (read-only; never modified or resumed)

- `<ORCH_HOME>/tasks/<TASK_ID>/reports/propose-scratch/review-notes.md` (v11, 8,309 bytes)
- `<ORCH_HOME>/tasks/<TASK_ID>/evidence.json`

The same spec spans four tasks. Its `spec-draft.md` reached 268,524 bytes. The
`evidence.json` transitions are: seq1 `submitted -> queued`; seq2 `draft ->
drafted`; seq3 `review -> needs_convergence`; seq4 `draft -> blocked /
runner_nonzero` with no outcome.

## The six High findings and their origin

| # | Finding | Origin | Basis |
|---|---|---|---|
| H1 | P6 uses the wrong profile carrier: the spec starts the existing two-stage `spec_review` where its own brief fixed a task-local single-stage profile | `newly_added_spec_mechanism` | the carrier, its `$EV` path and its disposition are all invented by this spec; the contradiction is between the spec's own sections |
| H2 | the helper's write-mode `sqlite3.connect` keeps create semantics, so a post-precheck unlink still produces a fresh empty DB | `original_surface` | the risk is in the database surface being specified, and predates the spec |
| H3 | `p6-artifact-index.tsv` cannot simultaneously carry the attempt counter, the artifact provenance and the "no probe after round three" evidence | `newly_added_spec_mechanism` | the index, its four columns and its one-row invariant are all introduced by this spec |
| H4 | the second-level window choreography is unexecutable and the release step names two different moments | `newly_added_spec_mechanism` | the timeline, the release marker and the step numbering are all introduced by this spec |
| H5 | RB-W's disposition gate accepts resumable statuses and skips the second leaf re-verification before `rmdir` | `newly_added_spec_mechanism` | `ALLOW_NEXT_ATTEMPT` is a predicate this spec defined; the engine already has a terminal-status set it could have reused |
| H6 | T7's completion evidence equates `mkdir_calls` with a row count that includes a file SQLite created, so no legal success path satisfies it | `newly_added_spec_mechanism` | the counters and the equation are the spec's own completion evidence |

Five of six blocking findings are contradictions the spec manufactured for itself.
Only H2 points at the durable-state risk the work was started to address.

## What the old profile did with this round

Under the frozen `old-profile.yaml`, `review` declares exactly two outcomes:
`needs_convergence -> draft` and `ready -> done`. Since High findings remain,
`ready` is unavailable, so all six findings above - regardless of origin - collapse
onto the single edge `review.needs_convergence -> draft`.

That is the structural cause of the positive feedback loop. The drafter is handed
"go back to draft and address the notes" for a round whose majority content is
"the mechanisms you added contradict each other", and the only move `draft` prompts
for is to address items, which means adding more.

## What the new profile does

Rule 1 applies: at least one `newly_added_spec_mechanism` blocking finding exists,
so the outcome is `needs_simplification` and the route target is `simplify`. H2 is
a correction in the same round, so it must survive: the simplify output has to
carry it in the "original risk to post-simplification owner" table.
