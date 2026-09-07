# The provider output boundary

Status: accepted. Supersedes nothing; adds a boundary that did not exist.

## The problem

A provider CLI's stdout is a **display** stream, not one utterance. A native
`codex exec` run prints, in order:

1. a session header,
2. the composed prompt echoed back under `User instructions:`,
3. reasoning summaries and tool events, including the stdout of every command
   the model ran,
4. the model's final message,
5. a token-usage line.

The engine read the typed outcome and the convergence record from that whole
stream. Both are claims the *model* makes, and reading them from the merged
stream had three consequences, all reachable without an adversary:

- **The engine's own prompt supplied outcomes.** The stage footer contains the
  literal `ORCHESTRATOR_OUTCOME:` line, and `_convergence_section` shows the
  convergence markers *twice*. Echoed back, a valid single record parsed as
  duplicated, so every branching run held with `convergence_record_invalid`.
- **Tool results supplied outcomes.** Any `cat` of a file containing a marker
  became a candidate answer.
- **A good run could not be recognised.** `_final_outcome_marker` reads the last
  non-blank line, which under a native transcript is the token-usage line. Even
  a perfect run fell through to the whole-stream scan and succeeded only when
  the stream happened to contain exactly one distinct marker.

And it did not stop there. A branching run held for an unreadable record stays
in the task's history, and `_convergence_context` read *every* prior branching
record. One held run therefore made every later review permanently unreadable:
no fresh, valid review could ever establish a baseline again.

## The decision

**A dedicated channel, not a better parser.** `codex exec` supports
`--output-last-message <FILE>`, which writes exactly the final agent message
and nothing else. The engine appends it, reads the file after the child is
reaped, and validates it strictly. The typed outcome and the convergence record
are read from that text and from nothing else.

Two properties matter, and neither is available from the display stream:

- the CLI decides where the final message begins and ends, so there is no
  framing to guess and no framing to forge;
- no prompt echo and no tool result can reach the file at all.

**`--json` was the alternative and is not used as the channel.** It reshapes the
display stream but still merges everything into one stream, and reading it means
tracking an event-envelope shape that has changed across Codex releases — a
version bump would silently reintroduce the parse this replaces. The two flags
are orthogonal, so an operator who sets `--json` for their own reasons keeps a
correct final response either way.

**Protocol selection is from configuration, never from content.**
`final_response_protocol(owner, command)` decides before the process starts:

| condition | protocol |
|---|---|
| `owner == "codex"`, argv[0] basename `codex`, argv[1] `exec` | `codex_output_last_message` |
| the same, but the command already sets `-o` / `--output-last-message` | **refused** — `provider_final_response_channel_conflict` |
| anything else | `whole_stream` |

Letting stream content select the protocol would be the same defect one level
up: unknown output choosing how it will be read. A stream that is byte-for-byte
a native transcript, produced by a command that is not a recognised native
Codex invocation, still gets the whole-stream protocol.

The middle row fails closed rather than downgrading. Two flags cannot both own
one file, so the engine will not overwrite the operator's; and quietly treating
such a command as whole-stream reopened exactly the contamination this boundary
exists to close — by configuration, with nothing in the run to say it had
happened. Provider preflight reports it as a provider configuration problem,
and `SubprocessRunner.run` refuses independently for any caller that skips
preflight.

Recognition is deliberately narrow, because a false positive fails a stage
closed on every run. The cost is that an operator who invokes Codex through a
wrapper script, or through some other subcommand, keeps whole-stream parsing and
therefore keeps the contamination exposure. That is a known boundary, not an
oversight: the engine cannot equip a command it does not recognise, and refusing
to run unrecognised commands would break every legacy, fake and custom provider
command the test suite deliberately preserves.

## Fail-closed cases

Under the native protocol the run has an authoritative final response or a
reason it has none. There is no fallback to the display stream — that fallback
*is* the hole.

| condition | stop reason |
|---|---|
| no file (the run never reached a final message; also how truncation presents) | `provider_final_response_missing` |
| file present, no non-whitespace content | `provider_final_response_empty` |
| not a regular file — a symlink, FIFO, socket, device or directory | `provider_final_response_unreadable` |
| unreadable, or not valid UTF-8 under a strict decode | `provider_final_response_unreadable` |
| larger than 4 MiB | `provider_final_response_too_large` |

### One descriptor, and why

The read is a single `os.open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)`, an
`fstat` on **that** descriptor, a bounded read of at most 4 MiB + 1 bytes from
it, and an unconditional close. Not a path `stat` followed by a path read. Two
separate reasons:

*Liveness.* `open(2)` on a FIFO for reading blocks until a writer appears. A
capture path that is a FIFO with no writer hung the worker indefinitely — and
it hung it *after* the child had been reaped, which is past the point where the
stage timeout can intervene, so the stage had no bound at all. `O_NONBLOCK`
makes the open return either way, and the file-type check means a FIFO is never
read from.

*File type.* `O_NOFOLLOW` refuses to open a symlink outright, and `fstat` on the
descriptor that was actually opened answers "what did I open" rather than "what
was at this path a moment ago". Anything that is not a regular file is a damaged
channel. Without this, a symlink at the capture path was followed and its
target's content became the final response.

The limit+1 read means a file that grew past the cap between the `fstat` and the
read is still caught, and never more than the cap reaches memory.

**This is file-type validation, and that is all it is.** It is not a claim of
immunity to active tampering by a process running as this same UID. Such a
process can replace a regular file's contents between any two operations, and
nothing here prevents that. Same-UID tampering is outside this engine's threat
model in the first place — the provider CLI already runs with this account's
full authority, and process isolation is not implemented (see
`docs/threat-model.md`). What these two flags close are ordinary defects: a
special file reached by an unlucky path, and an unbounded blocking read.

Precedence is unchanged above this: containment stop, then timeout, then
non-zero exit — including the rate-limit and socket signatures, which are CLI
reports and stay stream-read — and only then the boundary. A run that was killed
or that failed has no final response for reasons already reported.

The capture path is `<log>.containment/provider-final-response.txt`: run-local,
and inside the one directory besides the workspace that a contained child may
write. It is cleared *before* the spawn, so a leftover file from an earlier
attempt can never answer for this run.

It is **not** cleared by the runner afterwards. The runner cannot know whether
sealing will succeed, and deleting there cost the only durable copy of the
authoritative text whenever anything between the run and the seal failed. The
path travels on the run result instead, and the controller removes the file
once it has written the sealed artifact holding those exact bytes — a handoff,
not a hope. Two consequences worth stating:

- If sealing never happens, the capture stays on disk. That is the intended
  failure mode: unsealed but present beats verified-looking but gone.
- A run that failed *at* the channel sealed its display stream, not its
  capture, so its capture — empty, or undecodable — is never released. Those
  bytes are the only record of what went wrong.

## Evidence and sealing: manifest schema 3

Two artifacts, separately inspectable and separately hash-verified:

- `<log>.output.txt` — the **complete display stream**, unchanged. This is the
  audit evidence and it is never narrowed, filtered or rewritten. Contamination
  and all: what the provider emitted stays exactly what the record shows.
- the **authoritative final response**, named by `final_response_path` under
  `final_response_hash`.

Schema 3 adds five keys:

| key | meaning |
|---|---|
| `final_response_path` | the artifact the decision was read from |
| `final_response_hash` | its SHA-256 |
| `final_response_separate` | whether that artifact is a dedicated final response (`true`) or the display stream standing in for one (`false`) |
| `final_response_source` | the protocol name |
| `final_response_error` | the fail-closed reason, or `null` |

Under the whole-stream protocol the authoritative text *is* the display stream,
so the manifest names the same path rather than duplicating the bytes.
`final_response_separate` is recorded rather than inferred, so a reader never
has to reconstruct the discriminator from a combination of other fields.

### The state matrix

Exactly three states are legal, and every one of them is reachable:

| protocol | `separate` | `error` | named artifact |
|---|---|---|---|
| `whole_stream` | `false` | `null` | the display stream |
| `codex_output_last_message` | `true` | `null` | its own file |
| `codex_output_last_message` | `false` | a known reason | the display stream |

`validate_sealed_boundary` in `runner.py` is the single derivation. The manifest
writer, `controller._read_sealed_run` and the retained reader all call it, so
the three cannot drift on what a legal schema-3 run looks like. It also checks
the artifact coupling: a non-separate row must name the display stream *itself*,
by path and by hash, so a manifest cannot point "the display stream" at some
third file.

One rejected combination is the reason the function exists. *Native, not
separate, no error* claims a run used the dedicated channel, produced no
separate artifact, and did not fail. **No such run exists.** Accepting it left a
reader with no final response, so it fell back to the raw display stream and
reported that as the verified authoritative text — the contamination, laundered
through the evidence reader. With a display stream whose only outcome marker is
the echoed prompt, that produced `integrity: verified` and a stop-gate
`candidate_outcome: allow` harvested from the engine's own prompt.

The vocabularies are closed. An unknown protocol name and an unknown failure
reason are both refused rather than interpreted: a manifest naming a reason this
engine does not produce is not a failure any reader can vouch for.

The writer is held to the same matrix. `_seal_run_manifest` validates the
payload it just built and raises `ControllerError` rather than sealing metadata
no reader will accept.

**Legacy reader compatibility.** Both sealed readers accept schema 1, 2 and 3. A
schema-1 or schema-2 manifest names only the display stream, which is exactly
what such a run was classified from, so those bytes are its final response.
Nothing is rewritten to make old evidence fit the new contract, and retained
inspection reports `final_response_source: null` for it rather than inventing an
answer.

That raw fallback is restricted to schema 1 and 2 **only**. Applied to a
schema-3 manifest it would silently substitute the display stream for a final
response the matrix exists to validate, which is the same defect from the other
direction. A schema-3 manifest whose boundary metadata is missing, malformed or
contradictory is unreadable, not downgraded; a `schema_version` outside
`{1, 2, 3}` is unreadable too.

**A run with no final response has none.** For the third row,
`controller._read_sealed_run` raises rather than returning the display stream:
its convergence record cannot be read out of a transcript. Retained inspection
carries the failure reason into classification instead, so the candidate is that
failure and never an outcome scavenged from the stream.

**Deletion and tampering.** For schema 3, retained inspection verifies the final
response artifact on its own hash. A deleted file and an edited file both raise,
so neither can be reported as `integrity: verified`.

## The producer contract

Everything above is the *consumer* half: where the engine reads a decision from.
It has a producer half, and leaving it implicit cost a correct run.

`Controller._convergence_section` used to say a branching stage's "output must
carry exactly one convergence record ... parsed from your own output". Read on
its own that is accurate. Read where a provider actually reads it — immediately
above stage instructions that say *"Write apply-review.md into the reports
directory with an explicit verdict per axis"* — "put the record in your output"
reads naturally as "put it in the review I was just told to write". A reviewer
that did exactly that had done the work correctly and still failed closed on a
missing record, because the engine only ever looks at the final response.

The prompt now states the obligation as **machine output**, up front, and names
the report as the thing it is not:

- the record must be in the **final assistant response** — the last message the
  provider emits, in the response body itself;
- a record in `apply-review.md`, `apply-report.md`, any other file, or any tool
  result **does not satisfy it**, explicitly including the case where the stage
  instructions ask for that file;
- the two are separate deliverables and the provider owes both;
- ordering: the complete record block, then the `ORCHESTRATOR_OUTCOME` line,
  which remains the very last line of the output. Nothing after it.

Both branches carry it. The first-run directive and the repeat-review directive
each restate the final-response requirement in their own terms, so neither can
be read in isolation without it.

Two things this deliberately does **not** do:

- It does not weaken the hold. A missing or malformed record still ends the run
  at `needs_user_decision` with `convergence_record_invalid`, and a record that
  reached the engine only as a tool result is still missing. The prompt change
  is there so a competent provider stops arriving at that hold by accident — not
  so the engine forgives arriving there.
- It does not show the delimiters any more often than before: one pair in a
  first-run prompt, two in a repeat. A prompt that displayed them more would
  invite a model to echo more than one pair into its final response, which the
  consumer rejects as duplicated.

## Baseline recovery

`_sealed_convergence_records` distinguishes two kinds of unreadable prior
record, and the distinction is the whole point:

- A run held because its record was **never established** —
  `convergence_record_invalid` or `convergence_unverifiable` — made no accepted
  claim. It is skipped: it contributes neither a baseline nor a resolved
  identity, and it does not make later reviews unreadable. This is what lets a
  fresh valid review establish a baseline after a parser-held run, without any
  old seal being rewritten.
- Any **other** unreadable record — a deleted artifact, an edited one, a hash
  that no longer matches — stays a hard error. There the run *did* make an
  accepted claim and the evidence for it is gone.

`convergence_stalled`, `convergence_oscillating` and
`convergence_contradictory` all had a readable record, so they remain baselines
and are not skipped. Only the two "never established" reasons are.

## What is unchanged

Outcome strictness (`ambiguous_outcome`, `unknown_outcome`, `missing_outcome`),
convergence validation and its verdict rule, provider model selection,
timeouts, edge and transition caps, containment, and whole-stream classification
for every command that is not a recognised native `codex exec` — including one
that is unrecognised *because* it already claims the channel's flag under some
other executable name. Duplicate or
malformed records *inside* the final response still hold, exactly as before —
the boundary narrows where a record is read from, never how strictly it is
judged.
