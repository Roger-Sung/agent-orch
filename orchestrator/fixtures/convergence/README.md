# Convergence fixtures

These fixtures pin the propose convergence policy documented in
`docs/decisions/propose-convergence-policy.md`. Each routing fixture is a pair:

- `case.md` — the finding situation a reviewer faces, written in the finding
  schema of the policy document.
- `expected.json` — `{"outcome", "route_target", "must_preserve"}`.

## What is mechanically verified

`orchestrator/tests/test_propose_convergence.py` asserts only route-level facts:

- `expected.outcome` is a declared outcome of the `review` stage in
  `orchestrator/profiles/propose.yaml`.
- `expected.route_target` equals the profile's target for that outcome.
- every referenced edge has a cap in `edge_caps`.

No provider is called, so these assertions cannot flake on model wording.

## What is NOT mechanically verified

Whether a provider actually judges a given `case.md` the way `expected.json`
says is **not** tested here. That is human / dogfood verification, owned by the
A3 stop-gate. Do not add assertions that compare model prose.

## F-A9 is deliberately different

`F-A9` has a `case.md` and no `expected.json`. It records an apply-shaped risk
for future observation only. It is attached to no profile, adds no apply route,
and carries no assertion.
