# Conversation Processing

## Extraction Execution Contract

Explicit `process --apply` verifies all captured source records in the configured
archive before model access. Dry-run makes no model calls. Apply requires an
explicit local model file, hashes its stable bytes and checks that localhost
llama.cpp reports that same path. Native template/tokenizer endpoints check that
prompt plus reserved answer fits the reported context before generation. The
preflight passes the same frozen request body as generation, including template
kwargs and structured-output options, not messages alone. The pinned native
[server implementation](https://github.com/ggml-org/llama.cpp/blob/5aeac37a3/tools/server/server-context.cpp#L5194)
uses the chat parser for `/apply-template` as well as generation. This matters
when an option such as `enable_thinking` changes the rendered prompt. The
setup backend must be `vulkan` or `cpu`; `api` and `cuda` are refused.
HTTP responses and connections are both closed on rejection or size-bound
failure; cleanup does not depend on garbage collection or a persistent connection.

Results are saved per chunk, read back and checked independently before progress
is counted. Reuse identity covers the full model hash, provider metadata, rendered
prompt, request schema/options, source/chunk content and all input/validation
policy. Changed options cannot reuse old notes; equivalent mapping order can.
Scheduling/timeout changes alone need not invalidate results.
The packaged prompt is frozen per process, so an update cannot change it between
fingerprinting and later chunks. Start a new process after updating prompt data.

Returned JSON is structurally validated locally, including configured limits.
Every note retains exact source record links and structural citation problems.
Out-of-chunk citations remain flagged; the existing nearest-row fallback supplies
explicitly labelled candidate evidence, not proof of semantic truth. Decisions
must cite an owner row in the chunk or carry a visible problem. A successful
model reply never proves its factual correctness. Credential-shaped output must
be sanitized before persistence; unfiltered model replies are not printed/logged.

Successful chunks remain immutable and reusable after interruption. A failed
attempt is recorded separately with a safe error category, not transcript/API
error bodies. Each window retries failed chunks once at its end; remaining
failures produce a nonzero result, never a successful empty extraction. Reuse
revalidates saved output/provenance and never silently repairs corruption.
Attempt records retain safe per-chunk failure events and unresolved chunk IDs,
including HTTP status or schema/timeout category, without raw response bodies.
One worker processes a window's chunks, one after another; the nightly run and
the batch commands in [OPERATIONS.md](OPERATIONS.md) schedule the windows.

## Commands

By default, `process` previews closed conversation stretches from a stable Claude
transcript using the same reader/configuration as `import`. This dry-run makes no
model, keyring or database calls and writes nothing. It is not proof that a
transcript was archived or notes generated.

```sh
anamnema process --file /path/session.jsonl --instance assistant-name --session native-session
anamnema process --file /path/session.jsonl --instance assistant-name --session native-session --closed
anamnema import --file /path/session.jsonl --instance assistant-name --session native-session --closed --apply
anamnema process --file /path/session.jsonl --instance assistant-name --session native-session --closed --apply --model-file /path/notes-model.gguf
```

The default is `--dry-run`; apply without `--model-file` fails before configuration
or transcript reads. `--archive-config` selects the complete archive reader
policy, as for import. Use the same instance/session/closure choices for import
and processing so their source references match. There is no automatic session-closing detection: use
`--closed` only to attest that the session has ended.

`--speech-config` and `--notes-config` select complete nonsecret filtering and
chunk/binding policies instead of packaged defaults. Their fingerprints appear
in the report. The notes policy's `model_options` are the sampling options sent
with every request; the preview itself calls no model.

`--config` selects the complete setup/keyring/local-service configuration.
`--extraction-config` selects complete execution limits, pacing, native model
routes and result collection names. Results/attempt collections must be distinct
from the transcript archive. MongoDB must already contain verified copies and
the notes llama.cpp must be running on the setup's localhost notes endpoint;
`anamnema install` provides both, `process` installs neither. Missing/locked keyring, unmatched model,
missing source and failed requests are errors, not empty successful notes.

Model authentication is optional for the local endpoint. If needed, configure
both `model_keyring_entry` and `model_keyring_field` in extraction settings and
store that token in the project's keyring namespace using the manual guide.
Only its selector belongs in JSON; the token is sent as a bearer header, never
in arguments, logs, saved manifests or reuse identity. Credential-like keys in
request options are refused. The token is read from the logged-in user's unlocked
keyring; automated tests use a fixture lookup executable.

The model file must remain stable, regular and nonsymlink. Its path must match
the server-reported path; its full hash, build and generation metadata define
identity. File metadata and server properties are rechecked during execution
and before cache reuse. This detects reported/file changes, not cryptographic
attestation of the weights resident in a separate process; the operator must
load the declared file rather than replacing an already-loaded file in place.

## Boundaries And Sources

The shared preparation layer consumes the common records produced by the
substrate adapter, not native Claude dictionaries. A summary closes the rows
before it; a closing marker closes through its explicitly referenced final row.
The summary or marker is not itself dialogue. Each closed stretch retains its
ordered conversation record IDs, content digests and physical source lines.
Changing the original words changes those identities, even if a native UUID is
reused. Exact repeated records are counted and retained only once for this view.

Order comes from the captured source sequence, never an invented tie-break from
timestamps or UUIDs. Equal timestamps do not drop or duplicate adjacent rows.
This adapts the existing summary/closing-window method to the public envelope's
explicit source identities. A marker whose final-record reference does not match
the preceding conversation fails visibly. Mixed instances/sessions/substrates
are refused rather than combined. Open rows after the final boundary are
reported separately and are not presented as a closed stretch.

The JSON report includes only IDs, digests, counts and source-line references,
not conversation text. In dry-run, `archive_verified` and `notes_generated` are false.
The report also counts eligible spoken rows, exclusions, rows changed by
credential masking, explicit numbered-list bindings and prepared chunks. It
still contains no conversation text. A row number keeps its position in the
source sequence even when another row was excluded.

Apply reports created/reused chunks, model calls, note/problem counts and an
attempt ID. `notes_generated` means new verified chunks were saved, not that a
replay called a model. A wholly empty eligible input makes no model request.
Saved chunk documents retain the exact sanitized prepared input and validated
answer; manifests retain the full model identity, rendered prompt, nonsecret
policies and request shape. These are sensitive local memory data, not publication
artifacts. Resume does not trust counts/digest fields alone: saved answers and
derived source links are revalidated against the current chunk.

## Spoken Input And Chunks

The Claude speech extender reads text blocks or legacy plain message strings.
Tool/thinking blocks, sidechains, peer/relay metadata and known host injections
are not human speech. Unknown native roles are reported, not guessed as owner
turns. Shared filtering cuts pasted fenced code, SVG and the observed decoded
PNG envelope; when a room dispatch includes multiple speakers, only the human's
turns remain in an owner row. This is known-format filtering, not a general
proof that arbitrary malicious instructions can be detected.

Genuine assistant self-note lines are removed from model input to avoid duplicate
extraction; fenced/quoted examples remain examples. The originals stay archived.
Marked self-notes are captured by `capture` (below), which the installed Stop hook
queues at the end of every turn. A message delivered inside a tool call is not
treated as speech, outgoing-message text is not extracted, and tool results
never qualify.

MemHub's host-envelope and credential sanitization is retained with attribution
in NOTICE, followed by the source password-shape masking heuristic. This is not
a guarantee that every possible secret is recognized. Reports count changed
rows, not a purported exact number of secret values. Raw archive sources are
not modified and must remain protected by the operator.

If an owner directly answers the assistant's numbered list, code records only
explicitly named agreements/rejections. Unnamed items stay unresolved; the
binding is labelled as code's interpretation, not an owner quotation. The
current grammar is English, as is the tested extraction prompt.

Chunks retain speaker labels and exact source rows and obey configured
character and conservative estimated-token limits. Estimates are not model
tokenizer results or final context acceptance. Unlike the old source's
oversized-row exception, this public preparer refuses a single oversized row
visibly instead of truncating it or preparing an out-of-bounds call. There is no
model call on this failure or on any dry-run.

Extraction apply implements model calls, structural output/source checks and
incremental verified notes. Separate memory commands implement topic placement,
repeat decisions, lessons and manual cards; they are not implied by successful
extraction. The separate `nightly` command chains entity links, stale cards,
naming, lessons and held-briefing rebuilding; see [OPERATIONS.md](OPERATIONS.md).
A completed three-stage window is not, by itself, a completed finishing chain.

## Marked Self-Note Capture

The separate capture command takes
only the assistant's genuine `fact:`, `mistake:` and `learned_lesson:` lines,
outside code fences and quotes, using the same configured detector that excludes
them from extraction. Native tool, meta, summary and sidechain records do not
become manual notes. Text is sanitized without altering its original archive
record. Named suffixes remain provenance, not automatic entity confirmations.

Capture must verify the original archive records before graph writes and use
stable assistant/source/ordinal identities. Replay adds no second vote or note;
deleted notes stay deleted, and corrected notes are never rewritten or re-embedded.
Incomplete placement uses the existing stored-vector retry path. A principle
links to the preceding mistake in the same source message, never a guessed event.
Configuration changes cannot silently reinterpret already captured inputs.

Manual captures may have no closed conversation window, as in the source. Do not
invent a compaction boundary: retain exact source IDs and report cited-only,
partial context. Source display must retain genuine marked lines in that case.
Dry-run and empty capture make no secret, model or store calls. Explicit apply
archives/verifies source, embeds without a notes-model call, stores and places.
Lessons and cards are built by the nightly run ([OPERATIONS.md](OPERATIONS.md));
the installed Stop hook queues turn-end capture (`capture-events`).

`--last-turn`, mutually exclusive with `--whole`, selects dialogue after the latest
native user prompt or latest compaction boundary. Tool-result rows, meta turns
and sidechains do not open a new turn. Non-meta injected/peer notifications,
wrapper-only and image-only user prompts do delimit turns, as in the source;
they do not thereby become speech or manual-note evidence.
Without either boundary it falls back to the current
stretch. Selection must keep the same archived-source identities as whole-file
capture and cannot change the replay identity of a note. This selection is not
itself native Stop scheduling or proof that the final message has been flushed.

```sh
anamnema capture --file /path/session.jsonl --instance assistant-name --session native-session
anamnema capture --file /path/session.jsonl --instance assistant-name --session native-session --apply
anamnema capture --file /path/session.jsonl --instance assistant-name --session native-session --whole --apply
```

The default previews only the stretch after the latest summary. `--whole` is an
explicit backstop over the entire transcript; stable source identities prevent
another insertion or trust vote on replay. `--capture-config` selects the complete
marker-kind, native-session-field and capacity policy. Its catalogue must agree
with `--speech-config`. Semantic policy changes refuse existing captures and need
an explicit migration; changing capacity limits alone does not reinterpret notes.
The replay identity includes only agent attribution, marker selection and text
sanitization dependencies, not owner-only speech filtering. Origin comparisons
validate typed values, so JSON formatting and omitted optional defaults do not
invalidate a replay. Only LF separates message lines; Unicode line-separator
characters within a marked line remain part of its text.
Apply uses the configured archive, graph and encoder, not the notes model.
Reports expose counts and note IDs, never captured text. Interrupted placement
is retryable; insertion and origin metadata are one transaction, but the entire
capture batch and its topic placements are not one atomic operation.

## Verification

Synthetic tests cover multiple summaries, explicit closure with the final row,
open tails, equal timestamps, native UUID reuse, exact repeats, mixed ownership,
invalid closing references and malformed input. Execution tests run the actual
CLI against a disposable MongoDB and a synthetic HTTP llama.cpp protocol fixture,
including source verification, two chunks, replay, changed options, interruption,
slow-worker shutdown, malformed replies, context overflow, identity changes and
corrupt saved notes. On a clean Debian 13 machine, with native services, Claude
Code events and a real Gemma 4 12B notes model reached through an attested
loopback tunnel, one synthetic window produced six notes, passed storage and
replay, and completed the finishing run and a source-backed correction.
