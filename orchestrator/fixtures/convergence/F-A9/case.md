# F-A9 — apply-shaped additive repair / evidence loop (observation only)

**This fixture has no `expected.json`, no profile, no route assertion and no test.**
It exists so the next real apply lifecycle has something concrete to compare
against. It must not be given assertions until runtime evidence shows the shape
actually occurs in apply.

## The shape to watch for

The propose loop's failure mode was: a review round dominated by contradictions
among mechanisms the spec itself added, routed onto the single "go back and address
the notes" edge, answered by adding more mechanisms. The apply loop has a
structurally similar edge - the reviewer's repair route back to the executor.

Symptoms that would indicate the same shape in apply:

1. Successive repair rounds where most blocking findings are about code, tests,
   assertions or evidence files introduced by an **earlier round of this same
   apply**, rather than about the change the task was started to make.
2. Diff size growing monotonically across repair rounds while the number of
   distinct original risks addressed stays flat.
3. New test scaffolding, counters or evidence artifacts added specifically to prove
   that a previous round's addition works, which then themselves become the subject
   of the next round's findings.
4. A repair round whose deliverable is a reconciliation between two artifacts both
   created by this task.

## What to record when it is observed

For the apply lifecycle's stop-gate: the number of review rounds, and for each
blocking finding, whether its subject predates this task (`original_surface`), was
introduced by this task (`newly_added_spec_mechanism`), or reintroduces a risk a
previous round had covered (`regression`).

## Decision rule

If the distribution matches this shape, open a correction to the apply profile.
If it does not, record the observation and close. Either way, do not change the
apply profile as part of the convergence work - the apply profile is explicitly out
of scope until this evidence exists.
