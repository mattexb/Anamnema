# Public Memory Contracts

Status: setup planning/wizard, Claude archive import and bounded localhost note
extraction implemented locally. Semantic processing and native lifecycle integration
are not complete; real-model/installer acceptance remains pending.
Schema version: 1. The synthetic fixtures define examples, not real conversations.

One project exposes the `anamnema` command. Reuse and neutralize the existing
transcript importer and keyring internally; users do not assemble private tools.
The first installation target is Debian 13 trixie, native services on one machine.
The setup planning contract and current limitations are in [SETUP.md](SETUP.md).

## Ingestion

An adapter reads native transcript records without changing the original file.
It preserves attribution, timestamps, stable source IDs and compaction boundaries.
Native tool output, provider scaffolding and injected instructions may stay in
the archive but must not be promoted to human statements for extraction.

Archive records have a stable `_id`, instance, substrate, session, kind, timestamp,
payload digest, physical source line and the complete native source object (or
losslessly compressed JSON). Native `uuid`, `sessionId`, `type`, `message` and
summary flags remain inside that object without metadata overwriting them.
This intentional public-envelope difference is documented in [ARCHIVE.md](ARCHIVE.md)
and the tracker; it does not migrate the private archive in place.
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
stable native sequence tie-break, never guessed chronological order. A future
adapter cannot be declared supported by adding an enum value alone.

## Providers

The installer selects `vulkan`, `cuda` or `api` for the notes model. Vulkan is
the first local implementation; CUDA is reserved and must fail clearly until
implemented. API selects an explicit provider, endpoint, model and revision;
there is no fallback to a named machine or private keyring entries.

Extraction exposes capabilities: generation, token counting if available,
input bounds, model identity and structured response validation. llama.cpp
metadata/template/tokenizer methods are optional capabilities, not universal
chat API endpoints. Resume identity includes provider/model revision, prompt,
settings and chunk content, so changing a model cannot reuse incompatible notes.
Unknown token counts must be stated and bounded by a documented input policy.

Keep our tested models/settings as the baseline; researched alternatives are
documented as untested until exercised. Do not promise generic provider support.
Judge roles are strong/middle/light, independently configured for linking,
merging, cards, naming and briefings. Command-line and API providers normalize
responses to validated JSON; timeouts, rate limits, malformed responses and
unsupported schemas fail visibly and preserve resumable work. A model error
is not an empty successful result. The neutralized project keyring provides the
reviewed secrets path. Its setup/unlock and unattended use must be documented;
never distribute private entries or expose values in arguments, logs or reports.

Encoder and reranker are independent services with declared model/dimensions,
token limits, prefixes and indexed-result contracts. API extraction does not
silently remove them. Localhost is the default for archive, graph and inference.
Optional nginx provides TLS/forwarding only: the memory service checks its own
bearer and instance ownership even without a proxy. No Cloudflare credentials
are required for public remote access.

## Substrates And Hooks

One reusable substrate base contract supplies common records and normalized
events to the shared memory core. Per-substrate extenders implement transcript
reading and native event translation, not copies of the engine. Claude is first;
Codex and other substrates follow later and must prove their own paths.
Normalize session ID, project,
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
native Claude installation are required for the first target. The later Codex
acceptance timing will be reviewed separately; no Codex support is implied now.
