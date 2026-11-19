# Contributing

Use Issues for reproducible bugs and Discussions for questions and ideas. Bug
reports should identify the operating system, model backend (vulkan/cuda/api),
version and redacted installer or operation output. Do not post conversation
archives, identity files, people cards, credentials, tokens or private addresses.

Contributions arrive here as issues or pull requests under Apache-2.0. Include
tests and update the README and affected contracts with behavior changes.
Unsupported platforms/backends must be described as proposed, not already working.

This repository is an exported copy. Maintainers check `COPY_DIFFERENCES.json`,
carry fixes back into the private working source by hand, verify the relevant
invariants and regenerate the public tree. A public fix is not complete until
its upstream port or intentional public-only difference is recorded. Never patch
both copies independently and assume they will remain aligned.

Maintainer replies posted from the owner's account end with the maintainer's
name so they can be distinguished from the owner's replies. Routine how-to,
triage and fixes follow this workflow. Security, licensing, personal information
and changes of project direction are escalated to the repository owner first.

GitHub Discussions, labels, forms and private reporting must be enabled and
verified during repository setup; these files alone do not enable account settings.
