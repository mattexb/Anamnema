# Operations Contract

The installed `anamnema` command owns import, processing, storage, review,
correction and recovery. `batch-window` owns archive/extract/store; `nightly`
owns the separate finishing chain. Neither an individual successful stage nor a passing fixture
test means the whole memory batch is finished.

## Batch Recovery

The batch driver will use an explicit assistant, session, closed input and
validated deployment/profile. A job pins its input identity and ordered stage
plan. Each stage delegates to the existing application operation rather than
copying its memory algorithms. Status inspection and dry-run must not create
state, contact stores, read credentials or launch a model.

The durable local job journal records the running stage before execution,
then records its verified result before moving forward. One worker owns a job;
network/model work must not hold the short journal lock. A failure stops later
stages. A stop after a stage committed but before its result was journaled
replays that stage through its existing idempotency contract. A running marker
after process death is retryable, not success. Changed input/profile/stage plans
refuse automatic resume. Job completion requires every required stage, including
finishing and derivation, not just successful extraction.

Worker execution needs bounded input/output and a whole-invocation deadline.
The service supervisor must own descendant cleanup; standalone parent-death
cleanup is not inferred from normal exception handling. Read-only job reports
and nonzero failure exits must agree. The first supported driver is local;
no private SSH runner, home host, job endpoint or distributed queue is implied.

The implemented `BatchJournal` persists this ordering and retained
numeric attempt counters. It requires an immutable prepared input and verifies
that input before starting a stage and accepting its success. It must not be
pointed at a moving native transcript tail. A per-job OS worker lock spans
execution; separate state locks cover only atomic transitions. A stale worker
handle cannot advance state. Attempt/metric/state/input limits are configured.
`PreparedInputs`, `ArchiveWindow`, `WindowStages` and `BatchRunner` now connect
the actual archive, extraction and storage engines. The `batch-window` command
can prepare closed windows, run/resume those three stages and inspect their
journals without a live model or input file. Finishing stages and the complete
nightly command are separate from that window journal. A completed window job explicitly reports
`nightly_complete: false`.

### Prepared Window Inputs

Batch preparation will pin one already closed conversation window per job,
including its exact captured record order and closure boundary. It must not
close a live tail merely because the file currently ends. A content-addressed
private compressed payload avoids repeatedly copying the full transcript; a
small immutable manifest binds the owner, source identities and payload digest.
The original transcript is never edited.

The archive stage must import and independently verify that exact window before
extraction. Only after that stage's success is journaled may its temporary raw
payload be removed. Later stages reconstruct the pinned records from the owning
archive, never discover them by timestamps or today's source line positions.
An interrupted archive stage retains its input and can replay. Missing/corrupt
payloads or archive records are visible failures, not an empty successful batch.
This boundary is implemented and exercised with actual disposable stores and
HTTP fixture models. Separate disposable-guest acceptance exercised a real
notes model through authenticated loopback SSH transport, six-note extraction,
storage/replay, finishing, source-backed correction and card/briefing refresh.
That synthetic compatibility run is not a quality or performance benchmark.

### Window Commands

```sh
anamnema batch-window prepare --deployment FILE --file TRANSCRIPT --session ID
anamnema batch-window prepare --deployment FILE --file TRANSCRIPT --session ID --closed --apply
anamnema batch-window run --deployment FILE --input INPUT_ID --apply
anamnema batch-window status --deployment FILE --job JOB_ID
```

The deployment identifies one assistant, separate absolute input/job roots, an
explicit local notes-model file and optional complete nonsecret setup/policy
files. No private host or runner is discovered. Prepare defaults to read-only;
only real summary boundaries or explicit session-ended attestation close work.
Apply writes content-addressed private compressed windows, not the whole
transcript on every event. Run defaults to metadata-only preview with no
infrastructure. Actual run uses the existing model, source and graph engines;
the journal binds deployment, model and policy fingerprints. `--resume JOB_ID`
refuses a changed pinned plan. Ordinary failures name the recoverable job ID.

After recorded success, raw prepared payload retirement independently verifies
the owning archive and its exact captured-order reference. The manifest stays.
A later explicit reprocessing can recover that window from the verified archive
and reports `recovered_from_archive`, rather than requiring an old native file.
Corrupt local payloads are refused, never overwritten using that recovery path.
Source line positions may reflect the first archived capture; ordering and
identity always follow pinned record IDs, not today's physical transcript lines.

## Available Capture Recovery

`capture-events status` inspects pending, terminal and completed capture work.
`retry --job ID --reason TEXT --apply` retries a repaired terminal input;
`--accept-config-change` clones it under the current profile while preserving
the original failed request. `archive-failure --job ID --reason TEXT --apply`
preserves an exact terminal version in Mongo, verifies it independently and
then retires only that unchanged local version. The deployment's configured
archive/keyring route is used; no credential value enters arguments or reports.

Both retry and archival default to dry-run. Archival is not successful capture
and does not mark the original memory work complete. Failures do not create a
required-read debt or prevent a native session from ending.

## Manual Credentials

`keyring put` and `keyring verify` are implemented with explicit apply and
secret-safe pipes/prompts. They are not an unattended keyring unlock solution.
See the [keyring guide](../src/anamnema/guides/keyring.md) and
[setup contract](SETUP.md) for current limits.

## Nightly Finishing

```sh
anamnema nightly --nightly-config FILE --config SETUP --archive-config ARCHIVE --memory-config MEMORY
```

The configured installed command runs entity linking, stale-card rebuilding,
pending naming rounds, unjudged lessons and held-briefing rebuilds. Stage failures
are recorded and independent later stages continue; a failed chain exits nonzero.
Failed child diagnostics are bounded and retained only in the private run file;
the returned/printed record reports `diagnostic_kept`, not the diagnostic text.
Replacements are delivered through the briefing-refresh prompt hook and retain
required-read debt. The installer supplies the user oneshot/timer, not a private
machine runner. This does not automatically plan new transcript windows:
`batch-window` remains an explicit operation over closed, prepared inputs.

## Acceptance Boundary

An installed batch must prove ordered end-to-end processing and safe replay
after interruption on disposable data. Nightly registration, native lifecycle
delivery, user/service/keyring policy and the clean-machine target require their
own acceptance. Disposable-guest acceptance has exercised those paths, including
the real-model finishing chain and subsequent source-backed correction/card and
briefing refresh. These contracts do not authorize changing a user's live service;
configuration and explicit apply remain required.
