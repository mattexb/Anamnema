# Operations Contract

The installed `anamnema` command owns import, processing, storage, review,
correction and recovery. `batch-window` owns archive/extract/store; `nightly`
owns the separate finishing chain. A single successful stage does not mean the
whole memory batch is finished.

## Batch Recovery

The batch driver uses an explicit assistant, session, closed input and
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

Worker execution has bounded input and output and a whole-invocation deadline.
Descendant cleanup belongs to the systemd service's control group; for Claude
workers, the worker registry removes leftovers at the next session start (see
[CONTINUITY.md](CONTINUITY.md)). Read-only job reports and nonzero failure exits
agree. The driver is local: there is no SSH runner, job endpoint or distributed
queue.

The implemented `BatchJournal` persists this ordering and retained
numeric attempt counters. It requires an immutable prepared input and verifies
that input before starting a stage and accepting its success. It must not be
pointed at a moving native transcript tail. A per-job OS worker lock spans
execution; separate state locks cover only atomic transitions. A stale worker
handle cannot advance state. Attempt/metric/state/input limits are configured.
`PreparedInputs`, `ArchiveWindow`, `WindowStages` and `BatchRunner` connect
the actual archive, extraction and storage engines. The `batch-window` command
can prepare closed windows, run/resume those three stages and inspect their
journals without a live model or input file. Finishing stages and the complete
nightly command are separate from that window journal. A completed window job explicitly reports
`nightly_complete: false`.

### Prepared Window Inputs

Batch preparation pins one already closed conversation window per job,
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
Tests exercise this with real disposable stores and HTTP fixture models. On a
clean Debian 13 machine, a real notes model reached through an authenticated
loopback tunnel produced six notes, passed storage and replay, the finishing run,
a source-backed correction and the card and briefing refresh.

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

`capture-events status --deployment FILE` inspects pending, terminal and
completed capture work; `FILE` is the installed `capture-deployment.json`.
`capture-events retry --deployment FILE --job ID --reason TEXT --apply` retries a
repaired terminal input; `--accept-config-change` clones it under the current
profile while preserving the original failed request.
`capture-events archive-failure --deployment FILE --job ID --reason TEXT --apply`
preserves an exact terminal version in Mongo, verifies it independently and
then retires only that unchanged local version. The deployment's configured
archive/keyring route is used; no credential value enters arguments or reports.

Both retry and archival default to dry-run. Archival is not successful capture
and does not mark the original memory work complete. Failures do not create a
required-read debt or prevent a native session from ending.

## Manual Credentials

`keyring put` and `keyring verify` default to a dry run; with `--apply` the
secret comes from a hidden prompt or `--stdin`. They store and check entries;
they do not unlock a keyring. See the [keyring guide](../src/anamnema/guides/keyring.md).

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
required-read debt. The installer sets up the timer `anamnema-nightly.timer`:
every day at 03:30 with up to 10 minutes of random delay, catching up after the
machine was off (`nightly_timer.json`). Check it with
`systemctl --user list-timers anamnema-nightly.timer`. Nightly does not automatically plan new transcript windows:
`batch-window` remains an explicit operation over closed, prepared inputs.

## Verification

`anamnema install --apply` ends with its `verify` stage: databases, models,
services, authenticated health, hooks and timer. On a clean Debian 13 machine the
installed system passed ordered processing, replay after interruption, nightly
registration, the real-model finishing run, a source-backed correction and the
card and briefing refresh. Nothing here changes a live service without a
configuration file and an explicit `--apply`.
