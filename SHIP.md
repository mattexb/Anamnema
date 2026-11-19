# Verification And Release

This first checkpoint is documentation-only. It is not an application release.
The public branch is `main`. Do not publish runtime code by passing docs-only
checks or describe unexercised installation/backend/substrate paths as supported.

## Documentation Checkpoint

1. Inspect the exact staged tree. No runtime code, private source, conversation,
   credential, identity file, database, generated local state or private report.
2. Check README status, scope, engineering/testing discipline, current structure
   and substrate support against the contents actually committed.
3. Verify relative links, full Apache-2.0 text, public governance and private
   security-report guidance. Ensure public tracker entries explain the copy.
4. Open the standalone flow at desktop and mobile sizes. Check all eight stages,
   expanded passages, keyboard operation, no horizontal text overflow and no
   private names/addresses. The chart represents the intended public copy.
5. Scan the staged contents and resulting commit for secrets and private data.
   An allowlist and a short name-pattern filter are not a complete secrets audit.
6. After explicit publication authorization, verify the repository's public URL,
   hosted flow link, issue form, labels, Discussions and private reporting.

## Runtime Release Gates

Future runtime contributions must include synthetic tests, appropriate focused
and integration checks, and full lint/type/test gates before release. Prove
ingest -> extract -> store -> recall -> source -> correction/card using isolated
stores, including failures and replay. Verify native substrate hooks and a clean
Linux VM with an API notes model. Test installer dry-run and actual selected
backend; an unimplemented backend must fail visibly. Update README, flow and
difference tracker in the same commit as each behavioral change.

Public fixes are carried back manually to the private working source against
the tracker and regenerated. Publication must preserve third-party licences and
notices, scan built artifacts and history, and report skipped or unavailable
checks rather than calling them passed. Security, licensing, personal-data and
direction questions go to the repository owner before public action.
