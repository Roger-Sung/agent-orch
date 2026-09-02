# F-A1 — real data-loss risk in the reviewed design

Scenario: the reviewed spec opens a SQLite database in write mode with
`sqlite3.connect(path)`. SQLite's default write open has create semantics, so a
run that points at a wrong or transiently missing path silently creates an empty
database instead of failing.

## Blocking finding

```
Source: runtime_evidence
Source reference: <ORCH_HOME>/tasks/<TASK_ID>/reports/propose-scratch/review-notes.md (H2)
Failure scenario: the canonical file is unlinked between the lexists precheck and connect(); the writer creates a fresh empty DB and the next read returns zero rows instead of erroring.
Material consequence: silent loss of durable-state integrity; downstream consumers cannot distinguish "empty" from "missing".
Why spec must decide now: the connect contract is the spec's own durability guarantee; apply cannot choose it later without changing the spec's acceptance.
Simplest sufficient correction: use an explicit no-create SQLite URI (mode=rw) and fail closed when the path is absent.
Finding origin: original_surface
```

## Why this routes to correction

The risk lives in the surface being specified, not in a mechanism the spec
invented. The correction is a deletion of create semantics, not an addition, so
`draft` can carry it.
