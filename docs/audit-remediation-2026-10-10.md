# Audit remediation — 2026-10-10

The audit identified defects in committed source and fixes present only in the
working tree. This release includes those pending state-retention, path-validation
and Vault recovery fixes, plus the following changes:

- Rotate the metadata bootstrap SSH key before sending application credentials.
  Persist the replacement and instance binding before installation, verify a new
  connection, and resume interrupted installation with the same key. SSH uses
  explicit trust settings without inherited configuration or multiplexing.
- Provide `rotate-host-key` for upgrading existing deployments without
  provisioning or reconciling applications. Save a new Vault checkpoint afterward.
- Install Docker pre-start metadata filtering during full host bootstrap.
- Add and verify firewall rules before removing stale rules. Preserve uncertain
  outcomes and resolve matching legacy intents only after observed convergence
  or verified deletion.
- Require existing state for adoption; verify and record compute, firewall and
  boot-volume ownership, with resumable adoption.
- Preserve configured image references during CI digest releases; compare the
  resolved running image before replacement and migrate compatible old records.
- Validate SSH usernames, reject unsafe privileged authorized-key paths, report
  retirement stages correctly, accept successful public HTTP 2xx responses and
  reject malformed OCI instance/create responses safely.

## Limits and operating changes

Initial bootstrap still trusts the key in OCI metadata. Someone who can read
metadata and intercept first contact can impersonate the bootstrap host; rotation
is not independent out-of-band authentication. The replacement key never enters
metadata. Filtering covers forwarded container traffic, not root or host-network
processes. Live OCI/bootstrap/reboot behavior requires separately authorized
verification; this work uses isolated synthetic fixtures.

Ordinary SSH operations reject unverified bootstrap trust. Run `rotate-host-key`
or `converge`, then `vault-save`. A pre-rotation checkpoint cannot recover a lost
replacement key. Standalone rotation does not reconcile GitHub variables; full
convergence republishes the new host pin for CI.

An uncertain firewall addition remains blocked while desired rules are missing.
After establishing that the provider request has settled, an authorized operator
can restore the pending desired rules on the same owned NSG and retry. Merely
inspecting the rules does not clear the checkpoint.

Non-secret operator resource identifiers remain tracked as required by AGENTS.md.
They are not credentials, but the public configuration exposes infrastructure
identifiers. No live infrastructure inventory or historical secret-scan assurance
is inferred from local test success or a local deletion receipt.

## Validation

Regression coverage includes interrupted rotation, fresh host-key proof, effective
OpenSSH configuration, Vault recovery of rotation state, pending-operation
retention, firewall failures and legacy recovery, adoption, CI digest delivery,
symlink/hardlink rejection and HTTP health behavior. The release procedure runs
the full suite, builds the wheel and source archive, tests the installed wheel
outside the checkout, then tests and pins the published portable launcher.

Local source and isolated installed-wheel validation each passed 441 tests.
Both the wheel and source archive built successfully. These results do not
include live cloud provisioning, host reboot or packet-filter verification.
