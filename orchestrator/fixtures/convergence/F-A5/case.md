# F-A5 — unexecuted daemon-restart evidence must not block `ready`

Scenario: the reviewed spec changes engine code. The change cannot take effect
until the daemon restarts, and the spec author cannot restart it. The reviewer is
tempted to mark the spec not-ready because a required check has not been run.

## This is advisory, not blocking

```
Source: established_invariant
Source reference: convergence policy, ready contract R3
Failure scenario: none at spec time. The check is unexecutable by the drafter, so blocking on it makes `ready` unreachable for any spec that touches engine code.
Material consequence: none, provided the item appears in the Deferred evidence section with a risk, an observation target and a named owner/gate.
Why spec must decide now: it does not; the gate holds it.
Simplest sufficient correction: n/a - advisory.
Finding origin: original_surface
```

## What the reviewer must check instead

The Deferred evidence section contains a row of the form:

| Risk | Observation target | Owner / gate |
|---|---|---|
| engine change not in effect (daemon not restarted) | a new task hitting the hold outcome really stops at `waiting_user / user_decision_required`; daemon start time later than the change time | A3 stop-gate (manual) |

A deferred row with no named owner/gate is not deferred evidence. It belongs in
the out-of-program backlog and must not occupy the Deferred evidence section.
