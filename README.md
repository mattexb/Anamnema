> **IN PROGRESS: this repository does not contain installable application code yet.**
> This initial documentation commit describes the intended public project.

# PublicMemory

A self-correcting memory layer whose notes retain their source records,
attribution, dates and correction history.

**Status: documentation bootstrap, not an installable release.** This tree currently
contains the public contracts, flow chart and contribution policies.
The engine, operation commands, executable tests and installer are not exported yet.
No setup command in this checkpoint starts a service or processes real data.

## Design

Native conversations enter an append-only archive. Notes refer back to exact
records; a graph holds the notes, people/things, cards, topics and lessons.
Recall and briefings deliver dated evidence. Checking a note against its source
can correct it without erasing the conversation it came from.

The first installation target is one Linux machine with localhost services and
Vulkan inference or a configured model API. The configuration reserves a CUDA
backend; CUDA, Windows and macOS are later targets, not supported in this
checkpoint. A model API does not remove the encoder/reranker requirements.

## Contracts And Validation

- [The memory flow, explained visually](https://mattexb.github.io/PublicMemory/memory-flow.html) ([standalone HTML](docs/memory-flow.html))
- [Ingestion, providers, substrates and acceptance](docs/CONTRACTS.md)
- [Intentional copy differences](COPY_DIFFERENCES.json)
- [Contributing](CONTRIBUTING.md) and [private security reports](SECURITY.md)

The public tree is a generated copy of a private working source, not a runtime
dependency on it. Each export records its source revision and content hashes.
Public fixes are carried back manually before re-export, using the difference
tracker to preserve intentional changes.

## How We Write Code

We start with the behavior and its boundaries, then implement small, reusable
pieces. Deployment-specific paths, endpoints, model choices and limits belong
in configuration. Failures must be visible: an unavailable service is not an
empty successful search, and a partial run is not reported as complete.

Tests are part of each change. We run focused tests while developing, integration
checks for the affected workflow, and the full project gates at release
checkpoints. Tests use synthetic conversations and isolated stores; they must
not read personal memories or reset an operator's live graph. Failure paths,
interruption, replay/idempotency, source attribution and instance isolation
matter as much as the happy path. README and flow changes accompany behavior
changes in the same commit.

We report what was actually checked, including skips and missing evidence.
Passing a mock-based test does not establish native hook compatibility, a real
model response or a successful clean-machine installation. This bootstrap is
not an engine test result or an installer certification.

## Scope

The target workflow is transcript ingestion, source-linked notes, graph storage,
retrieval, cards, briefings and source-backed correction. It includes operation
commands, lifecycle hooks and an ordered installer. The default is one machine,
localhost services and no proxy. Remote access is optional, with generic nginx
TLS/forwarding and application-level authentication.

This is memory above the model, not weight training, an oracle, or a guarantee
that every recalled note is correct. Private infrastructure automation, native
transcript deletion/trimming, private secret managers and personal archives are
outside the public product. Third-party model/API use may send selected text
outside the local machine and must be chosen knowingly.

## Project Structure

The current exported tree contains:

```text
README.md                  Scope, engineering discipline and current status
LICENSE                    Apache-2.0
CONTRIBUTING.md             Contribution and upstream-copy workflow
SECURITY.md                 Private security reporting
COPY_DIFFERENCES.json       Export provenance and intentional differences
SHIP.md                    Documentation checks and future release gates
docs/CONTRACTS.md           Ingestion, providers, substrates and acceptance
docs/memory-flow.html       Standalone public flow chart
.github/                   Bug form and declarative labels
```

Engine source, synthetic fixtures, tests, commands and installer directories will appear with their
implementation, not as empty scaffolding. The chart describes the target public
architecture and marks this pre-release status; it changes as public behavior
differs from the private implementation. A hosted chart link is added only after
the documentation site is enabled and verified. The source link remains usable
as a standalone HTML download if the documentation site is temporarily unavailable.

`main` is the public documentation branch. There is no application release or
supported installation in this initial commit.

## Substrate Integration

This section grows with each native integration. "Existing" describes the
original system; it does not certify an untested public installation.

| Substrate | Existing integration | Public-copy status |
| --- | --- | --- |
| Claude Code | Working memory reader and lifecycle integration | Export/configuration and public installation validation pending |
| Codex | Native transcript archive support; consolidation reader and hook adaptation needed | Next integration target; acceptance pending |
| Hermes | Native transcript preservation support | Memory adapter and native hook validation planned |
| Antigravity | Native transcript preservation support | Memory adapter and native hook validation planned |
| Prime | Native transcript preservation support | Memory adapter and native hook validation planned |
| Grok | Native transcript preservation support | Memory adapter and native hook validation planned |

Each supported integration will document its native setup, configuration,
transcript format, hook/event coverage, model provider, read/capture behavior,
limitations and reproducible acceptance checks. Shared archive support is not
the same as working memory delivery. No later substrate is marked supported
until its own end-to-end path has been exercised.

## Licence

Apache-2.0; see [LICENSE](LICENSE). Third-party models and services keep their own
licences and are not redistributed merely because the application supports them.
