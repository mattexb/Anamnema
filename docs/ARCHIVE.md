# Conversation Archive

The adapter reads Claude Code's JSONL transcripts; the reusable
`SubstrateAdapter` base owns the bounded file-reading and provenance loop. An
adapter for another assistant extends classification, not MongoDB writes or memory
processing.

Archive records are numbered JSONL lines with conversation and summary
classification, content-derived identity, set-on-insert writes, bounded
independent read-back and lossless compressed source recovery. Sources are only
read, never changed.

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
Native conversation records, summaries and tool blocks remain intact;
[extraction](PROCESSING.md) separates dialogue from tools and instructions and
never treats a tool as speech.
Bookkeeping types excluded by the reviewed Claude policy are counted, not hidden.

`--closed` is your explicit statement that the session has ended; it does not
close or inspect the native Claude process. It adds a separate closing marker
after the final retained conversation, including that last record. A final
summary already closes the stretch, so it gets no redundant marker. Without
the flag there is no inferred closure from merely reaching the end of a file.

## Local Database And Keyring

The default database is `anamnema`. Connections accept one direct localhost
server, no SRV lookup or proxy, and no URI database different from archive policy.
The URI's port must match the setup's local archive endpoint. Authentication
options travel in the URI; the one `anamnema install` creates authenticates as
user `anamnema` on 127.0.0.1 with the configured port and database.

The keyring lookup uses three attributes: `service=anamnema`,
`entry=archive/mongodb`, `field=uri`. The namespace, selectors, executable, bus
fallback and timeout are configured; no credential file is read.

`anamnema install` stores that URI itself. To import into a MongoDB you set up
yourself, make the entry by hand with the configured attributes (defaults shown):

```sh
secret-tool store --label='Anamnema archive' service anamnema entry archive/mongodb field uri
```

Enter the URI at the password prompt, never in shell arguments, setup JSON or a
bug report. See the [keyring guide](KEYRING.md). The test suite uses a fake keyring
executable, never real credentials.

## Records, Replay And Proof

Each record has a content-derived `_id`, instance, substrate, session, kind,
timestamp, payload digest, first physical source line and original `source`
object. Oversized/non-BSON-native JSON uses bounded `source_lzma` instead, without
truncation or filesystem spill. Native fields stay inside the source object so
metadata cannot overwrite them.

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
secret-safe keyring and driver errors, and installed command import through the
fixture. The installed archive, keyring and native Claude Code path were also
exercised end to end on a clean Debian 13 machine ([setup](SETUP.md)).

Driver behavior references: [PyMongo upserts](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/update/)
and [MongoDB unique document identities](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/insert/).
