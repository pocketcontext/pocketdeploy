# Audit remediation — 2026-10-13

The audit covered commit `9330b3c`. This change removes unused secret-bearing
state, prevents an SSH firewall configuration without port 22, authenticates
tunnel readiness and makes host readiness require a successful redirect chain.

## Changes

- Stop saving `desired-config`. Matching writable state opens remove existing
  entries with SQLite secure deletion enabled. Read-only opens preserve state.
- Exclude resolved and literal application environment values from new operation
  fingerprints. These hashes journal operations; actual host settings still
  determine whether an application needs updating.
- Require port 22 in `compute-ssh-ports`; additional ports remain supported.
- Wait for an acknowledgement from the authenticated SSH session after forwarding
  initialization. A competing local TCP listener cannot satisfy readiness.
- Require final HTTP 2xx at the host, with up to five same-origin redirects and a
  shared probe budget. Reject loops, absent locations and origin/scheme changes.
  Restricted GitHub delivery uses this same health check.
- Remove diagnostic metadata when operations are pruned, including old orphans,
  while preserving running operations and unresolved intents.
- Pin CI actions to full commits resolved from the existing upstream major tags.
- Document local lock directory permissions and remote ONCE argument exposure.

## Remaining limits

The pinned ONCE v0.3.3 accepts application environment and SMTP settings through
command-line flags. Local source inspection found stdin support for registry
passwords only. Host processes allowed to inspect arguments can read application
secrets during deployment. SSH stdin protects the controller transport, not the
remote ONCE process arguments.

Deleting the retired SQLite entry does not erase old encrypted Vault versions,
filesystem snapshots or other historical copies. Necessary SMTP credentials
remain in private controller state. Restored private bindings are executable
when subsequently sourced by the operator's trusted shell; restoration itself
never executes them.

Fresh-state CLI plans retain local locking and its mode-0700 directory behavior,
now explicitly documented. The audit's line references and claims were corrected
in the workspace audit document. Public health's 2xx requirement was never a
universal prohibition on redirects: a redirect ending at a 200 login page can
pass both checks, so use an application readiness endpoint.

## Validation and release

Source, isolated installed-wheel and published-package suites each passed 953
tests and 4 subtests. The wheel and source archive built successfully. The
published package is `766d27e7159efa06b7b5c96a74e5117b22019a30`; its Test and
verification-image workflows passed, including image smoke and shutdown checks.
The portable launcher was then pinned to that commit and passed 37 checks from
an unrelated directory, including rejection of an SSH port list without 22.
Local validation used synthetic isolated fixtures without cloud access. CI
published the disposable verification image; no deployment was changed.
Previously copied launchers remain pinned until their owners update them.
