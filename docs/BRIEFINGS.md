# Briefing Builds

A briefing build resolves typed owned selectors, verifies attributed archive
sources, prepares bounded sanitized batches and sends each to a fresh tool-free
`NativeJudge` (a Claude Code worker) over stdin. On Claude Code 2.1.330 on a clean
Debian 13 machine, the judge received its prompt on stdin and answered in the
required structure, and no instruction file leaked into its answer; a
source-verified briefing was built, delivered as a required read and
acknowledged ([CONTINUITY.md](CONTINUITY.md)).

`BriefBuilder` uses strict schemas and short labels. Unknown citations drop the
whole claim; failed batches remain unread. Combining partial reads preserves
explicit accounting of lost citations. Unknown selectors, missing answers and
worker errors cannot produce a ready status. A failed or partial build remains
inspectable in an owner-only content-addressed bundle, with all pages and review
details durable before the manifest commits. Bundle read-back verifies ownership,
filenames and checksums independently. Building never corrects or votes on notes.
Source failures are per-note: affected notes remain unread, healthy notes still
reach the model, and even an all-unavailable build saves an inspectable failed
report without calling a model.

The deterministic renderer retains whole claims across pages. Contradictions
and open points stay on the first page even when that exceeds the estimate;
oversized pages are reported. Token counts are estimates, not tokenizer results.
When the first page is over its size target, one condense call rewrites it
denser (`briefing_condense.py`). Each condensed
claim names the draft claims it carries (`d1`, `d2`, ...). Only a claim whose
every citation the draft already had is accepted, and only an accepted claim's
`carries` count: a dropped claim written with a kept claim's text carries
nothing. Every draft claim no accepted claim carries goes to the further pages,
never away, even when a kept claim cites the same note. Notes the condensed page
no longer cites are listed as left out for size. A failed condense is an
integrity problem (partial) and the draft first page is kept. The bundle
metadata says `condensed`.

`BriefStack` reserves a page's estimated tokens before registering its required
read with `Continuity`. A failed announcement leaves a visible reservation;
replay never charges it twice. The hard ceiling refuses before creating read
debt, and the soft ceiling is reported as a warning. Only ready, checksum-verified
pages are deliverable. The managed copy is checksum-bound too, so a changed file
between bundle verification and copying is refused. Dropping a page frees its
future holding budget, keeps its archived files, and cannot discharge an existing
read acknowledgement. Explicitly delivering that same page again reserves it
afresh; an already owed proof remains the same, while a previously acknowledged
page gets a new required-read token. An accepted reservation may replay even if
the current policy ceiling has shrunk; the over-ceiling condition is reported,
and new reservations remain refused. Empty pages are controlled refusals before
state or debt. Token estimates are recalculated from verified page byte sizes
using the current density policy. Stack inspection creates no state. These estimates govern
held pages, not the complete native context token count.

## Commands

`brief --request FILE --briefing-root DIR --instance NAME` validates a bounded
JSON request with `subject` and typed `selectors` without opening infrastructure.
Explicit `--apply --judge-config FILE` builds it using configured local archive
and graph access. The judge file names an absolute executable, dedicated cwd and
explicit model. A failed or partial result returns nonzero and retains its
bundle. This is generation, not delivery.

`briefing read` verifies a stored bundle and returns its page path and metadata.
`briefing status` inspects a session stack without creating anything. `deliver`
and `drop` default to read-only previews; explicit `--apply` changes the stack.
Session operations require explicit continuity configuration, stack root and
session identity; the assistant must match that deployment. Delivery returns
the managed required-read instruction, never the nonce itself.

`brief --rebuild BUNDLE` refreshes that immutable bundle's declared subject and
selectors. An unchanged ready note snapshot under the same briefing, speech and
judge policy reuses the verified bundle without another model call. A changed
snapshot or failed/partial build is regenerated; old bundles remain intact.

`anamnema nightly` rebuilds every bundle a session still holds; a changed ready
rebuild is recorded as that bundle's replacement (`briefing_refresh.py`). On the
next prompt the `briefing-refresh` UserPromptSubmit hook delivers the replacement
page through `BriefStack.deliver` (a new required read) and drops the old page.
It runs on UserPromptSubmit because SessionStart and Stop already hold the
continuity lock; a failure is a visible notice and never blocks the prompt.
`anamnema install` registers that hook and the nightly timer (every day at 03:30,
`nightly_timer.json`); `anamnema nightly --nightly-config FILE` runs the same
chain by hand. On a clean Debian 13 machine with a real Gemma notes model, a
source-backed correction advanced a card revision, the nightly run rebuilt the
held briefing, and the prompt-refresh hook delivered the replacement page to the
Claude Code session, which then owed no reads.
