# Setup And Native Installation

`setup` provides configuration validation, a wizard and read-only prerequisite discovery.
The separate `install` command composes native components as described below.
The first reviewed target is Debian 13 trixie,
native services, one machine, Claude first. No containers are selected.

The approved first acceptance uses a small local CPU llama.cpp model inside a
disposable Debian 13 VM, not an external API. `--backend cpu` uses the same local
identity/template/tokenizer transport as Vulkan without requiring `vulkaninfo`.
Native database, keyring, CPU generation, embedding, reranking and combined
installer/replay checks have passed in a disposable guest. Native Claude
identity, Stop capture, compaction and briefing read acknowledgements were
also exercised. A real-model full path and guarded parent-death cleanup have
passed separately; final integration gates and publication approval remain.
These are compatibility checks, not model-quality or performance claims. The first account
arrangement is a logged-in user with an unlocked Secret
Service: app, worker and bundled model runtimes use that user's units; MongoDB
and Neo4j retain their package accounts. Unattended/no-login is not first-release
scope.

## Commands

Create the local development environment with `uv sync --locked --python 3.14`.
This installs only the Python project/dev dependencies into this checkout's
environment; it does not install the memory services.

```sh
uv run --no-sync anamnema setup --dry-run
uv run --no-sync anamnema setup --dry-run --backend api
uv run --no-sync anamnema setup --config /absolute/path/setup.json
uv run --no-sync anamnema setup --wizard
uv run --no-sync anamnema setup --keyring-guide
uv run --no-sync anamnema setup --wizard --save "$HOME/anamnema-setup.json"
uv run --no-sync anamnema setup --wizard --config "$HOME/anamnema-setup.json"
```

`setup` without a mode is also a dry run. `--apply` always fails before any
configuration loading or inspection: execution belongs to `install`, not `setup`.
CUDA is a reserved selection and reports unsupported. API selection plans a
future reviewed API route; it is not proof of generic API compatibility.

Output is JSON. Exit 0 means the plan was produced and the limited prerequisite
observations passed, not that Anamnema is installed. Exit 2 means a missing or
unsupported prerequisite, invalid configuration or unavailable operation.
Always inspect `ready_to_install`, currently false, plus `blockers` and
`not_checked`. Every install step is explicitly `not-implemented`.

## Wizard And Manual Path

The wizard asks whether work should continue while logged out, whether you want
user or system services, which accounts should run the components, manual versus
guided keyring setup, notes-model location and optional folder changes. User
scope describes the application service, not every daemon on the machine.
Databases retain their package accounts. In the first profile, application and
model workers share the logged-in user's account and keyring; other account
arrangements remain planning choices rather than tested service capabilities.
These are requested arrangements, not verified service capabilities.

The first native installation profile is a logged-in desktop
user and that user's unlocked Secret Service, with native daemon accounts for
the databases. Unattended/no-login operation is outside the first release.

Follow the [manual keyring guide](KEYRING.md) if you prefer to prepare it yourself.
The guided choice records the desired arrangement; actual account creation,
keyring configuration/unlock remain operator responsibilities. `install`
activates the explicitly configured native services.
The wizard asks for no passwords and never invokes a discovered executable.

Prompts and the complete proposed configuration go to stderr; stdout retains
one JSON report. EOF or Ctrl-C cancels with exit 130. Nothing is saved unless
`--save` names a file and you explicitly answer yes after reviewing the choices.
Without `--save`, or if you decline confirmation, no answer file is created.

The answer file contains only configuration/choices, never secret entries. It
is written with owner-only permissions and published atomically without replacing
an existing file, directory or symlink. Its parent must already exist. Resume
with `--config`: saved values become the wizard defaults. To save a revision,
use a different filename; the old configuration remains intact. Cancelling a
new attempt never changes the previous file. This is not automatic saving after
each question. Do not add passwords to account names, paths or other answers.

`--wizard`, `--keyring-guide`, `--dry-run` and `--apply` are mutually exclusive. `--save` without
`--wizard` is refused, so an ordinary dry-run cannot accidentally write answers.

## Explicit Native Install

Install the package with its `reranker` extra. Run as the intended non-root
Debian 13 desktop user, with an active user systemd manager, sudo authorization
for package preparation, and that user's unlocked Secret Service. MongoDB and
Neo4j use package accounts; application, capture and model workers use user
units. Installation does not create desktop accounts, unlock keyrings, log
Claude in, or configure GPU drivers.

`InstallProfile` in `anamnema.install_profile` defines the deployment schema.
It names complete, absolute JSON paths for setup, archive, memory, execution,
search, runtime, service, continuity, snapshot, capture, recall, briefing refresh
and nightly configuration. These must agree on instance, stores and model
identities. The snapshot worker names an existing Claude executable and working
directory. Its settings parent directory must already exist; Claude login or
explicit operator preparation creates it. No home configuration is inferred or
copied. The setup wizard does
not yet emit a complete installation profile.

Prepare the selected model/service bearer entries through `anamnema keyring`
and the [keyring guide](KEYRING.md), using a hidden prompt or stdin, never argv
or configuration files. Database bootstrap creates fresh credentials once and
retains them for replay; it refuses to regenerate after the secrets step.
Runtime credentials must already exist; lookup failure stops installation.

For the guarded profile, put the same optional `registry` object in continuity,
snapshot-worker and native-judge configuration. Its `records_dir` must be the
installation `state_root` plus `worker-records`; its `folder_roots` must contain
only the snapshot worker's `input_root`. The installer refuses disagreement,
including a differently configured nightly briefing judge. The judge working
directory is never a cleanup target. Records and inputs are private to the
logged-in user. Older configurations without this object still parse, but report
`parent_death_cleanup_configured: false` and do not provide that protection.

```sh
anamnema install --profile /absolute/path/installation.json --dry-run
anamnema install --profile /absolute/path/installation.json --apply --allow-downloads
anamnema install --profile /absolute/path/installation.json --apply --through services
anamnema install --status JOB --state-root /absolute/path/install-state --instance NAME
```

Planning reads explicit configuration and package/ownership state, but does not
install packages, read credentials, create receipts or change services/settings.
Apply runs platform, prerequisites, databases, models, schema, services, hooks,
schedule and verification. Hooks are registered after the capture watcher is
active. Nightly uses the installed store, execution/search policy and model
identities.

The `services` checkpoint precedes Claude login and never reports completion.
On new hardware, repeat the source-tree foreground-hook latency check
(`tests/test_capture_hook_latency.py`) before relying on the default large-file
timeout. The installer does not infer hardware speed or silently increase hook
limits; the tested guest result is recorded in CONTINUITY.
Partial and full plans have different journal identities; the full run reuses
component receipts. Keep deployment files unchanged when resuming. Changed
inputs require a new job, not editing a receipt. Completed replay checks live
health again; offline status reports history only. Neither claims native Claude
acceptance or release readiness.

Weights are not bundled. Downloads require explicit consent and pinned URLs,
revisions, lengths, checksums and licensing provenance. Existing files are
verified, not blindly replaced. An optional pinned llama.cpp CPU build uses
configured Git/CMake/Bash executables; an existing operator-provided binary can
instead be named. Remote notes models use the separate operator-attested
identity and authenticated loopback transport, not local-byte verification.

Failures retain journals and owned files. The component user-service helper
supports compare-and-restore rollback and explicit removal; the top-level
installer does not purge databases. Never delete receipts to force adoption.

The disposable-guest acceptance exercised the guarded profile with a real Claude
worker: killing its parent killed the worker, and the next native SessionStart
removed the leftover input folder and recovery record. That resumed session
also read and acknowledged its updated briefing. See [CONTINUITY.md](CONTINUITY.md)
for process-identity checks and remaining supervision limits.

## Configuration

Packaged defaults live in `src/anamnema/config/setup.json`. `--config` reads a
complete replacement, not a hidden merge with private defaults. No environment
or source-project configuration is implicitly loaded. Unknown fields, wrong
types, non-loopback service hosts, invalid/duplicate ports, relative paths and
parent traversal fail validation. Error output omits supplied values.

The configuration records Debian/native target, Python minimum, model-location
switch, prospective folders, separate local services, keyring namespace and
optional operator choices. Older setup documents without choices remain readable;
their choices start undecided, not implicitly configured.
Paths are declarations only and are not created. Credentials are not accepted
here. The archive importer now uses a neutralized keyring lookup, never copied
entries; setup itself still does not read or create credentials.

The default ports and folders are configurable, not tested installation results.
Finding `secret-tool` on PATH does not prove a usable/unlocked keyring. Finding
`vulkaninfo` does not prove a suitable GPU, driver, memory capacity or model speed.
Preflight reads OS metadata and searches PATH; it executes no discovered command,
contacts no service, reads no secret and changes no Claude setting or data folder.

Manual `anamnema keyring put`/`verify` are implemented separately; both default
to a no-secret dry-run. Apply uses the configured namespace, exact non-terminal
Secret Service values and hidden input or explicit stdin, never a value argument.
See the [keyring guide](../src/anamnema/guides/keyring.md) for replacement consent,
read-back verification and the still-unaccepted native/unattended boundary.

## Ordered Install Contract

The explicit apply path prepares prerequisites, requires the unlocked keyring,
installs MongoDB and Neo4j, activates model/application services, merges Claude
hooks, registers nightly work and verifies the installed components. Dependent
steps stop on failure; resume/replay must not destroy state or double-register.

The first profile's user/keyring access, native database packages, pinned model
acquisition, ownership preservation and component replay were exercised in the
disposable guest. This does not automate desktop login or keyring unlock.
The wizard collects intentions, not verified service access. Do not turn PATH discovery
into a false database/keyring health check or report an incomplete stack ready.

## Development Gates

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
These pure merge tests do not prove registration, watcher supervision or native
lifecycle delivery. The separate installed guest acceptance exercised registration,
watcher supervision and native read/capture/compaction events; it did not change
the operator's host settings.

### Worker Supervision Template

`render_worker_unit` generates an explicit user- or system-scope systemd service
without installing or starting it. A system unit requires a supplied non-root
account; a user unit uses its user manager. This does not establish unattended
keyring access, which is outside the first profile. The template restarts bounded
foreground workers, limits restart storms, sends their output to the journal,
sets an owner-only umask and prevents privilege elevation. Exec arguments use
systemd quoting, disabled environment substitution and escaped `%` specifiers;
`WorkingDirectory` uses that directive's different path grammar.

The template uses [control-group termination](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)
so descendants belong to the service's cleanup boundary. Offline parser tests
on systemd 257 validate the generated syntax, not live shutdown behavior or
Secret Service access. A process launched outside this unit gains no such
supervision: the earlier standalone snapshot-worker parent-kill finding remains
open for native lifecycle acceptance.

### Owned User-Service Installation

`UserServiceInstaller` now applies explicitly supplied user units with the same
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

This is an installation primitive, not the complete installer by itself. Database
installation, model acquisition and the complete native application lifecycle
are not proved by its temporary-file and fixture-manager tests. A disposable
Debian 13 guest additionally ran the installed wheel under its actual user
manager: killing the synthetic worker restarted it and removed its old child;
unchanged apply did not restart it; stopping the unit removed both remaining
processes. The test unit was removed afterward. Native Claude, Secret Service
and model-runtime acceptance were exercised separately, not inferred from it.

The project has its own package, environment, lockfile and installed `anamnema`
entry point. The provisional development version is not a release.

Archive integration tests also require an available `mongod` executable. They
start their own process on an ephemeral loopback port with a fresh temporary
database directory, verify that directory before writing, and stop that process
afterwards. They never use an existing database or real keyring. A missing
executable fails the gate rather than silently skipping those tests. Providing
this test dependency is separate from the future Debian installer recipe.

```sh
uv sync --frozen --python 3.14 --extra reranker
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pyright
uv run --no-sync pytest --cov=anamnema --cov-report=term-missing -q
uv build
git diff --check
gitleaks dir --no-banner .
```

Build and test the wheel in an isolated environment outside the checkout; inspect
wheel and source-archive contents for packaged defaults, entry point, licence and
the absence of internal plans/private code. Test invalid input and reserved apply,
wrong OS/Python, missing executables, backend selection and unchanged user files.
Setup apply remains unavailable; processing apply is implemented separately as
documented in [PROCESSING.md](PROCESSING.md). Its integration tests additionally
start a synthetic loopback HTTP llama.cpp fixture, not a real model or GPU job.
Mocks prove bounded discovery only. Archive tests exercise a real disposable
MongoDB and a fixture-only keyring executable, including the installed CLI;
they do not prove real keyring unlock or native service installation. GPU
execution, Claude events and the clean-VM install remain required later gates.

Memory integration additionally requires Docker with permission to launch a
fixture-owned container. It uses Neo4j Community 2026.10 pinned by image digest
in `tests/graph_fixtures.py`, publishes only an ephemeral loopback Bolt port,
uses no host bind mounts and removes the container on teardown. This test
dependency does not change the native Debian installation target. Missing
Docker/Neo4j is a failed gate, not a skipped test. The reranker extra is required
for the full development gate: a generated synthetic ONNX model exercises actual
CPU inference without downloading model weights. See [MEMORY.md](MEMORY.md).
