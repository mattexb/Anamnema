# Conversation Processing

## Extraction Execution Contract

Explicit `process --apply` verifies all captured source records in the configured
archive before model access. Dry-run makes no model calls. Apply requires an
explicit local model file, hashes its stable bytes and checks that localhost
llama.cpp reports that same path. Native template/tokenizer endpoints check that
prompt plus reserved answer fits the reported context before generation. The
setup backend must currently be Vulkan; API/CUDA remain explicitly unavailable.

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
Only one worker is in scope for this bounded execution checkpoint; parallel
workers and complete operation scheduling remain later work.

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
in the report. Model options in the notes policy preserve the tested baseline;
this preview does not call a model or establish support for alternative models.

`--config` selects the complete setup/keyring/local-service configuration.
`--extraction-config` selects complete execution limits, pacing, native model
routes and result collection names. Results/attempt collections must be distinct
from the transcript archive. MongoDB must already contain verified copies, and
llama.cpp must already be running on the setup's localhost notes endpoint. This
command does not install either service. Missing/locked keyring, unmatched model,
missing source and failed requests are errors, not empty successful notes.

Model authentication is optional for the local endpoint. If needed, configure
both `model_keyring_entry` and `model_keyring_field` in extraction settings and
store that token in the project's keyring namespace using the manual guide.
Only its selector belongs in JSON; the token is sent as a bearer header, never
in arguments, logs, saved manifests or reuse identity. Credential-like keys in
request options are refused. Real Secret Service unlock is a separate acceptance
gate; automated tests use only a fixture lookup executable.

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
Native self-note capture itself still belongs to lifecycle integration and is
not activated by this preview. A separately delivered message inside a tool call
is not promoted to speech here; extraction of approved outgoing-message text
needs a reviewed public delivery contract. Arbitrary tool results never qualify.

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

Extraction apply now implements model calls, structural output/source checks and
incremental verified notes. Lessons, repeat/link processing, naming, cards and
briefings remain unfinished. Those downstream results are not implied by a
successful extraction or this preview.

## Verification

Synthetic tests cover multiple summaries, explicit closure with the final row,
open tails, equal timestamps, native UUID reuse, exact repeats, mixed ownership,
invalid closing references and malformed input. Execution tests run the actual
CLI against a disposable MongoDB and a synthetic HTTP llama.cpp protocol fixture,
including source verification, two chunks, replay, changed options, interruption,
slow-worker shutdown, malformed replies, context overflow, identity changes and
corrupt saved notes. Real-model quality/performance, GPU execution, authenticated
service installation and native Claude events remain untested acceptance gates.
