# F-A6 — simplification dropped a real risk

Scenario: the previous round routed to `simplify`. The simplified spec correctly
deleted an invented probe and its ledger, but the pre/post data-integrity
comparison (sha256 plus per-table row counts) disappeared along with them, because
it had been described inside the deleted section.

## Blocking finding

```
Source: established_invariant
Source reference: convergence policy, simplify contract item 2 (original risk to post-simplification owner table)
Failure scenario: the migration runs with no pre/post comparison; a partial copy produces a database that opens successfully with fewer rows, and nothing in the acceptance detects it.
Material consequence: silent data loss reintroduced by the act of simplifying.
Why spec must decide now: the comparison is the migration's only integrity acceptance.
Simplest sufficient correction: restore the pre/post sha256 and per-table row-count comparison as an acceptance item and list it in the mapping table.
Finding origin: regression
```

## Why this routes to correction

`regression` is routed exactly like `original_surface`; the distinction exists
only so the origin distribution stays observable. Simplification may delete
mechanisms, never risks: every risk the previous version carried must appear in
the mapping table with an owner, even when the owner is "apply" or "stop-gate".
