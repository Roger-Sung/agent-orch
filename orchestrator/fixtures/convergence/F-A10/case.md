# F-A10 — Simplification must preserve sourced live observation

The immutable task input requires a worker's lifecycle stream to be readable while
the worker is still running, without allowing regular-file I/O to block the same
deadline-sensitive drain path. Provider lifecycle parity is also an acceptance
outcome.

The current draft added synchronous writes on the drain path. Review correctly
blocks because a slow filesystem can stall draining and change timeout behavior.
Deleting the live requirement or deferring every write until after the worker exits
would remove the contradiction, but would also weaken the immutable requirement.

Expected policy behavior: route to simplify. Simplify first attempts delete, merge,
or reuse. If none can preserve both live readability and deadline isolation, it may
add one bounded handoff mechanism justified only by this sourced failure. It may not
add supervision, recovery, semantic interpretation, another scheduler, or any
adjacent capability.
