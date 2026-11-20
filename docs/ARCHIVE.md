# Conversation Archive

The first adapter reads Claude JSONL; the reusable `SubstrateAdapter` base owns
the bounded file-reading/provenance loop. A substrate extends classification,
not Mongo writes or memory processing. Native event translation arrives in M5;
no other assistant integration is claimed by this module.

The method is adapted from the existing transcript importer: numbered JSONL,
conversation/summary classification, content-derived identity, set-on-insert,
bounded independent read-back and lossless compressed source recovery. The
physical-line fix from source revision `90db143` is included. Sources are read
only; no private package, credentials file, trim or machine runner is required.

## Commands

```sh
anamnema import --file /path/session.jsonl --instance assistant-name --session native-session
anamnema import --file /path/session.jsonl --instance assistant-name --session native-session --apply
```

The default is a dry-run: parse, validate and report counts/digest, without
opening the keyring or MongoDB. `--apply` explicitly requests archive writes.
`--config` selects the nonsecret setup/keyring configuration; `--archive-config`
selects a complete nonsecret archive policy instead of the packaged defaults.
Exit 0 means the selected operation completed; exit 2 means a visible failure.
Dry-run reports `verified: false`; it is not evidence of a saved copy.

The input must be a regular nonsymlink UTF-8 file, within the configured byte
limit, unchanged during reading. Blank lines retain their physical positions.
Malformed records and missing timestamps fail before any archive connection.
Native conversation records, summaries and tool blocks remain intact; extraction
will distinguish dialogue from tools/instructions later, not treat tools as speech.
Bookkeeping types excluded by the reviewed Claude policy are counted, not hidden.

`--closed` is your explicit statement that the session has ended; it does not
close or inspect the native Claude process. It adds a separate closing marker
after the final retained conversation, including that last record. A final
summary already closes the stretch, so it gets no redundant marker. Without
the flag there is no inferred closure from merely reaching the end of a file.

## Local Database And Keyring

The default database is `anamnema`. Connections accept one direct localhost
server, no SRV lookup or proxy, and no URI database different from archive policy.
The URI's port must match the setup's local archive endpoint. TLS/authentication
options are carried by the explicit URI; deployment/security acceptance is later.

The neutralized keyring lookup uses the existing three-attribute method:
`service=anamnema`, `entry=archive/mongodb`, `field=uri`. The namespace, selectors,
executable, bus fallback and timeout are configured. No private keyring entry or
credential file is read. Do not use the private namespace as a shortcut.

After preparing your own account's keyring, a manual terminal entry can be made
with the configured attributes (defaults shown):

```sh
secret-tool store --label='Anamnema archive' service anamnema entry archive/mongodb field uri
```

Enter the URI at the password prompt, never in shell arguments, setup JSON or a
bug report. See the [keyring guide](KEYRING.md). The test suite uses a fake keyring
executable, not real credentials; actual unlock/headless access remains untested.

## Records, Replay And Proof

Each record has a content-derived `_id`, instance, substrate, session, kind,
timestamp, payload digest, first physical source line and original `source`
object. Oversized/non-BSON-native JSON uses bounded `source_lzma` instead, without
truncation or filesystem spill. Native fields stay inside the source object so
metadata cannot overwrite them. This public envelope intentionally differs from
the original archive's top-level enrichment and is recorded in the tracker;
it is not an in-place migration of a private database.

Native UUID is retained inside the original source, but is not the unique key:
valid branches can reuse it, and multiple rows can share a timestamp. Identity
includes the instance/session/type/time/content, not the machine's absolute path
or current line number. Identical copies moved to another path still deduplicate.
The first occurrence's line stays unchanged on replay from a differently spaced
copy. Distinct content is a distinct source variant, not a destructive update.

Writes use `$setOnInsert`, never replacement, deletion or clearing collections.
Read-back recomputes source content and compares ownership/identity, including
newly inserted physical line numbers. It does not trust saved checksum fields
alone or equate booleans and integers. Only after all batches verify is a
deterministic completion receipt written and itself read back.

An interrupted run may leave a partial set of records. It does not earn a
completed receipt; rerunning the same source verifies existing rows and completes
the rest without duplicates. Keep original transcripts: no source/pending data
is deleted by this module. A corrupt existing row is reported, not overwritten.
The importer does not repair a graph, generate notes or register nightly jobs.

## Verification

The tests start a real disposable `mongod`, with a fresh directory and loopback
port, and verify its configured directory before any writes. They never reuse
a supplied URI or an existing service. Missing `mongod` fails the gate rather
than skipping integration tests. The fixture shuts down only its own process.

Tests cover blank-line provenance, native UUID/time collisions, source content,
closure, replay, cross-instance isolation, interrupted batches, corrupt content
with an unchanged saved digest, ownership changes, compression, typed scalars,
secret-safe keyring/driver errors and installed CLI import through the fixture.
These are synthetic/disposable-store results, not native Claude lifecycle or
production-keyring acceptance. Installer and complete memory-loop gates remain.

Driver behavior references: [PyMongo upserts](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/update/)
and [MongoDB unique document identities](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/insert/).
