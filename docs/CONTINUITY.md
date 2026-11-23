# Claude Continuity

How Anamnema keeps a Claude Code session continuous: capture at the end of each
turn, required reads, compaction snapshots and their archive sync. On a clean
Debian 13 machine with Claude Code 2.1.330, the installed hooks passed identity
reads, final-turn capture, compaction, briefing delivery and cleanup after a
killed session.

## Capture Scheduling

Explicit capture scheduling uses a private, bounded queue separate from the
required-read journal. A Stop payload may precede its final transcript record:
stage only expected sanitized marker values and native source coordinates, never
provisional notes or invented archive identities. A worker waits within configured
bounds for verified last-turn records, then calls the existing capture writer.
PreCompact/SessionEnd capture uses the current stretch as a backstop. Failed or
unflushed requests stay pending with visible errors; no success is inferred from
starting a child. Completion requires verified archive-backed storage/placement.

The queue journal commits atomically under a short lock. One separate worker
lock serializes execution while allowing new events to enqueue; network work
must not hold the queue-journal or continuity-session lock. Death before a
completion record leaves the request pending and replay uses the same note IDs.
Queue ownership, native fields, paths, permissions, capacities, wait/deadline
bounds and result history are explicit configuration. `anamnema install`
registers the hooks and starts the watcher. A capture outage never creates a
required-read Stop gate.

`anamnema capture-events hook --deployment FILE --event stop` previews bounded
native JSON from stdin without queue/store writes. Add `--apply` to enqueue;
`precompact` and `end` select their matching native events. Stop without genuine
markers creates no request or queue directory. The native payload must match the
configured cwd and an allowed transcript root; it never chooses the assistant or
credential selectors. Apply errors emit visible `systemMessage` context and exit
successfully without a blocking decision, keeping capture separate from explicit
read debt. Enqueue success means queued, not stored.

`anamnema capture-events drain --deployment FILE` defaults to read-only status.
Add `--apply` for one foreground drain, or `--apply --watch` to poll for new work
within the configured worker runtime bound. Each due request is attempted at
most once per drain pass. Watch retries transient failures after configured
backoff within that runtime, and polls again on worker-lock contention. Later
requests can proceed past a failed one. Configuration changes or exhausted
attempts move exact requests to a separate terminal-failed list, not pending.
Nonzero failed counts produce a nonzero CLI
exit status. `status --deployment FILE` reports pending job IDs, event names,
attempt counts, safe error categories and bounded completion summaries.

`retry --deployment FILE --job ID --reason TEXT` previews an operator retry;
`--apply` moves a terminal request back to pending after its original sources or
configuration have been repaired. `--accept-config-change --apply` explicitly
clones it with current configuration while retaining the original failed request
and the same pinned event range. No automatic retry silently changes its scope.
Terminal records have their own configured quota and share the journal's byte
limit. If that finite capacity fills, the error is explicit and no exact request
is dropped.

`anamnema capture-events archive-failure --deployment FILE --job ID --reason TEXT`
previews one terminal request without opening the keyring or database. `--apply`
keeps its complete input and failure state in a separate configured Mongo
collection, verifies the full document on an independent read, then removes only
the unchanged local failed entry. The queue lock does not span network work.
Archive/write/read-back failure or a changed local entry leaves the request
intact; a concurrent pending retry is never removed. Distinct terminal versions
of the same retried job have distinct archive identities. Repeating a completed
archive operation does not add another history entry or claim capture succeeded.
`--archive-policy FILE` selects the collection, which may not reuse a native
archive collection. The archive uses the deployment's existing setup/keyring
and archive database, not a separate host or credential file. This is operational
failure preservation, not a retention/deletion policy for raw conversations.

The foreground hook parses the bounded transcript to pin its event. Its
registration timeout is `hook_timeout_seconds` from the queue policy (10 s); a
regression test runs the real translator on a 300 MiB transcript and requires
each event below `hook_budget_fraction` of that timeout (half by default). On a
clean Debian 13 machine, with a 300 MiB transcript: Stop 1.67 s, PreCompact
1.56 s, End 1.56 s.

The hook starts no child and no service: the watcher
(`anamnema capture-events drain --apply --watch`, installed as a user service)
does the work. `capture_installation` builds all three hook commands and the
matching watcher unit from the same deployment and queue policy, and
`anamnema install` starts and verifies the watcher before it registers the hooks.

Deployment JSON explicitly names `instance`, `state_root`, `cwd`,
`transcript_roots` and the nonsecret setup `config`; optional `archive_config`,
`memory_config`, `search_config`, `speech_config` and `capture_config` select
complete public policies. Credentials remain in the project keyring. An explicit
`--queue-policy` replaces the complete packaged `capture_queue.json` limits and
native-field policy. Pending requests pin the normalized deployment/configuration;
changes are reported, not silently reinterpreted. Operational result history has
a configured count cap, not an archive-retention policy.

At enqueue the stable transcript supplies a pinned start boundary. Stop keeps
that turn open only until the next native user/compaction boundary, allowing its
own final record to flush later; PreCompact/End also pin the last observed record.
Anchors contain source identity and physical line to distinguish exact duplicate
records. The child receives the same structured pin, not a moving last-turn or
current-stretch query. Later prompts and summaries cannot move its range.
Changed/trimmed anchors are a visible refusal, never a guessed fallback. A truly
empty observed stretch is explicitly pinned empty.

Only hashes of expected sanitized marker values enter the request. Transcript
records must supply matching multiplicity before storage. The worker runs the
installed capture CLI with bounded output/deadline and checks its selected-source
digest against preparation before recording completion. A later prompt or changed
source selection cannot be counted as that request's success. Process death
before queue completion leaves pending work; idempotent insertion and placement
handle retry after already-committed graph work. Reserved orphan atomic-write
temporaries are recovered only under the journal lock with an explicit bound.

## State And Ownership

A deployment configuration names one assistant instance, an absolute private
state root and explicit identity files. Claude payloads supply the native
session, never the owning assistant. Instance and session isolate every managed
directory. Source identity files are read only; only managed copies can be
removed. State and content have configured private modes and bounded sizes.
Symlinks and corrupt/foreign state fail visibly. Concurrent invocations refuse
a busy per-session lock; callers may retry rather than overwrite state.

An immutable managed content file is published before one atomic session state
record refers to it. That record holds required reads, the staged snapshot,
its applied flag and the latest acknowledged snapshot reference. The retained
spool is separate from the active journal.
Pending-read acknowledgements and the clearing of an applied snapshot are
committed in one state replacement. An acknowledged snapshot is first published
durably into a separate private per-session spool. The active journal keeps only
its latest acknowledged reference, not an ever-growing list. A stop between spool
publication and journal commit can repeat the same acknowledgement safely.
Temporary files are removed on failure. Cleanup removes only unreferenced managed
content; acknowledged snapshots remain until verified archive sync.
The configured spool item bound is checked before accepting a new staged
snapshot. A full spool refuses staging visibly without creating a Stop gate or
dropping retained snapshots. Legacy journal spools migrate without discarding
content; an over-limit legacy backlog needs archive sync before new staging.

## Required Reads

The first observed SessionStart, including resume or compact, merges identity
into a managed JSONL document with a fresh nonce only on its last line. Context names the file and
acknowledgement marker, not the nonce. An exact acknowledgement line in the last
assistant message settles the read. Repeated starts and resume retain outstanding
tokens and reissue their instructions instead of rotating or forgetting debt.
Already settled reads are not re-created by a repeated event for that session.
No actual read or comprehension is asserted from acknowledgement alone.

`anamnema continuity require --session ID --producer briefings --key SUBJECT
--file FILE --continuity-config FILE` registers an existing derived briefing (or
another explicitly named read producer) without generating it or modifying the
original. The managed copy ends in the same read-token format as identity.
Trailing source `read_token: ` lines are removed only from the managed copy,
including repeated stamps separated by blank lines, before adding its one active
JSON token. A token-only file creates no debt. Other content and unstamped
whitespace remain unchanged. Repeated registration of the same cleaned body for
the same producer/key retains its
outstanding token. A newer derived version supersedes that key's unread copy
atomically with a new token; old proof cannot satisfy the new version. This is
current-derived-document behavior, unlike an applied compaction snapshot, which
cannot be replaced before acknowledgement. Missing sources or a full pending
queue fail before creating debt. Identity is reserved for SessionStart. The
command returns read instructions, not a claim of native announcement or briefing
generation. SessionStart reissues all pending producers; Stop settles them
independently. An explicit registration can create a real read obligation before
the first SessionStart; mere hot registration without such content still cannot.
Existing development copies with both source and managed tokens are superseded
on their next announcement; a source-only token refresh does not rotate an
outstanding managed token. A new announcement after acknowledgement creates a
fresh obligation even if its body is unchanged.

## Compaction

### Producer Port Contract

The producer uses the speech attribution and sanitization
boundary, reset dialogue at the native compaction boundary, preserve explicitly
marked self-corrections, and omit private Recognition enrichment. Excerpts use
the reviewed UTF-8 cut algorithm, newest-first order and configured byte/file
bounds, including joining newlines. Header overhead is separate and bounded.

The producer must run only an explicitly configured fresh worker, outside the
session lock, with bounded output/deadline and private temporary inputs removed
on success or failure. Output must satisfy configured structural/image checks
before staging through the existing journal; applied unread snapshots cannot
be overwritten. Failures leave prior staged content intact. Native payload
translation and archive classification remain separate.

An explicit snapshot-staging operation validates the configured marker and exact
instance/session headers. Staging alone does not require a read: idle compaction
can occur before application. A compact SessionStart marks that snapshot applied
and issues its path and content hash. Stop requires the exact session/hash-bound
acknowledgement before clearing its active reference. A newer precompute replaces
an unapplied snapshot: that content was not shown to the session. Applied unread
content cannot be replaced; identical staging is retryable.
Repeated compact injection after acknowledgement does not invent a new read.
The worker must supply an actual semantic snapshot; missing, failed or malformed
output is never treated as a successful no-op PreCompact hook.

### Producer Commands

`anamnema continuity precompute --continuity-config FILE --session ID
--transcript FILE --trigger manual --worker-config FILE` explicitly runs the
producer and returns a staged-snapshot report. `precompact` takes the same
deployment/policy options, but reads session, transcript, trigger and cwd from
bounded native PreCompact JSON on stdin. Its cwd must match the deployment.
Success emits `{}`; failures are visible and exit nonzero. Neither command
registers hooks or changes native settings.

The worker deployment JSON contains `executable`, `cwd`, `input_root` (absolute
paths), and optional `model`. The input root must be private and outside managed
session state. The latest eligible assistant model wins; otherwise the explicit
model or native CLI default is used. No settings file is inspected to guess it.
`--producer-policy` and `--speech-policy` accept complete public configurations.
Producer policy owns transcript/output/deadline/file bounds and native keys.
Worker arguments must retain the fixed security floor: print-only, Read tool,
hooks disabled, no persistence/browser, no project settings or resume flags.
This is a CLI contract, not an OS sandbox around an arbitrary configured binary.
In particular, inherited user permission rules can grant Read access outside the
input directory. Directory options precede the fixed options, and `--` separates
the final prompt from variadic arguments. JSONL records split only on physical
LF; raw Unicode separators inside JSON strings remain part of that record.

Both worker pipes are drained under a combined byte bound and whole-run deadline;
failure kills/reaps the owned process group and removes private temporary inputs.
Worker output is withheld on failure because it can contain transcript material;
the error reports transport class or exit status. No network/model call holds
the live session lock. The producer stages through the same ownership/quota and
applied-snapshot protections as explicit snapshot staging. The transcript is
bounded and checked for changes before worker use, never modified or trimmed.
Genuine marked correction lines stay in this input; private incident-store
enrichment is absent. Without the worker `registry` configuration, a SIGKILL of
the parent or a power loss bypasses in-process cleanup and leaves the worker and
its input folder; with it, the next session start removes them (below).

On Claude Code 2.1.330 on a clean Debian 13 machine: SessionStart required the
identity read, the assistant read and acknowledged it, and Stop capture reached
the graph. A real `/compact` invoked the producer, the snapshot was applied at
the next SessionStart, and the assistant read and acknowledged it with no debt
left. A source-verified briefing was built, delivered and acknowledged. The
native judge's answer contained no instruction-file marker. With the registry
configured, the worker was killed together with its parent and the next
SessionStart removed its input folder and recovery record; no worker, record or
temporary directory remained. The nightly replacement briefing was delivered by
the `briefing-refresh` hook, with no reads left owed.

### Registered Worker Recovery

Optional `registry` configuration is shared by continuity, snapshot workers and
native judges. It names an absolute private records directory, explicit cleanup
roots and bounded record reads, scan count and lock wait. A unique intent is
written before launch under a registry lock. A separate executable launcher
checks its expected parent identity, arms Linux's parent-death signal, rechecks
the parent, records its own PID/start time and execs the worker. No Python runs
as a `preexec_fn` in a forked threaded process. Start and wait must remain in the
same thread for the worker's lifetime.

Snapshot intents remain until their per-invocation inputs are removed, not just
until the worker is reaped. Judges receive evidence on stdin and register no
cleanup folder; their configured working directory is never deleted. At
SessionStart, the sweep checks uid, private regular-file permissions, boot and
process start time. Live parents are skipped; reused PIDs are never signalled.
Folder removal rejects parent traversal and walks directory descriptors without
following links, including the root and its ancestors. Failed removal retains
the intent for retry and is not reported as success. Errors do not gate the
session, and logs contain counts rather than input text.

This protects registered direct workers on the tested Linux profile. The tiny
interval between creating inputs and recording intent can still leave an
unregistered folder after abrupt death. The sweep ends the worker's whole
session, including processes it started that moved to another process group or
outlived it: the worker leads its own session, its descendants keep its PID as
their session ID, and Linux never reassigns that number while any process
still uses it, so every member of that session is the worker's own. Each
member is signalled through a pidfd and only after its session, owner and
start time are checked again, so a member PID reused after the scan is never
hit; a worker PID now held by another process is never signalled. The record
is released only once the session is verified empty within a bounded wait;
otherwise it stays and the next start retries. Processes that leave the
session with their own setsid are outside this boundary.

## Commands And Failures

`anamnema continuity start --continuity-config FILE` and `stop` consume bounded
Claude event JSON on stdin and return native JSON. The configured event name
must match the invoked command. Start adds context; Stop blocks while explicit
reads remain owed, including when `stop_hook_active` is true. It does not bypass
unfulfilled debt to avoid a loop. Files remain available and actionable.

`anamnema continuity snapshot --continuity-config FILE --session ID --file FILE`
stages an explicitly supplied snapshot without invoking a model or touching a
native transcript. Configuration contains `instance`, `state_root`, and
`identity_files`; `--continuity-policy FILE` can replace the complete packaged
nonsecret policy. No user paths, identity contents or service accounts are
inferred. Native settings are neither read nor changed by these commands.

Every handler logs success/error to stderr without content or tokens. Start
errors add visible context. Missing identity sources or a missing staged snapshot
do not invent read debt or persist an error-only Stop gate; an existing required
read or applied snapshot remains owed. Stop before the first observed start has
no inferred debt, allowing registration into already-running sessions. An
identity-source failure does not prevent an available snapshot from being applied.
Corrupt/unavailable managed state still fails visibly rather than being reset.
Retrieval does not run in these handlers and cannot cause a required-read gate.
Retrying start after source repair creates the previously unavailable identity read.
State paths must have no symlink ancestors. Anamnema runs on Linux (Debian 13).

`anamnema recall-hook` runs on UserPromptSubmit: it reads the native JSON on stdin
and brings related notes into the conversation. `anamnema briefing-refresh` runs
on the same event and delivers a rebuilt briefing ([BRIEFINGS.md](BRIEFINGS.md)).
`anamnema install` registers both.

See the [official hook protocol](https://code.claude.com/docs/en/hooks) for the
context-only SessionStart and blocking Stop envelopes.

## Snapshot Archive Sync

`anamnema continuity sync --session ID --continuity-config FILE` defaults to a
read-only local preview. It does not obtain credentials, connect to Mongo, create
state directories, migrate journals or delete files. `--apply` explicitly enables
the shared project keyring/Mongo path (`--config`, `--archive-config`).
`--snapshot-archive-policy` selects a complete nonsecret snapshot collection and
batch policy. Snapshots use their own collection in the same archive database;
they are not native compaction summaries or extraction dialogue.

Sync copies a bounded set of immutable spool files under short session locks,
releases the lock before network work, inserts only if absent and independently
reads back exact text, hashes and assistant/session ownership. Only verified,
unchanged files are removed under the session lock. Archive outage, mismatch,
interruption or concurrent change retains the local copy. Retry is idempotent;
one verified file can be removed while a later failing file remains retryable.
Malformed local spool files are retained and listed by basename in `refused`;
scanning up to the configured spool capacity lets later valid files fill the
upload batch instead of repeatedly stopping at the same corrupt entry. Session
journal/lock failures and archive outages still fail visibly. A refusal needs
operator repair; it never authorizes deletion of unverified content.
Spool copies preserve the original managed snapshot's modification time for
provenance. Existing development spools may retain only their previously stored
timestamp; no older missing timestamp is invented. After upload, sync also tries
a configured bounded batch of pending snapshot pairs, including when no local
spool files remain. Only an independently verified native summary of the same
assistant/session inside the configured time window can pair. Multiple current
candidates become ambiguous; none stays pending until the session's archived
summaries advance past the window, then becomes unpaired. Wall-clock age never
expires a pending snapshot. Counts/bytes above the configured bounds fail rather
than turn a truncated set into a false unique match. Invalid or timezone-naive
summary dates keep the session's pairing pending; `repair_required` identifies
the owned summary records and `blocked_snapshots` counts the affected batch.
An invalid date cannot safely be assigned to a smaller window; manual archive
repair is required, and UTC is not guessed. Pairing failure returns a separate
`pairing_error` beside completed upload/removal counts, with `pairs` null; it
does not imply that completed removals were rolled back. Pairing describes the
summaries currently archived, not proof that no
later import can contain another candidate. Snapshots remain distinct from the
native summaries they reference.
