# Memory Storage And Retrieval

The storage/retrieval core and semantic finishing are implemented. Isolated real
encoder/reranker checks and a disposable Debian 13 native installation passed;
a synthetic full path also passed with real notes-model inference through the
explicit remote-attestation profile. These are compatibility checks, not quality
or hardware-performance benchmarks.

Lesson orchestration is implemented. All lessons, history, concept and
episode links, and unresolved note marks of one judgement commit together. A stop
before commit leaves its notes pending; a lost response after commit does not
cause normal replay to judge them again. Native lifecycle and installation
evidence are separate from that transactional contract; see SETUP and CONTINUITY.

## Ownership And Stores

MongoDB remains the authoritative archive and immutable extraction store.
Neo4j is the derived memory graph. No private database migration is attempted.
Every graph operation carries an explicit assistant instance. Notes, checks,
groups and passage pieces use project-owned labels and stable identities.
An existing note is never overwritten by an extraction replay. Corrections
are explicit, accountable events; replaying one check must not count twice.

Graph connections are direct localhost Bolt connections selected by setup,
with the username/password in the project's configured Secret Service keyring.
No production host discovery, credentials-file fallback or remote proxy.

## Preserved Algorithms

### Subject Naming Contract

Subject naming derives a short identity/activity label from the note and verified
source rows, never a new factual claim or a replacement for the note. Unknown
subjects stay empty and non-searchable. The source's word-boundary length cap,
missing-label reporting and note-first/source-only-for-disambiguation rules are
retained. Public generation uses the same verified local model transport and
configured non-thinking JSON request as extraction/lessons, with strict boolean
answers and credential masking. Pure judge implementation is in progress;
persistence is held for the source owner's confirmed concurrent-correction fix.
Naming must never mark corrected text named using a judgement of its old text.

Shadow subject vectors/word search will preserve text-only note vectors and
owner-scoped retrieval; unnarrated notes retain a text-only fallback. This is
existing source functionality, not a new semantic feature or a quality claim.

The storage/retrieval source baseline is KinTools 0.7.10; topic placement follows
the reviewed 0.7.12 fix. Preserve separate query/passage
embeddings, overlapping passage pieces for long notes, meaning/word/identifier
retrieval, reciprocal-rank fusion, similarity and trust floors, priority as a
tie-breaker, count-and-text-budget pagination and explicit reranker degradation.
The CPU encoder remains llama.cpp e5-small-v2 Q8_0 (384 dimensions, query/passage
prefixes, 512-token limit). The CPU reranker is MiniLM L6 ONNX, raw logits.
Synthetic tests prove contracts, not model quality or performance.

Owner-filtered candidate searches follow the reviewed 0.7.15 fix: grow the
global index budget until enough owned results are found or the index is
exhausted, rather than filtering a fixed global top-k. This applies to notes,
passage pieces, topics and words. Foreign candidates never leave the store.
The vector index itself remains approximate; no index rebuild is implied.

Correction outcomes keep the source distinctions: right adds positive evidence;
wrong blocks reading until cleared and may explicitly delete the note; partly
rewrites in place and refreshes vectors; outdated preserves the historical note;
pointer repairs its source references without a trust vote. A check retains
its sources and reason. Strict boolean inputs follow the source 0.7.10 fix.
A correction must invalidate dependent summaries/cards rather than deliver
stale derived text. Archived conversation records are never deleted by it.

## Acceptance

### Repeat And Retelling Contract

Repeat comparison is an explicit optional store stage, disabled until configured
with a judge model file. Cosine only selects candidates; a structured model
judgment decides `same_claim`, `same_event`, `changed` or `different`. Missing,
invalid or failed judgments leave a new note marked `merge_deferred`, with a
visible count and audit, never a guessed merge. Unknown time or scope is different.
Inactive or wrong-open notes cannot receive repeats.

A repeat becomes a separately keyed occurrence with its own text, sources,
window, session, date, judge identity and original insertion proposal. It never
rewrites the root's text, trust, checks or dates. Only same_claim contributes to
the displayed repetition count; same_event is a weightless retelling. Decisions
are retained in the graph. Occurrences commit before topic placement, so retry
after a placement interruption does not judge or count them again. A different
extraction run for an already merged window is refused. Failed writes remain
visible; no model/database outage is translated into a successful empty result.

Unmerge restores the occurrence as its own note, places it before removing the
occurrence, and retains the reason. A stop at either boundary is retryable.
Archive originals are never changed. Delivery-echo suppression belongs to the
native lifecycle integration; archive-only processing has no invented delivery log.

### Card Rebuild Contract

Cards are source-backed statements, not copied notes. Preserve the source's
separate rating: effective trust times logarithmic real-repeat count times age;
deduplicate identical text and take the best note per source window. Outdated
notes have no current-fact weight. Manual pins/moves outlive generated line IDs;
an ambiguous old move is reported for settlement, never guessed onto a new line.
The writer's identity lines and rated facts have separate ordering tiers.
All caps, age/open weights and note-selection shares belong to card policy.

Correction-dependent rebuilding must not serve a previously generated card or
lesson as current after its supporting note changes. Save/compare the exact
input revision when committing a rebuild, and reject concurrent stale output.
An unavailable model leaves a visible pending rebuild; it does not restore a
stale card or turn a correction into a second trust vote. Manual entity links and
bounded card rebuilding are implemented. Lesson model rebuilding is wired through
the shared engine, with one transaction per complete model judgement.

### Lesson Evidence Contract

A lesson counts real source-backed events, not notes or model deliveries.
Shared source records establish event equivalence; a model-equated event without
shared records remains explicitly judged, with its reasons retained. Stronger
configured evidence wins a class's evidence category. Wrong or superseded
evidence is held back from the support/counterexample vote. An event nominated
against a lesson cannot simultaneously support it; the lesson stops when valid
counterexamples outnumber standing support, without erasing its history.

Admission uses configured event/owner-word gates. A lesson restricted to one
external system stays there; a mechanism evidenced in general work remains
general. Exception routes are never cut by the ordinary-route budget. Lesson
versioning, evidence reads, correction rebuilding and multi-lesson interruption
recovery are integrated locally.

The reviewed lesson-version contract, retained in KinTools 0.7.21, means changes to statement,
mechanism, routes, severity, status or conflict retain a complete prior snapshot
and increment the version. Model/judging-time changes alone do not. The neutral
port scopes lesson identities by assistant, validates every episode against
its owned note's actual source records, and commits content/history/links together.
Corrected evidence invalidates current derived lessons transactionally; a stale
source token or generation revision cannot restore them. The bounded persistence
core commits an entire judgement's content/history/concept/episode links atomically. Complete episode
audit remains on the lesson after source-note deletion. Correction results retain
affected lesson IDs and report pending rebuilds; dirty lessons cannot be read as
current. Model rebuilding and CLI/HTTP operations are implemented locally. Every
input note is locked and checked before derived writes; stale evidence, a changed
lesson revision or an already-committed pending-only input refuses the whole batch.

Lesson generation retains labelled multi-lesson answers, one note's membership
in multiple lessons, configured evidence/severity/concepts, and source-scoped
event labels. An unresolved note is never forced into a nearest lesson. Model
input uses bounded attributed sanitized source dialogue with explicit partial
context; missing verified context fails visibly. Correction rebuilds must address
the existing lesson and compare source tokens and its generation revision.

### Topic Placement Contract

Topic placement follows the reviewed KinTools 0.7.12 recovery invariant:
discoverable and retryable, not atomic with note insertion. New notes carry
`placement_pending`; placement removes it in the transaction that creates all
requested memberships. A missing or foreign note/group rolls back the whole
placement. Exact identifier facets remain distinct from semantic topics.

The loader places saved notes by their current stored query vectors, never by
re-embedding an extraction during recovery. It joins the closest topic exemplars
above the configured similarity floor, capped by the membership limit, or makes
the note a new exemplar. Newly created exemplars remain visible to the same
loader before the vector index refreshes. Every lookup and cache is scoped to
the assistant instance. Replay must preserve corrections, trust and status;
deleted notes must not be resurrected. Notes predating the marker are not
silently backfilled. Repeat judging/occurrences remain a separate semantic stage.

Use real disposable Neo4j and MongoDB with synthetic conversations. Prove
insert/replay/conflict, meaning/word/id retrieval, exact original-source lookup,
correction/replay/history/deletion and ownership rejection. Test connection
outages and rollback rather than returning an empty search on failure.
The installed package must work without KinTools imports or private config.

The service must enforce its own assistant token on every data route; an
optional proxy is never the authentication boundary. Encoder/reranker servers
and their validated configuration belong to the product, not just their clients.
Full M4 completion also requires these services, integration with the extraction
store and correction-dependent semantic stages. A graph helper alone is not M4.

## Commands

`anamnema store --file SOURCE --instance ASSISTANT --session SESSION --closed
--run RUN` previews input without infrastructure. Add `--apply` to load already
saved extraction chunks from that exact run. It verifies the original archive
and saved answers before creating stable source-linked notes. It does not run
extraction again or overwrite a corrected note. Use the same source and
notes/speech/extraction policies as `anamnema process`.

`anamnema search --instance ASSISTANT "question"` searches the index.
`--facet kind=value` adds an explicit identifier and `--page` opens later pages.
`anamnema source --instance ASSISTANT --note ID` shows citation-centred dialogue,
not a model's retelling. Add `--raw` to include exact original records. This is
deliberately an explicit data-disclosure operation: original records can contain
sensitive text. There is no automatic
raw-archive search after an empty index result. Source reads verify record
digests and ownership and fail when their configured byte budget is exceeded;
they do not silently truncate an original or repair corruption. The text budget
applies to displayed dialogue; the separate byte bound applies to raw source
lookup/decompression. HTTP source requests likewise default to compact output;
`{"note": "NOTE_ID", "raw": true}` explicitly includes originals. A string
`"false"` is not a valid boolean for that switch.

`anamnema check --instance ASSISTANT --file CHECK.json` records a correction.
`anamnema history --instance ASSISTANT --note ID` shows its trace, even after
explicit deletion. A minimal partly-wrong check is:

```json
{
  "id": "retention-correction-1",
  "note": "NOTE_ID",
  "outcome": "partly",
  "reason": "The original conversation says fourteen, not seven.",
  "against": ["SOURCE_ID"],
  "rewrite": "Retention is fourteen days."
}
```

Replaying an identical check does not vote twice or re-embed a completed rewrite.
Reusing its id for different content is a conflict. Explicit deletion keeps the
check as a durable deletion marker, so extraction replay cannot resurrect it.
Rewrite replaces vectors and passage pieces atomically, invalidates group
summary markers and advances affected entity-card revisions. A configured card
builder refreshes entity cards after the check; missing or unavailable generation
is reported as pending derived work and dirty cards are not served as current.
With an explicit `--lesson-model-file`, checks also attempt lesson rebuilding.
Outages leave visible pending results, and replay retries only derived work.

Repeat processing is under active integration, not accepted yet. Its policy is
`config/merge.json`, selected with `store --merge-config`; enable it explicitly
and select `--merge-model-file` for the verified local llama.cpp judge. It uses
the notes listener and bounded transport policy, with its own prompt/options
identity. Missing model selection is an error, not an implicit fallback.
`anamnema unmerge --instance ASSISTANT --note OCCURRENCE_ALIAS --reason REASON`
restores an incorrectly merged occurrence. Source output includes bounded
separately cited occurrences and total same-claim/retelling counts.

Manual entity decisions use `anamnema entities --create NAME --type TYPE --reason
REASON --instance ASSISTANT`, `--drop NAME`, `--undrop NAME` or `--show NAME`.
Retirement reasons/history survive restoration; old links do not return by guess.
`--link NOTE --entity NAME --reason REASON` settles a note link; add `--pin` to
keep that evidence in card input selection. These commands use only the graph,
not an archive-wide name search or a private memory database.
`--pin-note NOTE --entity NAME --reason REASON` explicitly pins and settles an
owned note. `--unpin-note NOTE --entity NAME --reason REASON` removes that pair's
pin without deleting its settled link. Unpin requires an existing owned link;
it never creates one as a side effect. Both invalidate the current card revision,
refuse stale in-flight output and need no model call; rebuild is a separate action.
The shared entity HTTP route accepts `pin` and `unpin` operations with the same
name/note/reason fields. A separate `pinned` field is accepted only by `link`.

Manual card moves follow the reviewed KinTools 0.7.23 transaction boundary:
`anamnema card --entity NAME --promote STATEMENT --reason TEXT` and `--demote
STATEMENT` change the visible line, durable source-backed intent and front cap
together. A unique owned statement prefix is required; absent or ambiguous
selections fail without writes. Only a current live card may be moved. Moves
advance its revision while keeping the adjusted card readable, so in-flight
generation cannot overwrite the decision. They do not alter source notes or call
a model. Promotion uses the configured front capacity. `--forget-move MOVE_ID
--reason TEXT` removes owned intent and marks the card pending explicit rebuild;
it does not guess the replacement position. Rebuilds report unmatched moves.
The same operations use `operation` (`promote`, `demote`, `forget`), `statement`
and `reason` in the authenticated card endpoint; `statement` holds the move id
for `forget`. These fields cannot be combined with pagination or rebuild.

`anamnema build-cards --entity NAME --card-model-file MODEL_FILE --instance
ASSISTANT` explicitly generates a card. `anamnema card --entity NAME --instance
ASSISTANT [--page PAGE]` reads an already current one without a model call.
Use `--cards-config` for complete card bounds and `--extraction-config` for the
bounded llama.cpp transport. `check` and `serve` accept `--card-model-file` to
rebuild affected cards after corrections. Without it, corrections still commit
once and report pending card work; reading a dirty card fails explicitly.
If generation fails, retrying the identical correction retries the rebuild but
never adds another trust/usefulness vote. A concurrent source change refuses
stale generated output rather than overwriting the newer revision.

Development lesson commands are `build-lessons --note NOTE` (repeat `--note` for
a bounded batch) or `build-lessons --lesson LESSON` (explicit rebuild), with
`--lesson-model-file MODEL_FILE`. `lesson --lesson LESSON` reads current content.
Normal builds skip committed episode/unresolved decisions without model or encoder
calls. `build-lessons --note NOTE --rejudge` explicitly re-evaluates those inputs.
An unresolved-only judgement is durable too, never a silently discarded answer.
`lesson --historical` explicitly inspects pending content and history. `--cwd PATH`
supplies activity scope for admission. `--lessons-config` selects complete policy,
including the concept vocabulary, evidence categories, event minimum and external
system map; none are taken from a private workspace. `check`/`serve` may select
the same lesson model explicitly. A missing model never becomes an implicit call.
The lesson answer reserve is 4,096 tokens; the baseline notes model reports an
8,192-token context, so reserving that entire context for the answer would make
every nonempty prompt fail. Sampling/template defaults match notes extraction,
including `enable_thinking: false`. The native tokenizer still checks the complete
templated prompt plus reserve exactly; batch/candidate/source limits are maxima,
not a promise that their maximum contents fit. Oversized requests fail visibly
and require explicit policy adjustment, never silent clipping of notes/lessons.

`lesson-against --lesson LESSON --note NOTE --condition CONDITION --reason REASON`
records the learner's source-backed counterexample nomination. It verifies actual
archive references and retains the authenticated assistant as the nominator.
It is not a usefulness/delivery count or a person's verdict on the lesson. Repeated
nomination of one event adds no votes. A rewrite holds back both old support and
counterevidence; a reputation-only check does not erase unchanged counterevidence.
Reads expose evidence holdback, real-event counts, support/against vote and
admission, without removing the lesson's audit/history.

The configured `/v1/lessons` route accepts strict `read`, `build`, `rebuild` or
`against` operations under the service's own bearer. Historical access is a JSON
boolean, never a truthy string. Build-only `rejudge` is a strict JSON boolean too.
Source/model outages and stale concurrent reads fail explicitly. These local
development operations do not imply the native delivery lifecycle is complete.

All commands have their own `--help`. `--config` selects the setup document;
`--archive-config`, `--memory-config`, `--search-config`, `--retrieval-config`
and `--service-config` select complete nonsecret documents. Omitted flags use
packaged defaults, not a private home directory or source checkout.
`--source-config` selects window-reference and excerpt bounds; `--speech-config`
selects the same attribution/filtering policy used during processing.

## Local Services And Credentials

The graph uses setup's `services.graph` loopback port and `memory.json` database.
Project Secret Service selectors are `graph/local` fields `username`/`password`,
`encoder/local` field `token` and `reranker/local` field `token`. Archive access
uses the existing `archive/mongodb` field `uri`. The namespace and selectors are
configurable; [KEYRING.md](KEYRING.md) explains manual setup. Never put values in
JSON configuration or command-line arguments.

`anamnema serve --service-config SERVICE.json` starts the memory HTTP service.
The service configuration needs an explicit access list, for example:

```json
"access": [
  {"instance": "assistant-one", "entry": "memory/assistant-one", "field": "token"}
]
```

Every route, including health and OpenAPI, checks its own bearer. Caller-supplied
instance/proxy headers cannot select another assistant. The authenticated
`/openapi.json` lists typed request schemas and error statuses. The service
enforces request/response byte bounds, per-token rates, bounded in-flight work,
graph transaction timeouts and separate model-call timeouts. Errors do not echo
request bodies, secret values or database exception details. HTTP is restricted
to localhost; remote TLS/forwarding is separate future setup work.

`anamnema serve-reranker --model-dir MODEL_DIRECTORY --service-config SERVICE.json`
starts the bundled ONNX implementation on setup's reranker port. Install the
`reranker` package extra. Provide the model's `model.onnx`, `tokenizer.json` and
`special_tokens_map.json`; filenames/limits are in `config/reranker.json`.
Only operator-selected trusted model artifacts are loaded; no model download
or arbitrary remote inference code is executed. The encoder uses an independently
running localhost llama.cpp `--embedding` service. The native runtime installer
provides pinned acquisition and user-unit activation; see [SETUP.md](SETUP.md).

The baseline model families are [e5-small-v2](https://huggingface.co/intfloat/e5-small-v2),
the [Q8_0 GGUF](https://huggingface.co/ggml-org/e5-small-v2-Q8_0-GGUF), and
[MiniLM L6](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2).
Their licences remain their own; Anamnema's Apache licence does not replace
model terms. No weights are bundled. Changing encoder model/prefix/dimensions
requires a reviewed re-embedding/rebuild, not silent reuse of an existing index.
The client validates vector shape and finite values, not resident weight identity.

The verified baseline encoder file is `e5-small-v2-q8_0.gguf`, SHA256
`afdfb5c342d2efc2a051c426dd1d00913495d5f2bbceaea100d2f3892aa31cbc`, present in
[GGUF revision 14aa7ca](https://huggingface.co/ggml-org/e5-small-v2-Q8_0-GGUF/tree/14aa7ca88ca627e2ea0051929636561158e289ab).
The reranker baseline is `onnx/model.onnx` (fp32), SHA256
`5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a`, in
[MiniLM revision 233902d](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2/tree/233902d25c440f23af6f7d6e94d2946bac0bee0a).
Its `tokenizer.json` SHA256 is
`d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66`; its
`special_tokens_map.json` SHA256 is
`3c3507f36dff57bce437223db3b3081d1e2b52ec3e56ee55438193ecb2c94dd6`.
Both tokenizer files were checked against that pinned upstream revision.
These pins identify verified file bytes, not the original download or conversion
history. A bundle retaining `onnx/model.onnx` needs that relative filename in the
reranker configuration; no rename or flattening of the supplied bundle is needed.

## Briefing Evidence Contract

Briefings recall the selected subject's memory; they do not answer it as an
oracle. The briefing reader receives short note labels and bounded attributed
source rows. Every accepted claim cites only notes given to that particular
call. One unknown citation drops the entire claim, not just that citation.
Facts, instructions, lessons, rejected approaches, inferences, open questions,
low-relevance details and unresolved contradictions remain distinct. A later
date alone does not settle a contradiction.

`BriefLedger` accounts separately for recalled notes, explicit omissions,
suspect-only review suggestions, unaccounted notes and selected-but-unread notes.
A combine call may cite only notes actually cited by accepted partial claims;
lost citations remain visible. These pure accounting operations never correct,
vote, delete or rewrite a stored note. Worker errors and selection failures may
not be presented as complete recall.

Selectors are explicit `group:<id>`, `concept:<name>` or `note:<id>`, resolved
inside the requesting assistant's scope. Concept selection follows the public
lesson's episode notes. Direct notes lead, then concept notes, then group-only
notes, each deduplicated with its routes retained. Enumeration refuses a note
limit instead of silently truncating. Unresolved selectors stay explicit.

The renderer moves whole claims to further pages by the source priority order.
Contradictions and open points remain on page one, with an oversized-page
result when necessary. The configured size is explicitly a UTF-8 estimate,
not measured model tokens. File generation, native delivery and stack budgets
are not all accepted from ledger/render tests alone.

`BriefFiles` writes private, immutable, content-addressed bundles. Every page and
the separate review file is fsynced before a manifest commits the bundle. Reads
verify the owner, manifest identity, exact configured filenames and all file
hashes. A stop before the manifest leaves an uncommitted build; identical replay
can finish it without rewriting existing pages. Corruption is refused, not
silently overwritten. Resource bounds apply before publication and on reads.
The bundle store does not register a required read or claim native delivery.

## Independent Native Judgements

`NativeJudge` provides the fresh, structured Claude worker used by derived
briefing and card-growth work. Deployment supplies an absolute executable,
dedicated working directory and explicit model. The packaged `judge.json`
bounds input/output bytes and the entire invocation deadline. Evidence and the
answer schema are passed inline, followed by `--` and the prompt; there is no
shell or live-session attachment. The worker requests no tools, no MCP servers,
no inherited settings sources, disabled hooks and no session persistence.
These arguments are validated even for policy overrides.

Only a successful native result envelope with a nonempty reported model list
and a strict schema-valid `structured_output` is accepted. Free-text JSON and
error envelopes do not count as a judgement. The actual reported model names
are retained alongside the configured model/policy fingerprint. These checks
are protocol evidence, not proof of resident model weights or model quality.
Fixture-process tests exercise this contract. Separate guest acceptance on the
operator's own Claude login passed stdin/schema transport; neither user nor
project instruction-file canary appeared in the answer. This result is specific
to the tested CLI/profile, not proof about all future versions. Optional worker
registry configuration adds the guarded launch and SessionStart recovery described
in [CONTINUITY.md](CONTINUITY.md); arbitrary descendants still need supervision.

## Tested And Pending

### Source Context Contract

The source-display extension stores immutable window references when loading
verified extraction with `store --apply`: the exact ordered conversation IDs and closing/summary boundary,
verified against the original archive. Their derived key is the same common
window identity already carried by notes. Re-imported line numbers and equal
timestamps must never be used to reconstruct that order.

The compact display reuses the substrate speech reader, takes configured
speaker turns around citations and spend its text budget nearest citations
first. Tool/injected records remain raw sources, not invented human dialogue.
Context scanning and decompression remain bounded and limitations are explicit.
`context.status` is available, partial or unavailable; row clipping, scan bounds,
filtered citations and missing manifests are reported. No physical-line or
timestamp sorting is substituted for a missing captured-order reference.
Missing historical manifests produce an unavailable-context result, not an
archive-wide search or fabricated context. Replay `store --apply` with the same
original source, run and processing policies to backfill a missing verified
manifest without replacing corrected notes. Corrupt manifests fail rather than
being repaired by a read. Exact raw access remains explicit.

Tests exercise real disposable Neo4j/MongoDB, actual keyring-command routing
through a fixture executable, and native-shaped HTTP inference fixtures. A
generated small ONNX graph and tokenizer exercise real CPU inference and the
authenticated reranker route; this is not a MiniLM quality/performance test.
The private model measurements are not new Anamnema acceptance evidence.

Separate installed-wheel checks now exercise the pinned real e5-small-v2 Q8_0
encoder with llama.cpp 5aeac37a3 and the real MiniLM L6 ONNX bundle. Synthetic
questions/passages verify finite 384-dimensional normalized vectors, distinct
query/passage prefixes, long-note pieces, exact token trimming, raw-logit ranking,
and bearer refusal through actual HTTP. The isolated encoder process is stopped
afterward; a separate read-only ARM service smoke test passes too. These are
compatibility checks, not model-quality, latency or hardware-parity benchmarks.
No private memory data is used and no weights are bundled.

The current graph implements notes, explicit identifier groups, retryable topics,
repeat/retelling occurrences and bounded entity-card rebuilding with accountable
manual entity lifecycle/link decisions and atomic manual card move commands.
Entity-link persistence, subject-name search, the nightly finishing chain and
the explicit native installer are implemented and exercised. Native session
delivery is covered by the separate lifecycle acceptance. Source
context now retains the source algorithm's speaker-turn and citation-first text
budgets, with an additional bounded raw-record scan and explicit partial status.
These copy differences are intentional at this checkpoint and
must not be described as complete parity.

Neo4j search uses the source's Cypher 25 `SEARCH` syntax. The test image is
2026.10 Community; full-text `SEARCH` requires 2026.09 or later, as documented
in the [Neo4j manual](https://neo4j.com/docs/cypher-manual/current/clauses/search/).
Do not advertise support for older Neo4j versions from these tests.
