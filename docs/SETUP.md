# Setup Planning Foundation

Current scope: configuration validation, a setup wizard and read-only prerequisite discovery.
This is not an installer yet. The first reviewed target is Debian 13 trixie,
native services, one machine, Claude first. No containers are selected.

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
configuration loading or inspection because execution is not implemented.
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
services name one account for all components. System database accounts may be
left to the native packages. The application's keyring belongs to its own account.
These are requested arrangements, not verified service capabilities.

Follow the [manual keyring guide](KEYRING.md) if you prefer to prepare it yourself.
The guided choice records the desired arrangement; actual account creation,
keyring configuration/unlock and native service activation are not implemented.
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

## Ordered Install Contract

The future apply path must prepare prerequisites, configure keyring, install
MongoDB and Neo4j, set up the models, install the application, merge Claude hooks,
register nightly work, then verify the complete installed workflow. Dependent
steps stop on failure; resume/replay must not destroy state or double-register.

Before writing native service actions, review privilege/service-account and
headless keyring unlock policy with Matteo. The wizard collects intentions, not
verified service access. Database/model installation recipes,
licence/download consent, existing-install preservation and actual package
availability on Debian 13 still need verification. Do not turn PATH discovery
into a false database/keyring health check or report an incomplete stack ready.

## Development Gates

The project has its own package, environment, lockfile and installed `anamnema`
entry point. The provisional development version is not a release.

Archive integration tests also require an available `mongod` executable. They
start their own process on an ephemeral loopback port with a fresh temporary
database directory, verify that directory before writing, and stop that process
afterwards. They never use an existing database or real keyring. A missing
executable fails the gate rather than silently skipping those tests. Providing
this test dependency is separate from the future Debian installer recipe.

```sh
uv sync --locked --python 3.14
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
