# Setup And Installation

Anamnema installs on one Debian 13 (trixie) machine with native services, no
containers, for Claude Code. Two commands do the work:

- `anamnema setup` checks configuration and prerequisites and runs an
  interactive wizard. It changes nothing on the machine.
- `anamnema install` installs and configures everything from an installation
  profile, in nine ordered stages, and can resume after a failure.

The supported profile is a logged-in desktop user: the application, the capture
worker and the bundled llama.cpp encoder, notes and reranker run as that user's
systemd user services and use that user's unlocked keyring. MongoDB and Neo4j
run under their own package accounts. `install` refuses unattended operation
(`run_when_logged_out: true`), system-scope services and any account other than
the logged-in user.

## Requirements

- Debian 13, as the user who will run Anamnema (not root).
- `sudo` that works for that user without a password prompt: the installer runs `sudo -n`.
- A running user service manager (a normal desktop login provides it).
- That user's login keyring unlocked; see the [keyring guide](KEYRING.md).
- Claude Code installed and logged in as that user (its first run creates `~/.claude`).
- MongoDB and Neo4j not installed yet: `install` refuses a machine that already has either, and leaves it untouched.
- [uv](https://docs.astral.sh/uv/getting-started/installation/). Debian 13 ships Python 3.13; Anamnema needs 3.14, which uv fetches.

Installation does not create desktop accounts, unlock keyrings, log Claude in
or install GPU drivers.

## Worked Example: One Debian 13 Machine

One assistant instance named `main`, every service on the loopback address,
llama.cpp built from its pinned source and run on the CPU, Gemma 4 12B as the
notes model, credentials in the keyring. To use another instance name, replace
`main` in every file.

### 1. Prepare the account

```sh
sudo apt install git gnome-keyring libsecret-tools dbus-user-session
```

Log in to the desktop and unlock the login keyring (`secret-tool store` and
`secret-tool lookup` must work). Install Claude Code, run `claude` once and log in.

### 2. Install Anamnema

```sh
uv tool install --python 3.14 "anamnema[reranker] @ git+https://github.com/mattexb/Anamnema@v0.2.0"
anamnema setup --dry-run --backend cpu
```

The command is installed as `~/.local/bin/anamnema`.

### 3. Write the configuration

All files live in `~/.config/anamnema`. Most are the packaged defaults; the
deployment files tie them together:

````sh
PKG="$("$(uv tool dir)/anamnema/bin/python" -c 'import anamnema; print(anamnema.__path__[0])')/config"
C="$HOME/.config/anamnema"
D="$HOME/.local/share/anamnema"
S="$HOME/.local/state/anamnema"
mkdir -p "$C" "$D/worker/inputs" "$D/judge" "$D/models/reranker" "$S"

# Packaged defaults, unchanged
cp "$PKG/archive.json"          "$C/archive.json"
cp "$PKG/search_services.json"  "$C/search.json"
cp "$PKG/memory.json"           "$C/memory.json"
cp "$PKG/reranker.json"         "$C/onnx.json"
cp "$PKG/capture_queue.json"    "$C/capture-policy.json"

# Packaged defaults with one change each
PKG="$PKG" C="$C" python3 - <<'EOF'
import json, os
pkg, out = os.environ["PKG"], os.environ["C"]

def edit(source, target, change):
    with open(f"{pkg}/{source}") as f:
        data = json.load(f)
    change(data)
    with open(f"{out}/{target}", "w") as f:
        json.dump(data, f, indent=2)

edit("setup.json", "setup.json", lambda d: d.update(backend="cpu"))
edit("extraction.json", "extraction.json",
     lambda d: d.update(model_keyring_entry="notes/local", model_keyring_field="token"))
edit("memory_service.json", "memory-service.json",
     lambda d: d.update(access=[{"instance": "main", "entry": "memory/local", "field": "token"}]))
edit("memory_service.json", "reranker-service.json",
     lambda d: d.update(bind={"host": "127.0.0.1", "port": 18093},
                        access=[{"instance": "main", "entry": "reranker/local", "field": "token"}]))
EOF

# What the assistant reads at the start of every session
printf '%s\n' 'Write here who the assistant is and what it must know at the start of every session.' > "$C/identity.md"

cat > "$C/continuity.json" <<EOF
{
  "instance": "main",
  "state_root": "$S/continuity",
  "identity_files": ["$C/identity.md"],
  "registry": {"records_dir": "$S/installation/worker-records", "folder_roots": ["$D/worker/inputs"]}
}
EOF

cat > "$C/snapshot-worker.json" <<EOF
{
  "executable": "$HOME/.local/bin/claude",
  "cwd": "$D/worker",
  "input_root": "$D/worker/inputs",
  "registry": {"records_dir": "$S/installation/worker-records", "folder_roots": ["$D/worker/inputs"]}
}
EOF

cat > "$C/native-judge.json" <<EOF
{
  "executable": "$HOME/.local/bin/claude",
  "cwd": "$D/judge",
  "model": "sonnet",
  "registry": {"records_dir": "$S/installation/worker-records", "folder_roots": ["$D/worker/inputs"]}
}
EOF

cat > "$C/capture-deployment.json" <<EOF
{
  "instance": "main",
  "state_root": "$S/capture",
  "cwd": "$D/worker",
  "transcript_roots": ["$HOME/.claude/projects"],
  "config": "$C/setup.json",
  "archive_config": "$C/archive.json",
  "memory_config": "$C/memory.json",
  "search_config": "$C/search.json"
}
EOF

cat > "$C/recall-deployment.json" <<EOF
{
  "instance": "main",
  "cwd": "$D/worker",
  "config": "$C/setup.json",
  "archive_config": "$C/archive.json",
  "service_config": "$C/memory-service.json"
}
EOF

cat > "$C/refresh-deployment.json" <<EOF
{
  "instance": "main",
  "briefing_root": "$S/briefings",
  "stack_root": "$S/stacks",
  "state_directory": "$S/nightly",
  "continuity_config": "$C/continuity.json"
}
EOF

NOTES="$D/models/gemma-4-12b-it-qat-q4_0.gguf"
cat > "$C/nightly.json" <<EOF
{
  "instance": "main",
  "executable": "$HOME/.local/bin/anamnema",
  "state_directory": "$S/nightly",
  "common": ["--config", "$C/setup.json", "--archive-config", "$C/archive.json", "--memory-config", "$C/memory.json"],
  "extraction_config": "$C/extraction.json",
  "search_config": "$C/search.json",
  "model_file": "$NOTES",
  "card_model_file": "$NOTES",
  "lesson_model_file": "$NOTES",
  "link_limit": 50,
  "naming_rounds": 3,
  "max_cards": 20,
  "lesson_batch": 20,
  "stage_deadline_seconds": 1800.0,
  "max_output_bytes": 1048576,
  "history": 14,
  "briefings": {
    "briefing_root": "$S/briefings",
    "stack_root": "$S/stacks",
    "judge_config": "$C/native-judge.json",
    "max_rebuilds": 3
  }
}
EOF
````

`"model": "sonnet"` is passed to `claude --model` for the briefing judge. Then
the installation profile itself. The encoder and reranker files come from the
packaged catalogue; the Gemma file is pinned to its Hugging Face revision and
checksum:

````sh
ASSETS_JSON="$(CATALOGUE="$PKG/model_assets.json" python3 - <<'EOF'
import json, os
catalogue = json.load(open(os.environ["CATALOGUE"]))
models = os.environ["HOME"] + "/.local/share/anamnema/models"
print(json.dumps([
    {"directory": models, "resource": catalogue["encoder"]},
    {"directory": models + "/reranker", "resource": catalogue["reranker_model"]},
    {"directory": models + "/reranker", "resource": catalogue["reranker_tokenizer"]},
    {"directory": models + "/reranker", "resource": catalogue["reranker_specials"]},
    {"directory": models, "resource": {
        "filename": "gemma-4-12b-it-qat-q4_0.gguf",
        "url": "https://huggingface.co/google/gemma-4-12B-it-qat-q4_0-gguf/resolve/29d097773436b69ff9feafd636ab4cf873786537/gemma-4-12b-it-qat-q4_0.gguf",
        "sha256": "93567e57a8fe10b23569b9d9ec38cd005deedf71e29477c421a4b83f418a538b",
        "size_bytes": 6975879296,
        "license_url": "https://ai.google.dev/gemma/terms",
        "revision": "29d097773436b69ff9feafd636ab4cf873786537"}},
], indent=2))
EOF
)"

cat > "$C/installation.json" <<EOF
{
  "schema_version": 1,
  "instance": "main",
  "state_root": "$S/installation",
  "database_state": "$S/databases",
  "dpkg_query": "/usr/bin/dpkg-query",
  "claude_settings": "$HOME/.claude/settings.json",
  "native_commands": {
    "max_output_bytes": 1048576,
    "max_input_bytes": 1048576,
    "read_chunk_bytes": 65536,
    "timeout_seconds": 3600.0
  },
  "memory_file": "$C/memory.json",
  "memory_service_file": "$C/memory-service.json",
  "continuity_file": "$C/continuity.json",
  "snapshot_worker_file": "$C/snapshot-worker.json",
  "capture_deployment_file": "$C/capture-deployment.json",
  "capture_policy_file": "$C/capture-policy.json",
  "recall_deployment_file": "$C/recall-deployment.json",
  "refresh_deployment_file": "$C/refresh-deployment.json",
  "nightly_file": "$C/nightly.json",
  "runtime": {
    "instance": "main",
    "model_root": "$D/models",
    "extraction_file": "$C/extraction.json",
    "notes_identity_file": "$NOTES",
    "readiness_seconds": 300.0,
    "poll_seconds": 2.0,
    "runtime": {
      "executable": "$HOME/.local/bin/anamnema",
      "llama_server": "$D/llama-build/source/build/bin/llama-server",
      "cwd": "$D/worker",
      "setup_file": "$C/setup.json",
      "archive_file": "$C/archive.json",
      "search_file": "$C/search.json",
      "service_file": "$C/reranker-service.json",
      "onnx_file": "$C/onnx.json",
      "reranker_directory": "$D/models/reranker",
      "encoder": {
        "file": "$D/models/e5-small-v2-q8_0.gguf",
        "alias": "e5-small-v2-q8_0",
        "credential": {"entry": "encoder/local", "field": "token"},
        "context": 512, "batch": 512, "micro_batch": 512, "threads": 4
      },
      "notes": {
        "file": "$NOTES",
        "alias": "gemma-4-12b-it-qat-q4_0",
        "credential": {"entry": "notes/local", "field": "token"},
        "context": 8192, "batch": 2048, "micro_batch": 512, "threads": 4
      }
    },
    "build": {
      "root": "$D/llama-build",
      "shell": "/usr/bin/bash",
      "revision": "5aeac37a35b44fde4bdffaec9159f2fcb253b622",
      "jobs": 4,
      "timeout_seconds": 3600.0,
      "max_artifact_bytes": 1073741824,
      "max_artifacts": 64,
      "hash_block_bytes": 1048576,
      "max_receipt_bytes": 1048576
    },
    "manager": {
      "units_directory": "$HOME/.config/systemd/user",
      "state_directory": "$S/runtime-units",
      "systemctl": "/usr/bin/systemctl",
      "command_seconds": 60.0,
      "max_state_bytes": 262144,
      "max_units": 8
    },
    "downloads": {"chunk_bytes": 1048576, "idle_seconds": 60.0, "transfer_seconds": 3600.0},
    "assets": $ASSETS_JSON
  }
}
EOF
````

`threads` and `jobs` match a 4-core machine; raise them on a larger one.

### 4. Store the access tokens

Each local service checks a bearer token (a shared secret each request must
carry). Generate one per service into the keyring, through stdin, never as a
command argument:

```sh
for entry in encoder/local reranker/local memory/local notes/local; do
  python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_hex(32))' |
    anamnema keyring put --config "$C/setup.json" --archive-config "$C/archive.json" \
      --entry "$entry" --field token --stdin --confirm-replace --apply
done
```

A token is 33 to 126 characters without spaces or a trailing newline. The
installer creates the database credentials itself: `archive/mongodb:uri`,
`archive/mongodb-admin:password`, `graph/local:username` and
`graph/local:password`, once, reused on every replay.

### 5. Plan, then install

```sh
anamnema install --profile "$C/installation.json" --dry-run
```

The plan changes nothing. In its output, `databases.mongodb.state` and
`databases.neo4j.state` read `absent` (`foreign` means that database is already
installed, and apply refuses it); `runtimes.assets` lists the five downloads;
`application_units` holds `anamnema-memory.service` and
`anamnema-capture.service`; `hooks.events` lists `PreCompact`, `PreToolUse`,
`SessionEnd`, `SessionStart`, `Stop` and `UserPromptSubmit`; `nightly.units` holds the
nightly service and timer; `parent_death_cleanup_configured` is `true`.

```sh
anamnema install --profile "$C/installation.json" --apply --allow-downloads
```

The llama.cpp build and the 7 GB of downloads take most of the time. If a stage
fails, fix the cause and run the same command again with the files unchanged:
it resumes where it stopped. A changed file starts a new job.

### 6. Check it

```sh
systemctl --user is-active anamnema-encoder anamnema-notes anamnema-reranker anamnema-memory anamnema-capture anamnema-nightly.timer
anamnema install --profile "$C/installation.json" --apply
anamnema install --status JOB --state-root "$S/installation" --instance main
```

Running apply again on a finished installation replays it and checks live
health again; `--status` reads the journal of job `JOB` (the `job` field of the
apply report). Open a new Claude Code session: the hooks are in
`~/.claude/settings.json`, next to any you already had.

## Install Stages

`install --apply` runs nine stages in order and stops at the first failure:

| Stage | What it does |
| --- | --- |
| `platform` | Checks Debian 13, a non-root user and user-scope choices |
| `prerequisites` | `sudo -n apt-get install` of the prerequisite packages |
| `databases` | MongoDB and Neo4j packages, loopback configuration, application users, credentials into the keyring |
| `models` | Pinned llama.cpp source build, pinned downloads, encoder, notes and reranker services, readiness wait |
| `schema` | The graph schema |
| `services` | Memory and capture services, authenticated health check |
| `hooks` | Claude Code settings merge; foreign settings and hooks are kept |
| `schedule` | The nightly timer (03:30, up to 10 minutes random delay) |
| `verify` | Databases, models, services, health, hooks and timer |

`--through STAGE` stops after a stage. A failed stage keeps its journal and
receipts, and the same command resumes it. Downloads need `--allow-downloads`,
pinned addresses, revisions, sizes, checksums and licence links; existing files
are verified, never blindly replaced. Failures keep journals and installed
files; the installer never purges a database, and receipts must not be deleted
to force a retry.

### Installation profile reference

Unknown fields are rejected and values are strictly typed: seconds are written
with a decimal point. Every path is absolute, without `..`.

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `instance` | Assistant name. The same in `runtime.instance`, the continuity, capture, recall, refresh and nightly files and the service `access` rows. |
| `state_root` | Installer state: jobs, hooks, units, worker records. |
| `database_state` | Database receipts. |
| `native_commands` | Bounds for the installer's own commands: `max_output_bytes`, `max_input_bytes`, `read_chunk_bytes`, `timeout_seconds` (optional `probe_seconds`, default 1.0). Every command deadline, including the build's and the 900 s database commands, fits inside `timeout_seconds`. |
| `dpkg_query` | Path of `dpkg-query`. |
| `prerequisite_packages` | Optional. Default: `ca-certificates curl gpg build-essential cmake git pkg-config gnome-keyring libsecret-tools dbus-user-session`. |
| `memory_file`, `memory_service_file`, `continuity_file`, `snapshot_worker_file`, `capture_deployment_file`, `capture_policy_file`, `recall_deployment_file`, `refresh_deployment_file`, `nightly_file` | The companion files below. |
| `claude_settings` | Claude Code `settings.json` that receives the hooks; its folder must exist. |
| `database_policy_file`, `worker_policy_file`, `nightly_policy_file`, `hook_policy_file` | Optional replacements of the packaged `debian_databases.json`, `worker_unit.json`, `nightly_timer.json`, `claude_hooks.json`. |
| `max_profile_bytes` | Optional read limit, default and maximum 1048576. |

`runtime`:

| Field | Meaning |
|---|---|
| `instance`, `model_root` | Assistant name; the folder that holds every downloaded file. |
| `runtime.executable` | The installed `anamnema` command, used by services, hooks and the timer. |
| `runtime.llama_server` | `<build.root>/source/build/bin/llama-server` when `build` is set; otherwise your own llama.cpp server. |
| `runtime.cwd` | Working folder of services and hooks; the same as the capture and recall `cwd`. |
| `runtime.setup_file`, `archive_file`, `search_file` | Setup, archive and search configuration shared by all services. |
| `runtime.service_file` | Reranker service: `bind` equal to the setup reranker endpoint, one `access` row for the instance with the search file's `reranker_keyring_entry` and `token_field`. |
| `runtime.onnx_file` | ONNX reranker settings. |
| `runtime.reranker_directory` | Folder of `model.onnx`, `tokenizer.json`, `special_tokens_map.json`. |
| `runtime.encoder`, `runtime.notes` | `file`, `alias`, `credential` (`entry`, `field`), `context`, `batch`, `micro_batch`, `threads`, with `micro_batch <= batch <= context`. The encoder alias equals `encoder_model` in the search file. |
| `runtime.notes_tunnel` | Instead of `notes`: the notes model on another machine, reached through an SSH loopback tunnel (`host`, `user`, `port`, `model_port`, `private_key`, `public_host_key`, `expected_host_key_sha256`, `known_hosts_file`, `ssh`, `alive_seconds`, `missed_keepalives`). `notes_identity_file` is then the operator's attestation JSON and `extraction.json` sets `model_identity: "operator-attested-remote"`. |
| `assets` | Pinned downloads, `{directory, resource}`; each destination inside `model_root`, all distinct. Resource fields: `filename`, `url`, `sha256`, `size_bytes`, `license_url`, `revision`; both addresses `https`, without credentials or query. |
| `build` | Pinned llama.cpp source build: `root`, `shell` (bash), optional `git` and `cmake` paths, `revision` (40 hex characters), `jobs`, `timeout_seconds`, `max_artifact_bytes`, `max_artifacts`, `hash_block_bytes`, `max_receipt_bytes`. If the build stops with "exceeds configured bound", raise the artifact limits. |
| `manager` | systemd user manager: `units_directory`, `state_directory` (separate, not nested), `systemctl`, `command_seconds`, `max_state_bytes`, `max_units`. |
| `downloads` | `chunk_bytes`, `idle_seconds`, `transfer_seconds`. |
| `extraction_file` | Extraction settings (`extraction.json`). |
| `notes_identity_file` | The notes model file, or the attestation JSON for a remote model. |
| `readiness_seconds`, `poll_seconds` | How long to wait for the model services, and how often to check. |

### Companion files

| File | Content |
|---|---|
| `setup.json` | Packaged `setup.json` with `backend` set. Loopback endpoints with six distinct ports: archive 27017, graph 7687, encoder 18090, reranker 18093, notes 18083, memory service 8095. |
| `archive.json`, `search.json`, `memory.json`, `onnx.json`, `capture-policy.json` | Packaged defaults, unchanged. |
| `extraction.json` | Packaged `extraction.json` with `model_keyring_entry` and `model_keyring_field` equal to the notes credential. |
| `memory-service.json` | Packaged `memory_service.json` with exactly one `access` row for the instance. |
| `reranker-service.json` | The same, with `bind` equal to the reranker endpoint. |
| `continuity.json` | `instance`, `state_root`, `identity_files` (text files the assistant reads at every session start), `registry`. |
| `snapshot-worker.json` | `executable` (Claude), `cwd`, `input_root`, optional `model`, `registry`. |
| `native-judge.json` | `executable` (Claude), `cwd`, `model`, `registry`. |
| `registry` | `records_dir` (`<state_root>/worker-records`) and `folder_roots` (exactly the snapshot `input_root`); optional `max_record_bytes`, `max_records_scanned`, `lock_wait_seconds`. Identical in the continuity, snapshot-worker and judge files. With it, a Claude worker killed together with its session is cleaned up at the next session start. |
| `capture-deployment.json` | `instance`, `state_root`, `cwd`, `transcript_roots` (Claude's `projects` folder), `config`, `archive_config`, `memory_config`, `search_config`; optional `speech_config`, `capture_config`. |
| `recall-deployment.json` | `instance`, `cwd`, `config`, `archive_config`, `service_config`. |
| `refresh-deployment.json` | `instance`, `briefing_root`, `stack_root`, `state_directory`, `continuity_config`; optional `max_replacements_bytes`. |
| `nightly.json` | `instance`, `executable`, `state_directory`, `common`, `extraction_config`, `search_config`, `model_file`, `card_model_file`, `lesson_model_file`, `link_limit`, `naming_rounds`, `max_cards`, `lesson_batch`, `stage_deadline_seconds`, `max_output_bytes`, `history`; optional `lesson_kinds`, `diagnostic_tail_bytes`, `briefings` (`briefing_root`, `stack_root`, `judge_config`, `max_rebuilds`). |

### Cross-file rules

The installer refuses a profile that breaks one of these, before it changes anything:

- One `instance` everywhere.
- Capture and recall `config` equal `runtime.setup_file` and their `archive_config` equals `runtime.archive_file`; capture `memory_config` equals `memory_file` and `search_config` equals `runtime.search_file`; recall `service_config` equals `memory_service_file`; both `cwd` equal `runtime.cwd`.
- Refresh `continuity_config` equals `continuity_file`.
- Nightly `executable` equals `runtime.executable`; its three model files equal `notes_identity_file`; `common` is exactly `--config <setup> --archive-config <archive> --memory-config <memory>`; `extraction_config` and `search_config` equal the runtime ones.
- Six distinct ports.
- A local notes model: `notes_identity_file` equals `notes.file`, and `notes.credential` equals the extraction keyring selector.
- The registry rules above.

## The `setup` Command

```sh
anamnema setup --dry-run
anamnema setup --dry-run --backend cpu
anamnema setup --config /absolute/path/setup.json
anamnema setup --wizard
anamnema setup --wizard --save "$HOME/anamnema-setup.json"
anamnema setup --wizard --config "$HOME/anamnema-setup.json"
anamnema setup --keyring-guide
```

`setup` without a mode is a dry run. Its output is JSON. Exit 0 means the plan
was produced and the prerequisite observations passed; exit 2 means a missing or
unsupported prerequisite, invalid configuration or an unavailable operation.
`setup` only plans: `ready_to_install` is always `false`, every entry of
`steps` reads `run-by-install`, and the blocker names the command that installs:
`anamnema install --profile FILE --apply`. `setup --apply` is refused and points
to the same command. `process
--apply` accepts the `vulkan` and `cpu` backends; `cuda` reports unsupported.

### Wizard

The wizard asks whether work should continue while logged out, user or system
services, which accounts run the components, manual or guided keyring setup,
where the notes model runs and optional folder changes. It writes the setup
answers (`setup.json`) only, never `installation.json` or its companion files:
those come from the worked example above. It asks for no passwords and never
runs a discovered program.

Prompts and the proposed configuration go to stderr; stdout carries one JSON
report. EOF or Ctrl-C cancels with exit 130. Nothing is saved unless `--save`
names a file and you answer yes after reviewing the choices. The answer file
holds choices only, never secrets; it is written owner-only and atomically and
never replaces an existing file, directory or link. Its folder must exist.
Resume with `--config`: the saved values become the defaults. To save a
revision, use a different filename.

`--wizard`, `--keyring-guide`, `--dry-run` and `--apply` are mutually exclusive;
`--save` without `--wizard` is refused, so a dry run never writes answers.

## Configuration

Packaged defaults live in `src/anamnema/config/`. `--config` reads a complete
replacement, never a hidden merge. Unknown fields, wrong types, non-loopback
service hosts, invalid or duplicate ports, relative paths and `..` fail
validation, and error output omits the supplied values.

The `paths` in `setup.json` are declarations only and are not created; the
installer uses the absolute paths of `installation.json`. `setup.json` holds no
credentials. The archive importer reads its address from the keyring (default
`service=anamnema entry=archive/mongodb field=uri`).

Finding `secret-tool` on PATH does not prove an unlocked keyring, and finding
`vulkaninfo` does not prove a suitable GPU. The prerequisite check reads OS
metadata and searches PATH; it runs no discovered command, contacts no service,
reads no secret and changes no Claude setting or data folder.

`anamnema keyring put` and `verify` default to a dry run. With `--apply` they
use the configured namespace and take the secret from a hidden prompt or from
`--stdin`, never from an argument. See the [keyring guide](../src/anamnema/guides/keyring.md)
for replacement consent and read-back verification.

## For Contributors

The commands for the development gate are in [CONTRIBUTING.md](../CONTRIBUTING.md).
The sections below describe what the tests of each component prove.

### Claude Settings Merge Contract

The installer-side `merge_hooks` helper is pure: it proposes a document without
reading or writing native settings. New matcher groups are appended, preserving
foreign settings and hook groups. An upgrade removes only exact groups named in
the prior installation's ownership receipt. Missing, edited or duplicated owned
groups refuse reconciliation; a matching foreign group is not silently adopted.
Disabled hooks stay disabled. Hook commands use shell-quoted argv and explicit
timeouts, with native input still delivered on stdin. The receipt is separate
from Claude settings, not an invented native settings key.

The native shape follows the [Claude hook reference](https://code.claude.com/docs/en/hooks).
Registration, watcher supervision and the native read, capture and compaction
events were exercised on a clean Debian 13 machine with the installed package.

### Worker Supervision Template

`render_worker_unit` generates an explicit user- or system-scope systemd service
without installing or starting it. A system unit requires a supplied non-root
account; a user unit uses its user manager. The template restarts bounded
foreground workers, limits restart storms, sends their output to the journal,
sets an owner-only umask and prevents privilege elevation. Exec arguments use
systemd quoting, disabled environment substitution and escaped `%` specifiers;
`WorkingDirectory` uses that directive's different path grammar.

The template uses [control-group termination](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)
so descendants belong to the service's cleanup boundary; parser tests on systemd
257 validate the generated syntax. A process started outside a unit has no such
supervision: Claude workers started by the installed hooks are covered by the
worker registry instead (see [CONTINUITY.md](CONTINUITY.md)).

### Owned User-Service Installation

`UserServiceInstaller` applies explicitly supplied user units with the same
renderer. Its pure `plan` does not inspect or change the host. Apply refuses root,
foreign target files, edited/missing owned files, unsafe directories and a receipt
from a different account or unit directory. It never adopts a matching foreign
file. Database package units are outside this helper's ownership.

A private receipt is committed before unit files change. An interrupted file
installation must finish its prepared plan or explicitly `rollback()` before
an upgrade. Command failures keep installed files, previous working bodies and
the receipt for retry or rollback. Activation reloads the
user manager, enables and restarts only the named units, then checks they are
active. A successful unchanged replay checks activity without restarting workers.
An upgrade or failed activation retry can restart them, so workers must retain
their own durable replay guarantees. Manager output is withheld and each command
has a deadline; a refusal is visible, never reported as a successful install.

`rollback()` restores the previous active bodies (or removes a first partial
installation). `remove()` stops/disables and compare-deletes only owned units,
not application data or foreign files. Both journal their intent before changing
files and can resume interruption. An inactive unchanged unit is reported with
the explicit `systemctl --user status`/restart recovery step, not silently healed.

This is the unit-installation step `install` uses. On a clean Debian 13
machine, under its real user manager, killing a worker restarted it and removed
its old child, an unchanged apply did not restart it, and stopping the unit
removed both remaining processes.

Archive integration tests also require an available `mongod` executable. They
start their own process on an ephemeral loopback port with a fresh temporary
database directory, verify that directory before writing, and stop that process
afterwards. They never use an existing database or real keyring. A missing
executable fails the gate rather than silently skipping those tests. This
`mongod` is the contributor's, not the one the installer sets up.

Build and test the wheel in an isolated environment outside the checkout; inspect
wheel and source-archive contents for packaged defaults, entry point, licence and
the absence of internal plans or private code. Test invalid input, the refused
`setup --apply`, wrong OS or Python, missing executables, backend selection and
unchanged user files. Processing integration tests start a synthetic loopback
llama.cpp fixture; archive tests run a real disposable MongoDB and a fixture-only
keyring executable, including the installed command.

Memory integration additionally requires Docker with permission to launch a
fixture-owned container. It uses Neo4j Community 2026.10 pinned by image digest
in `tests/graph_fixtures.py`, publishes only an ephemeral loopback Bolt port,
uses no host bind mounts and removes the container on teardown. Missing
Docker/Neo4j is a failed gate, not a skipped test. The reranker extra is required
for the full development gate: a generated synthetic ONNX model exercises actual
CPU inference without downloading model weights. See [MEMORY.md](MEMORY.md).
