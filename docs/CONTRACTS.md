# Public Memory Contracts

Status: accepted implementation boundary; runtime export is not complete.
Schema version: 1. The synthetic fixtures define examples, not real conversations.

## Ingestion

An adapter reads native transcript records without changing the original file.
It preserves attribution, timestamps, stable source IDs and compaction boundaries.
Native tool output, provider scaffolding and injected instructions may stay in
the archive but must not be promoted to human statements for extraction.

Archive conversations have `uuid`, `sessionId`, `timestamp`, `type`, `message`
and `_kin` provenance. `_kin.substrate` and `_kin.source_synth` are retained as
schema-compatibility field names; they are not a dependency on private tools.
Compaction summaries carry their own UUID, session/time and `isCompactSummary`.
Session-closing markers are separate from summaries and include the final row.
Room/day support, if exported, must preserve every speaker and explicit timezone.

Stable IDs are derived from substrate, instance, session and native source
identity, not the machine's absolute file path. Identical re-import is idempotent.
The same ID with different canonical content is a conflict, never an overwrite.
Provenance includes source row numbers and content digest. A receipt counts as
proved only after read-back comparison of the complete imported record set.
A partial write stays resumable and is not a successful ingest. No truncate,
trim, delete, secret lookup or private launcher side effect belongs to ingestion.

Windows contain rows after the previous boundary up to the next boundary;
closing markers include the last spoken record. Equal timestamps require a
stable native sequence tie-break, never guessed chronological order. A future
adapter cannot be declared supported by adding an enum value alone.

## Providers

The installer selects `vulkan`, `cuda` or `api` for the notes model. Vulkan is
the first local implementation; CUDA is reserved and must fail clearly until
implemented. API selects an explicit provider, endpoint, model and revision;
there is no fallback to a named machine or private keyring.

Extraction exposes capabilities: generation, token counting if available,
input bounds, model identity and structured response validation. llama.cpp
metadata/template/tokenizer methods are optional capabilities, not universal
chat API endpoints. Resume identity includes provider/model revision, prompt,
settings and chunk content, so changing a model cannot reuse incompatible notes.
Unknown token counts must be stated and bounded by a documented input policy.

Judge roles are strong/middle/light, independently configured for linking,
merging, cards, naming and briefings. Command-line and API providers normalize
responses to validated JSON; timeouts, rate limits, malformed responses and
unsupported schemas fail visibly and preserve resumable work. A model error
is not an empty successful result. API secrets come only from explicit
environment selectors or a protected, operator-selected file and never logs.

Encoder and reranker are independent services with declared model/dimensions,
token limits, prefixes and indexed-result contracts. API extraction does not
silently remove them. Localhost is the default for archive, graph and inference.
Optional nginx provides TLS/forwarding only: the memory service checks its own
bearer and instance ownership even without a proxy. No Cloudflare credentials
are required for public remote access.

## Substrates And Hooks

Each substrate supplies both a native archive reader and a hook payload adapter.
Claude and Codex must each prove their own paths. Normalize session ID, project,
prompt, tool name/arguments/result and final assistant message. Development
instructions, system/developer messages, hidden reasoning, tool results and
transport envelopes never become attributed owner speech.

Required moments are session start, prompt, pre-tool, post-tool, tool failure,
turn end, pre-compaction and session end. Unsupported native events are reported
as limitations, not quietly skipped. The turn-end payload may arrive before the
native transcript contains the final message; capture must be idempotent when
the same turn is captured again at compaction or shutdown.

Identity is local and instance-owned, with an explicit boot file and no shipped
personal identity. Briefing and identity read producers use the same pending
directory/marker as the turn-end checker. Compaction writer, injector,
acknowledger, spool and verified archive sync share one path/marker contract.
Retrieval outages are visible without making an unavailable service a deadlock;
read-proof gates and explicit keep/drop decisions are separate from retrieval.

## Synthetic Acceptance Flow

The private-source bootstrap defines synthetic scenario, Claude and Codex
fixtures for expected statements, correction and ignored tool content. They
will enter the public tree with the implementation and executable checks,
not this docs-only initial commit. None uses a real person or private host.

The completed implementation must prove, separately for each substrate:

1. Import native records, compare read-back and repeat with no duplicate writes.
2. Enumerate the compaction window and final closed stretch exactly once.
3. Extract only attributed dialogue; synthetic tool and injected bait stay out.
4. Store a note with its exact source IDs; rerunning adds no duplicate note.
5. Find the note, open its original records and record a source-backed correction.
6. Build the corrected card/briefing and enforce the appropriate read token.
7. Capture turn-end and compaction notes once and sync the acknowledged snapshot.
8. Show visible failure and resumable state for unavailable archive, graph/model,
   malformed responses, interrupted runs and mixed-instance access.

Fixture validation is not a substitute for this workflow. Real Linux VM/API and
native Codex installation are required before the engine is called usable.
