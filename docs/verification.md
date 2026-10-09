# OCI verification — 9 October 2026

PocketDeploy was exercised on a separately authorized OCI test deployment,
profile `pocketdeploy-oci-test`. Production `once-pocketcontext-v2` resources and
state were not adopted or changed. The non-secret live desired configuration, including account/subnet identifiers,
is tracked in root `colors.yml`; `.envrc` is tracked too. SSH keys, private
bindings and SQLite state remain ignored at the repository root. Earlier restore
verification files are preserved outside the repository under the operator’s
`~/.local/state/pocketdeploy/` directory.

## Historical initial live checks

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

The initial implementation’s local synthetic suite passed 61 tests. It covers configuration/binding validation, private SQLite
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

## Portable launcher verification

The portable uv launcher pins published PocketDeploy commit
`78fc76f04c9f3d0df227205416e10645680abbfe`. Configuration discovery and explicit
file selection increased the synthetic suite to 66 passing tests. Six additional
copied-launcher checks passed outside the checkout, including an unrelated
project with invalid dependencies, nested configuration discovery, explicit file
selection and nonzero failures. These checks block external deployment tools.
The skill passed its frontmatter validator and independent operational review.
A copy of the launcher outside the checkout also ran from its `docs/`
subdirectory against the live test deployment: status was healthy and the plan
reported no changes. No cloud mutation was required for this verification.

## Explicit Vault workflow

The current interface separates local initialization and encrypted snapshots
from cloud workflows. `init` prepares local UUID/state/SSH authority without
cloud calls. `vault-save` is explicit; `create`, `converge`, `delete` and failed
workflows do not save automatically. The former `vault-save-after-run` setting
is rejected, including false. The earlier live checks above used the initial
implementation, which supported automatic snapshots; they are historical
evidence, not a claim that the new explicit workflow was rerun live.

The intended first-deployment sequence is `init → vault-save → create →
vault-save`; routine updates use `plan → converge → vault-save`. A missing or
failed explicit save leaves an older remote checkpoint. Local state must be
preserved, and recovery must reconcile cloud reality before new mutations.

Validation of the explicit workflow passed 83 synthetic tests and eight copied
launcher checks, including offline `init`, repeated initialization without key
replacement, and success/failure paths that never invoke Vault. The portable
launcher now pins published package commit
`14e72c350e6da55cc2e9c6f4347075dea454fd4d`. Skill validation and package builds passed.

A live unchanged convergence was attempted after removing the automatic-save
setting. It reached compute and failed in about one second; OCI local session
validation reported an expired/invalid session, and CLI refresh failed. That attempt did not complete convergence or create a Vault checkpoint.

After the user refreshed the OCI token, the published portable launcher completed
an unchanged live convergence in 15.4 seconds. It reported zero application
changes and performed no automatic Vault save. Follow-up status confirmed a
healthy running application with no pending host operations; the subsequent plan
retained compute/firewall and reported no application changes. No new Vault
checkpoint was created during this validation.

## Text and JSON output verification

The portable launcher pins package commit
`6f07340332d614548a4cdd9e9fc1d4744e8898f9`. The suite passed 111 synthetic tests
and 11 copied-launcher checks. Skill validation and package builds passed.

Live unchanged convergence produced a human summary on stdout and timed stage
progress only on stderr: compute 11.0s, host 2.5s, applications 1.2s, verification
0.4s. A subsequent `plan --json --quiet` returned one versioned success envelope,
empty stderr and no changes. An explicit harmless SSH command printed a test
marker and exited 7; the launcher preserved exactly that stdout and exit code,
without appending a result. No Vault snapshot was created.

## No-argument help

The launcher now pins `03c542e42f115c09ad2ccc88e43d3934e79c9485`. Running it
without arguments prints help on stdout and exits 0, without loading configuration
or accessing deployment resources. Unknown commands and a lone `--json` still
exit 2. Validation passed 116 tests, 12 copied-launcher checks, skill validation
and package builds.

## Current-directory configuration

The launcher now pins `40f20bb2bbbd848e3a8c7bd10b1a7988faa18279`. Default
configuration selection uses only `colors.yml` in the current working directory;
parent files are ignored. Explicit `-f` paths remain relative to the caller, and
state/key paths remain relative to the selected configuration. Validation passed
117 tests, 12 copied-launcher checks and skill validation. Earlier upward-search
verification above describes historical behavior.

## Verbose diagnostics and expired OCI sessions

The launcher now pins `4b250673977f1d0329e38251860c0d17aaaa4efb`. Validation passed
146 tests, including safe OCI/SSH timing output, heartbeat cleanup and distinct
expired-token versus rejected-authentication errors. The copied launcher passed
15 isolated checks. A synthetic expired JWT was rejected in 0.17 seconds before
the guarded OCI executable could run.

The operator's current OCI session had also expired. The published CLI rejected
it locally with `oci_token_expired`, exit 1 and exact profile-specific refresh
and reauthentication guidance in 0.90 seconds including launcher startup (5 ms
in the CLI). No token contents or provider responses were emitted. This verified
the real failure path; a successful live verbose plan was not run with renewed
credentials during this change. No cloud resources or Vault snapshots changed.

## Region-aware session guidance

The launcher now pins `f7293be492dbfc9c8d3ef3d516087a848c31b6dd`. Renewal
guidance includes the effective region from desired configuration, the OCI region
environment override or the selected native profile, in that order. Unknown or
unsafe region values produce an explicit placeholder rather than a guess.
Validation passed 150 tests, 15 copied-launcher checks, skill validation and
package builds. The synthetic expired-token check verified both region-qualified
renewal commands and failed locally in 0.17 seconds without invoking OCI.

## Managed DNS, SMTP and GitHub integration — 9 October 2026

Added profile-named GitHub environments, ownership-checked Cloudflare DNS,
retained Resend sending infrastructure and explicit VPS s-nail testing. Removed
`create` without an alias. The root desired configuration preserves the existing
HTTP demo and adds `www.bigconfig.online` with sending identity
`mail@notifications.bigconfig.online`.

The website workflow was published at
`7e029322b722d2f8064d5fa9ff286b024d10f167`; run
https://github.com/pocketcontext/pocketcontext-website/actions/runs/37980840096
passed checks, native image builds, dynamic environment discovery and actual
`once-v2` deployment/health. Removed only the stale website-repository
`once-oracle-ampere` environment; the Colors server and its repositories were
preserved. `once-google` was already deleted. Every remaining environment is
an active target, with no opt-in flag.

Devenv built and executed Cloudflare CLI 1.0.0-beta.14, Resend CLI 2.23.0 and
s-nail 14.9.25. npm transitives and source downloads are integrity-pinned. Actual
CLI help and offline dry-runs verified Cloudflare/Resend command contracts.

Live provider reads identified existing bigconfig.online zone
`f8d9f9cb95c9431f754df2adec8fd504`. A dedicated zone-only DNS Write/Zone Read token
was created using authorized account rotation authority; its private value is
stored only in ignored `.envrc.private`. No rotation credential was copied.
Live plan retained OCI compute and firewall. Convergence created and verified
only the owned website A record, then stopped at Resend domain creation. The
application/GitHub/SMTP delivery checks have not yet completed; failure handling
and subsequent recovery are recorded below when resolved.

Resend's final classified response was HTTP 403/domain-limit rejection
(`provider_domain_quota`). Repeated complete domain listings confirmed
`notifications.bigconfig.online` was absent before the explicit diagnostic
retries. No Resend domain or sending key was created, no email was sent, and
no PocketDeploy GitHub environment was published. No unrelated mail domains
were changed. The recorded pending domain creation remains blocked for explicit
reconciliation after the account domain allowance is resolved. Error parsing now
handles CLI chatter preceding JSON without emitting provider-controlled text.
Live website deployment, HTTPS, SMTP acceptance and PocketDeploy CD verification
remain pending this external account limitation.

Published implementation `24a90cb04e2af0904b661bd133d7c169b0f20eb6` passed
209 tests, skill validation and package builds; its GitHub CI passed at
https://github.com/pocketcontext/pocketdeploy/actions/runs/37982262752 .
The portable launcher pins that immutable implementation and passed 17 isolated
checks, including removed-command rejection and expired OCI token rejection in
0.17 seconds without invoking OCI. Published `init` also succeeded against the
existing test deployment, preserving UUID and preparing its separate GitHub keys
without cloud/provider calls. No Vault snapshot was taken.

## Resend quota resolved — live retry

After the user fixed the Resend account, a complete domain listing confirmed
absence and the pending creation was explicitly reconciled. Domain
`notifications.bigconfig.online` was created as
`00de3ff5-8582-4b4a-9327-c86064ed5472`. The live response included a zone-relative
tracking CNAME (`rsend.notifications`), exposing a normalization omission. Fix
`f12fea208fd330b420c756ca2a37c7367e51fe94` handles zone-relative records only when
the resulting FQDN remains within the sending domain; a regression verifies that
other subdomains are rejected. It passed 210 tests and package builds. Launcher
pin commit `9326df1` passed 17 copied-launcher checks and GitHub CI.

All four email DNS records were created and resolved publicly. Resend subsequently
reported the domain and every record verified. The next full convergence stopped
at local OCI token expiry before compute operations. Noninteractive OCI session
refresh failed; renewed interactive authentication is required. No SMTP test
email has yet been sent, and app/HTTPS/GitHub verification remains pending.
