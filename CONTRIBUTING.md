# Contributing

Use Issues for reproducible bugs and Discussions for questions and ideas. Bug
reports should identify the operating system, model backend (vulkan, cpu, cuda or
api), the Anamnema version and redacted installer or operation output. Do not post
conversation archives, identity files, people cards, credentials, tokens or
private addresses.

Contributions arrive here as issues or pull requests under Apache-2.0. Include
tests and update the README and affected contracts with behavior changes. A change
that adds a platform or backend includes its tests and its acceptance results.

## Development

From a clone, with [uv](https://docs.astral.sh/uv/):

```sh
uv sync --frozen --python 3.14 --extra reranker
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pyright
uv run --no-sync pytest --cov=anamnema --cov-report=term-missing -q
uv build
gitleaks dir --no-banner .
```

The archive tests need a `mongod` executable and the memory tests need Docker,
which they use to start a disposable Neo4j; both start, use and remove their own
instance. [docs/SETUP.md](docs/SETUP.md) describes what each group of tests
covers, and [SHIP.md](SHIP.md) the release gates.

## How changes travel

This repository is exported from the maintainers' working source. Maintainers
record every public-only difference in `COPY_DIFFERENCES.json` and carry accepted
fixes back by hand, so the two copies never drift apart unnoticed.

Maintainer replies posted from the owner's account end with the maintainer's
name. Routine how-to, triage and fixes follow this workflow; security, licensing,
personal information and changes of project direction go to the repository owner
first.
