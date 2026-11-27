# Public Memory Contracts

Anamnema implements setup, Claude Code archive import, extraction, storage and
retrieval, semantic finishing (links, cards, naming, lessons, briefings), the
native Claude Code lifecycle and the installer. Schema version: 1. The synthetic
fixtures are examples, not real conversations.

The memory boundary is described in [MEMORY.md](MEMORY.md). The same reusable
engine serves local commands and authenticated HTTP. Mongo owns original source
records and saved extraction; Neo4j owns derived notes and correction history.
A source-backed
correction never changes or deletes its archived conversations.

Subject naming is described in [NAMING.md](NAMING.md): a separate representation,
text/source-bound writes after locking, and immediate correction fallback.
`anamnema nightly` runs it.

Verified window references preserve captured source order separately from
mutable file-line positions. Source display uses the common speech reader and
bounded context around citations; raw originals are an explicit CLI/HTTP option.
Missing/corrupt context cannot trigger guessed ordering or archive-wide search.

The loader stores a durable topic-placement marker with each new note, then
places it by its persisted query vector. Placement is all-or-nothing within its
own transaction, not atomic with extraction insertion. Failed placement remains
retryable without rewriting corrected properties or re-embedding known notes.
Topic identities cannot collide with user-supplied explicit identifier facets.

One project exposes the `anamnema` command, with its own transcript importer and
keyring tool. The installation target is Debian 13 trixie, native services on one
machine; see [SETUP.md](SETUP.md).

## Ingestion

An adapter reads native transcript records without changing the original file.
It preserves attribution, timestamps, stable source IDs and compaction boundaries.
Native tool output, provider scaffolding and injected instructions may stay in
the archive but must not be promoted to human statements for extraction.

Archive records have a stable `_id`, instance, substrate, session, kind, timestamp,
payload digest, physical source line and the complete native source object (or
losslessly compressed JSON). Native `uuid`, `sessionId`, `type`, `message` and
summary flags remain inside that object without metadata overwriting them
([ARCHIVE.md](ARCHIVE.md)).
Session-closing markers are separate from summaries and include the final row;
closure currently requires explicit operator attestation, not guessed EOF.
Room/day support, if exported, must preserve every speaker and explicit timezone.

Stable IDs derive from substrate, instance, session, kind, timestamp and canonical
source content, not the machine's absolute path or physical row. Native UUIDs
can repeat across valid source variants and are not unique archive keys.
Identical re-import is idempotent.
The [processing preview](PROCESSING.md) consumes common records in captured
source order, not timestamp-only ranges. Summaries close preceding conversation;
explicit markers include their referenced final row. Open tails remain pending.
Its file-based dry-run neither verifies archive storage nor generates notes.
The speech extender reports tools/injections/unknown roles as exclusions; shared
code filters/redacts kept rows, binds explicit numbered answers and bounds chunks.
Reports retain source references without conversation text; preview estimates
are not tokenizer/context acceptance. Explicit apply verifies archived source,
then uses native llama.cpp template/tokenizer endpoints before generation.
Saved chunks retain exact prepared input and sanitized, strictly validated notes;
attempt manifests preserve model/prompt/request/policy provenance. Reuse checks
content and source references, not just a saved success flag. See PROCESSING.md.
The same ID with different canonical content is a conflict, never an overwrite.
Provenance includes source row numbers and content digest. A receipt counts as
proved only after read-back comparison of every distinct imported record.
A partial write stays resumable and is not a successful ingest. No truncate,
trim, delete or private launcher side effect belongs to ingestion. Dry-run never
reads secrets; explicit archive apply resolves only the configured project keyring.

Windows contain rows after the previous boundary up to the next boundary;
closing markers include the last spoken record. Equal timestamps require a
stable native sequence tie-break, never guessed chronological order. Supporting
another assistant takes its own adapter and its own acceptance run, not an enum
value.

## Providers

The setup profile selects `vulkan`, `cpu`, `cuda` or `api` for the notes model.
CPU and Vulkan share the localhost llama.cpp transport. `cuda` reports
unsupported. API selects an explicit provider, endpoint, model and revision;
there is no fallback to a named machine or private keyring entries.

Extraction exposes capabilities: generation, token counting if available,
input bounds, model identity and structured response validation. llama.cpp
metadata/template/tokenizer methods are optional capabilities, not universal
chat API endpoints. Resume identity includes provider/model revision, prompt,
settings and chunk content, so changing a model cannot reuse incompatible notes.
Unknown token counts must be stated and bounded by a documented input policy.

The tested notes model is Gemma 4 12B through llama.cpp. Each model stage
(extraction, merge, cards, lessons, naming, briefings) takes its own explicit
model file or judge configuration. Command-line and API providers normalize
responses to validated JSON; timeouts, rate limits, malformed responses and
unsupported schemas fail visibly and preserve resumable work. A model error
is not an empty successful result. Secrets come from the project keyring
([KEYRING.md](KEYRING.md)), never from arguments, logs or reports.

Encoder and reranker are independent services with declared model/dimensions,
token limits, prefixes and indexed-result contracts. API extraction does not
silently remove them. Localhost is the default for archive, graph and inference.
Optional nginx provides TLS/forwarding only: the memory service checks its own
bearer and instance ownership even without a proxy. No Cloudflare credentials
are required for public remote access.

## Substrates And Hooks

One reusable substrate base contract supplies common records and normalized
events to the shared memory core. Per-substrate extenders implement transcript
reading and native event translation, not copies of the engine. Claude Code is
the supported substrate. Normalize session ID, project,
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

The bounded [continuity core](CONTINUITY.md) implements identity required reads,
snapshot staging/application/acknowledgement and verified archive sync/pairing.
The producer's explicit precompute command and bounded PreCompact stdin adapter
generate through an explicitly configured fresh worker, then stage through the
same journal. `anamnema install` registers the hooks: SessionStart, Stop,
PreCompact, SessionEnd, UserPromptSubmit (recall and briefing refresh) and
PreToolUse (lessons that fit the action). Lessons arrive by fit, never by a fixed
count: every admitted owned lesson near the action comes, bounded only by the
hook's context bytes. Harmless reads are skipped. Post-tool and tool-failure
moments are not yet registered.

## Acceptance Flow

Before 0.1.0 was published, steps 1 to 7 passed on a clean Debian 13 machine with
Claude Code 2.1.330 and a real Gemma 4 12B notes model; the failure cases of step
8 are covered by the test suite with disposable stores:

1. Import native records, compare read-back and repeat with no duplicate writes.
2. Enumerate the compaction window and final closed stretch exactly once.
3. Extract only attributed dialogue; synthetic tool and injected bait stay out.
4. Store a note with its exact source IDs; rerunning adds no duplicate note.
5. Find the note, open its original records and record a source-backed correction.
6. Build the corrected card/briefing and enforce the appropriate read token.
7. Capture turn-end and compaction notes once and sync the acknowledged snapshot.
8. Show visible failure and resumable state for unavailable archive, graph/model,
   malformed responses, interrupted runs and mixed-instance access.

An adapter for another assistant has to pass the same flow with its own records.
