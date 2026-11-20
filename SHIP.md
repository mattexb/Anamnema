# Anamnema Verification And Commits

This project-owned procedure governs work here. Read it fully before staging.
It must not be overwritten by the old source-project template. The published
checkpoint is documentation-only, not a working application release.

## Hard Boundaries

1. KinTools is read-only: no edits to code, tests, config, docs, generated files
   or history. Read/copy reviewed source; neutralize it inside Anamnema. Do not
   use checks or formatters that write into the source tree. Leave earlier edits
   there untouched, including Elian's own earlier changes.
2. A bug or gap found in KinTools stops the affected work. Record evidence here
   and discuss the possible fix with Matteo. No source fix, silent workaround or
   automatic carry-back; an Anamnema grant does not authorize source changes.
3. Local commits only until the whole agreed code is present and verified.
   No push, remote branch/tag, release, publication or deployment. Matteo must
   explicitly reopen publication and provide fresh push authorization later.
4. System installation, deployed hook changes and live database/transcript
   mutations require separate authorization. Tests use synthetic records,
   temporary state and explicitly disposable databases.
5. The local plan records decisions and open questions. Discuss unresolved
   installation choices before implementing them. Internal handoff context is
   not part of the public distribution or documentation site.

## Ownership And Branches

Inspect `git status --short --branch` and the complete diff before editing or
staging. Own explicit paths; preserve unrelated work. One writer owns a worktree;
parallel writers need separate worktrees and an agreed integration procedure.

`main` currently holds the published documentation checkpoint. Implementation
uses local `feature/<scope>` or `fix/<scope>` branches; declare the transition in
README before starting. Do not switch another writer's worktree or invent shared
integration/release lines. A handoff gives exact branch, commit, uncommitted
paths, checks/results, unresolved issues and next action.

No force-push, rewritten shared history, destructive cleanup, broad staging or
hook bypass. A guard refusal is a stopping condition, not a reason to bypass it.

## Gates

Run focused checks while iterating and full relevant gates once at a coherent
checkpoint. Record exact commands/results and unavailable evidence. Required
gate failure or unexpected skipping blocks the corresponding commit/release;
silence does not accept a finding.

### Every Commit

- Review the complete intended diff and exact staged tree, including new files.
- Run `git diff --check` and `git diff --cached --check`.
- Update affected contracts, help, README, flow and difference tracker. Verify
  relative links, source/output hashes and appropriate third-party notices.
- Exclude credentials, personal fixtures, native conversations, identity files,
  databases, logs, local handoff plans and private runtime assumptions. Approved
  signed accounts/artwork do not authorize unrelated personal disclosures.
- Scan files before committing with `gitleaks dir --no-banner .`; scan the
  resulting commit with `gitleaks git --no-banner --log-opts=-1 .`. Missing tools
  or scan failures are reported and block completion, not silently skipped.

### Documentation

- Preserve full Apache-2.0 text, contribution/security guidance and truthful
  tested-versus-planned scope. Signed accounts change only with author approval.
- For layout changes, check GitHub-compatible rendering on desktop light/dark
  and mobile: images load, keyboard expansion works, no overlap/page overflow.
- Keep README and standalone flow passages synchronized. Check all stages and
  expanded passages on desktop and mobile when the flow changes.
- Prose-only changes do not require unrelated engine tests. Report that no
  runtime behavior was exercised; docs checks are not engine-readiness evidence.

### Code And Configuration

- Follow applicable engineering standards: small reusable modules, validated
  configuration, visible failures and no secret-bearing output.
- Test the actual changed path, including relevant failures, interruption,
  replay/idempotency, source attribution and ownership. Keep databases disposable;
  no fallback to a real store or source-project credentials.
- At a module checkpoint run its full tests, lint, type and package checks in
  Anamnema's own declared environment. At final integration run all application
  gates once. Passing private-source tests does not prove the neutralized copy.
- The first setup foundation's test/lint/type/build commands are declared in
  [docs/SETUP.md](docs/SETUP.md) and pyproject.toml. Expand them as modules arrive;
  absent commands are unfinished work, never a passing gate.
- Prove imports, packaged resources and child commands resolve without private
  modules, settings or home hosts. A CLI help test alone is not operational proof.

### Installer And Claude

- Prove dry-run makes no writes, package/service changes or hook edits.
- Test prerequisites, unsupported OS/backend, unavailable/locked keyring,
  non-destructive settings merge, partial failure and repeat/resume behavior.
- Exercise installed `anamnema` outside the source checkout and the actual Claude
  lifecycle: delivery, capture, identity/read gates and compaction continuity.
  Mocked hook payloads do not prove native registration or final-message capture.
- Default to localhost. Test the service's own access tokens and record ownership
  independently of any optional proxy. Unsupported paths fail explicitly.

## Local Commit Procedure

1. Complete applicable gates and record evidence in project-local notes.
2. Obtain a fresh explicit local `kin-commit` grant. Do not request `--push`
   during this phase. Check session, expiry, count and repository/branch scope;
   stop on refusal, expiry or exhaustion. Earlier grants cannot be reused.
3. Stage explicit paths in a separate `git add -- <paths>` call. Never use
   `git add .`, `git add -A` or force-add ignored private plans.
4. Review `git diff --cached` and staged whitespace. Refuse unrelated/private data.
5. Make one coherent Conventional Commit per granted commit. End its body with:

   ```text
   Signed-by: <author>
   Commit-mode: direct
   ```

6. Scan the resulting commit. Record hash/subject, gate results, unresolved
   findings and final `git status --short --branch`.
7. Leave it local. A checkpoint is not a release/version or permission to push.

## Gates Before Publication

When the whole agreed code is in, demonstrate:

1. Native import -> verified archive -> extraction -> storage -> search ->
   original source -> correction -> updated card/briefing, retaining the trace.
2. Failure, interruption, replay, malformed replies, unavailable dependencies
   and mixed-assistant access with isolated data; not only mocked success.
3. Native Claude continuity and complete operation/nightly commands.
4. End-to-end single-machine Linux setup, including the reviewed clean-VM API
   notes-model test. Only tested backend/platform paths count as supported.
5. Full application gates, inspected wheel/archive contents, and installation
   without a private checkout, credentials, hosts or machine runner.
6. Reviewed copy provenance, repeatable transforms and retained third-party
   licences/model terms. Do not overwrite project-owned policy or local records.
7. Current README/flow, measured-versus-estimated hardware information and final
   artifact/history privacy scans. State skipped/unavailable checks honestly.
8. Release evidence naming exact commit/artifact checksums, results, unresolved
   issues and Matteo's decision on any nonblocking issue. Escalate security,
   licensing, personal-data and direction questions before public action.

Only after Matteo explicitly reopens publication and authorizes push may the
accepted commit be published. Then verify public URLs, hosted flow, issue form,
labels/topics, Discussions and private reporting. Do not create a release or
compatibility claim before the relevant acceptance evidence exists.
