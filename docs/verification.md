# OCI verification — 9 October 2026

PocketDeploy was exercised on a separately authorized OCI test deployment,
profile `pocketdeploy-oci-test`. Production `once-pocketcontext-v2` resources and
state were not adopted or changed. Real account/subnet identifiers, configuration,
SSH keys, SQLite state and Vault document references remain ignored locally.

## Live checks

- Created a dedicated NSG and ARM A1.Flex VPS using an existing public subnet.
  Shape: 1 OCPU, 6 GiB RAM, 50 GiB boot disk; Canonical Ubuntu 24.04.
- Verified SSH using the locally generated server key delivered by cloud-init,
  without trusting an unauthenticated first-connection key scan.
- Bootstrapped Docker from Ubuntu packages and checksum-pinned ONCE v0.3.3.
- Deleted the first disposable instance, boot disk and owned NSG through the
  controller, then repeated delete successfully. Shared networking remained.
- Recreated the verification host and deployed the CI-tested ARM64 demo image.
- Public `/up` returned HTTP 200 and `ok`. `/generation` initially returned
  `initial`, then `updated` after a parameter-reference environment update.
- Stop-first update passed restart fencing, clean exit, immutable image and
  named-volume continuity checks. Replacement health passed; no host pending
  marker remained.
- A subsequent unchanged converge returned no application actions and preserved
  the exact container identity. The OCI plan retained compute and firewall.
- Saved seven recovery files plus a consistent SQLite snapshot to a dedicated
  VaultContext vault. Restored an exact saved version into a separate private
  directory; its plan reported no infrastructure or application changes.
- `status` works without resolving application secret bindings. The SSH workflow
  was used for host inspection.

The retained live demo is HTTP-only at
<http://130.61.21.56.sslip.io/>. It contains synthetic test data and exposes only
the intentionally public test-generation value. TLS, production app migration,
DNS/SMTP ownership transfer and GitHub deployment-key publication were not tested
or implemented as production integrations.

The image is
`ghcr.io/pocketcontext/pocketdeploy-demo@sha256:0a856aee114c01e9af0aabd92f2b9563a649d11a2b6e2507b7f0f70009521433`.
Its CI verifies `/up`, the synthetic generation endpoint and clean shutdown before
publishing AMD64/ARM64 manifests. Anonymous registry pull was independently checked.

## Local and CI checks

The final local synthetic suite passed 61 tests. It covers configuration/binding validation, private SQLite
identity and file safety, backups/restores, OCI ownership/drift/interruption,
SSH trust, environment updates, stop-first/rolling behavior and retained data.

`devenv shell` successfully supplied the CLI requirements, including the pinned
VaultContext wrapper. The wheel installed and its command ran from an unrelated
`/tmp` environment. Both wheel and source archive were inspected for accidental
private-file inclusion; neither contained deployment state, keys or private
configuration. Git staging received a separate private-path and credential-pattern
check before public publication.

GitHub Actions runs the synthetic suite and builds distribution artifacts on each
push. Live OCI access is not part of CI. Failed historical operations remain in
private state for audit; the final host/application health is a separate observation.

## Limits

Docker's Ubuntu package version is not pinned. ONCE requires application
`--env` values in the trusted host's local process arguments; transport uses SSH
stdin and command/error logs suppress them. The pinned ONCE CLI cannot clear the
last environment binding, which is rejected explicitly. SQLite is plaintext
locally, encrypted only in Vault; one deployment operator is supported. Restoring
state does not restore the VPS or establish distributed ownership.
