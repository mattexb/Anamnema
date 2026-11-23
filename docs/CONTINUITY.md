# Claude Continuity

This is the first-profile lifecycle contract. Disposable Debian 13 acceptance
exercised installed hook registration, identity reads, final-turn capture,
compaction, briefing delivery and guarded parent-death recovery with Claude Code
2.1.330. Explicit snapshot archive sync and the bounded producer are implemented
below; fixture tests alone do not establish native compatibility.

## Capture Scheduling (Local Implementation)

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
bounds and result history are explicit configuration. Starting or registering
live hooks remains a separate authorization/acceptance gate. Capture outages
must not create a required-read Stop gate.

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

The foreground hook currently parses the bounded transcript to pin its event.
The native registration timeout must use `hook_timeout_seconds` from queue policy,
not a shorter unrelated default. On the development host a 300 MiB synthetic
native transcript took 1.307 s for Stop and 1.204 s for PreCompact/End, with peak
RSS about 445 MiB. A regression runs the real translator on that size and requires
each event below `hook_budget_fraction` of its 10 s default timeout (half by
default). This is a measured host result, not a hardware-independent guarantee;
the installation acceptance gate must repeat it on the target before relying on
the large-transcript hook bound. The disposable Debian 13 guest repeated the
300 MiB check: Stop 1.673 s, PreCompact 1.563 s and End 1.562 s, all below its
5 s margin within the 10 s registered timeout. Temporary files were removed;
the parser-only check used no models, services or credentials.

The hook does not start an unobserved child or a service. A foreground watcher
must already be running, or an explicit drain must follow. The native installer
starts and verifies the watcher before registering hooks; registering only a
hook does not establish working automatic capture. The pure
`capture_installation` assembly now produces all three hook commands and their
matching watcher unit from the same deployment and queue policy. It uses the
latency-tested timeout rather than an unrelated default, but execution still
must start and verify the watcher before merging those settings. Tests invoke
the generated commands with synthetic native input; the separate guest acceptance
also exercised installed hooks and capture against the real stores.

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

The source is Hooks at `c0019f256bc6c51c7a8401fa7381f069c940ab66`:
SessionStart `require_reads.py` and `inject_compaction_snapshot.py`, Stop
`check_required_reads.py` and `acknowledge_compaction_snapshot.py`. No private
identity files, Recognition enrichment or handoff catalogue are copied.

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

The public producer port is pinned to Hooks
`8d23cfe8713fe2f529bff227fe4523b8ed2fccf1`, independently verified against the
reviewed source digest. It will use the existing speech attribution/sanitization
boundary, reset dialogue at the native compaction boundary, preserve explicitly
marked self-corrections, and omit private Recognition enrichment. Excerpts use
the reviewed UTF-8 cut algorithm, newest-first order and configured byte/file
bounds, including joining newlines. Header overhead is separate and bounded.

The producer must run only an explicitly configured fresh worker, outside the
session lock, with bounded output/deadline and private temporary inputs removed
on success or failure. Output must satisfy configured structural/image checks
before staging through the existing journal; applied unread snapshots cannot
be overwritten. Failures leave prior staged content intact. Native payload
translation and archive classification remain separate. No native worker or live
registration acceptance is inferred from fixture subprocesses. Separate native
guest evidence is described below.

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
enrichment is absent. Native invocation and model quality are separate acceptance.
Without an explicit worker registry, parent SIGKILL or power loss can bypass
in-process cleanup. Catchable-failure tests alone do not establish orphan recovery.

A bounded native acceptance used one fresh print worker with synthetic temporary
files and inherited user permissions. Actual Read tool results confirmed the
positional prompt after `--` was accepted and a synthetic file outside `--add-dir`
was readable. SIGKILL of the transport's owning parent before the final result
left the worker running; the independent fixture observer then reaped it and
removed its temporary state. Diagnostic stream-JSON output, an explicit synthetic
system prompt and empty strict MCP configuration made the evidence observable
without private integrations. This is not normal text-mode snapshot staging,
live registration, input confinement or automatic orphan recovery acceptance.

A later disposable Debian 13 guest acceptance exercised the installed native
registration: SessionStart required the synthetic identity read, the real
assistant read and acknowledged it, and Stop capture reached the isolated
graph. A real `/compact` invoked the producer, applied its snapshot at
SessionStart, and the resumed assistant read and acknowledged it without
remaining debt. A source-verified briefing was also built, delivered and
acknowledged through that native session. The guest's outside-directory Read
was denied; that result does not override the earlier permission-dependent
observation or establish an OS sandbox. A separate native-judge canary check
returned neither the user nor project instruction-file marker.

The initial guest parent-SIGKILL probe reproduced the surviving-worker limit
above. A later guarded native run passed: the kernel killed the real worker,
and the next native SessionStart removed its input folder and recovery record.
The fixture confirmed no worker, record or temporary test directory remained.
The resumed native session also held the nightly-generated replacement briefing
and ended with no required reads remaining; the prompt refresh hook delivered
that replacement, rather than a manual injection by the test driver.

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
unregistered folder after abrupt death. The sweep does not chase a process group
whose leader has disappeared; service supervision remains responsible for
arbitrary descendants. This is not a scan or deletion of unrelated processes.

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
Retrieval does not run in this slice and cannot cause a required-read gate.
Retrying start after source repair creates the previously unavailable identity read.
State paths must have no symlink ancestors; on macOS this excludes the usual
`/tmp` and `/var` aliases. The current implementation target remains native Linux.

See the [official hook protocol](https://code.claude.com/docs/en/hooks) for the
context-only SessionStart and blocking Stop envelopes. Payload replay and installed
CLI tests establish this bounded interface, not native lifecycle support.

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
