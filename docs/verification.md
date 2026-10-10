# Deployment verification

## DigitalOcean live verification — 10 October 2026

A disposable Frankfurt deployment completed using the existing `default-fra1`
VPC. Profile `pocketdeploy-do-test`, deployment UUID
`522b26b4-20a4-4665-856b-065c87930ef2`, used Ubuntu 24.04 AMD64 and
`s-1vcpu-2gb`. Authentication used `COLORS_PAR_DIGITALOCEAN_ACCESS_TOKEN` from
ignored private bindings. The Droplet (`607963476`), dedicated firewall,
deployment tag and generated SSH files were deleted after testing. Independent
inventory at **19:10:37 UTC** showed zero Droplets, no test firewall/tag, zero
local resources or pending steps, and the original VPC retained.

Verified live:

- Provisioning, restricted SSH ingress, pinned host-key rotation, Docker and ONCE.
- Digest-pinned demo application with public, certificate-verified HTTPS.
- No-op plan/converge preserved the running container.
- Stop-first application update replaced the container and preserved a synthetic
  file in its named volume.
- Reboot changed the kernel boot ID; the updated container, volume file, HTTPS,
  Docker and ONCE recovered.
- Host metadata was reachable while container metadata requests were blocked;
  packet counters confirmed the DROP rule handled them, including after reboot.
- Resend port 2587 accepted certificate-verified STARTTLS with TLS 1.3 before and
  after reboot. No SMTP authentication or email was sent.
- Dedicated firewall source additions and removal; the final update completed
  with bounded asynchronous verification and no manual retry.
- Deletion preflight, application quiescence, resumed deletion and key cleanup.

The live run exposed API normalization (`action: allow`, all ports represented
as `0`), delayed inventory visibility and asynchronous firewall application.
The adapter now normalizes equivalent rules and waits after successful creates
and firewall writes without repeating uncertain mutations. During deletion,
DigitalOcean briefly removed the Droplet tag before removing the Droplet; the
live run safely resumed after disappearance. Post-delete polling now observes
the exact authorized ID until absent, with regression coverage; that final
polling change was validated synthetically, not with another paid deployment.
Normal ownership/preflight checks remain strict. Final validation: **688 tests
passed**, `uv build`, skill validation and `git diff --check` passed.

Safe receipts, synthetic probes and private state remain in ignored
`.colors-do-live-20261010/`. No shared networking, managed DNS, SMTP domains or
GitHub environments were changed. Application email delivery, Vault recovery and
populated application backup restoration were not tested. No live test server
remains. Package publication and portable launcher repinning remain pending.

## Google Cloud live verification — 10 October 2026

A separately authorized disposable deployment completed in project `pocketcontext`,
zone `europe-west3-a`, using the existing `default` network/subnet. Its profile was
`pocketdeploy-gcp-test`, deployment UUID `7623e166-825e-4939-b291-3c3a47e462bd`.
The E2 small AMD64 VM used Ubuntu 24.04 and a 20 GiB balanced persistent boot disk.
The VM, disk, both dedicated firewall rules and generated SSH files were deleted;
independent inventory checks completed at **18:48:01 UTC**. The shared network
remained, and both pre-existing project VMs retained their IDs and running state.
The former test hostname `34.89.231.180.sslip.io` is no longer this deployment.

The refreshed operator identity was Application Default Credentials, not an active
gcloud CLI account. Added explicit `gcp-auth: application-default` support instead
of silently falling back between credential sources. Default CLI authentication
is unchanged; combining ADC with `gcp-account` is rejected. Tokens were never
printed, stored in deployment state or sent to the host.

Live checks passed:

- Local initialization and read-only planning, then VM/disk/firewall creation.
- Pinned bootstrap SSH trust, replacement host-key verification, Docker and
  checksum-pinned ONCE v0.3.3 installation.
- Deployment of the pinned AMD64 demo image, public HTTP `/up` and synthetic
  `/generation`, followed by valid public HTTPS with ONCE-managed TLS.
- An unchanged plan reported only retained compute resources and no app actions.
  Repeated convergence preserved the exact application container identity; the
  mutation journal contained only the four original infrastructure creates.
- A stop-first environment update replaced the container, served the new synthetic
  generation, preserved the named volume identities and retained a synthetic file
  written under `/storage`.
- A host reboot was confirmed by a changed boot ID. Docker, ONCE, the same app
  container, public HTTPS and the synthetic volume file recovered.
- Both metadata DROP rules survived reboot. A container request increased their
  packet counters and could not reach IMDS, while the host could reach the same
  metadata endpoint. Root and host-network processes remain outside this boundary.
- Resend `smtp.resend.com:2587` accepted STARTTLS with certificate verification and
  TLS 1.3 before and after reboot. No authentication or test email was attempted.
- An owned SSH firewall source update was planned and applied through the Compute
  API without replacing the application or changing other compute resources.
- Deletion dry-run, clean application quiescence, remote resource removal and
  local SSH-key cleanup. Final cloud inspection found no deployment resources;
  SQLite reported zero resources and zero pending steps, with zero generated key
  files remaining.

The live configuration, private state and safe probe receipts remain under ignored
`.colors-gcp-live-20261010-183722/` in the local checkout. This test did not modify
root OCI configuration, production deployments, shared networking, managed DNS,
Resend domains or GitHub environments. The default VPC's inherited SSH permissions
are broader than the dedicated test rule, so this does not prove exclusive ingress
isolation. Application email delivery, Vault checkpoint recovery, populated
application restore and live fault injection remain separate checks. DigitalOcean was subsequently checked as recorded above.

After the authentication change: **676 synthetic tests passed**, `uv build`, skill
validation and `git diff --check` passed. Package publication and portable launcher
repinning were not performed.

## Multi-provider source validation — 10 October 2026

The working source adds DigitalOcean and Google Compute Engine adapters, shared
provider scope/recovery handling and configurable Resend SMTP transport. New
examples select port 2587 with STARTTLS; omitted settings preserve legacy 465.
The portable launcher still points to its existing published package, which does
not include these source changes. Run `uv run pocketdeploy` for the new adapters.

Validation completed locally:

- `uv sync --extra test` and final locked dependency synchronization.
- Full synthetic suite: 672 tests passed. Coverage includes preserved OCI hashes
  and scopes, offline provider initialization, cross-provider recovery rejection,
  both new provider lifecycles, uncertain operations, foreign firewall targets,
  image pinning, deletion receipt atomicity and SMTP transport validation.
- `uv build` produced the source archive and wheel. The wheel was installed in
  an isolated environment outside the checkout; CLI help, both provider imports,
  offline initialization, private key permissions and scope rejection passed.
- The existing published launcher passed 31 copied-launcher checks without cloud
  access. These checks do not establish new-provider support in that old pin.
- The updated devenv shell provided Google Cloud CLI successfully; the operator
  skill passed its validator and `git diff --check` passed.

No live cloud resources or emails were created. Provider API compatibility is
covered with synthetic fixtures, not an actual account rehearsal. Live startup,
reboot, firewall behavior, application SMTP and populated data recovery remain
unverified for the new backends. Package publication and launcher repinning are
not part of this source validation.

## Latest recorded OCI status — 10 October 2026

The `pocketdeploy-oci-test` deployment was intentionally deleted. The final
operation completed at 07:39:17 UTC after an initial preflight read failure and
a successful second delete invocation. Compute, firewall, boot volume, owned
DNS and SMTP resources, the GitHub deployment environment, and generated SSH
files were removed. Subsequent CLI checks reported zero recorded resources;
one historical pending step was preserved. Shared networking, management
credentials, private bindings, SQLite receipts, and Vault history remain.

The last locally acknowledged Vault checkpoint predates the final lifecycle
rehearsals and deletion. It is stale; remote Vault was not queried for newer
versions. Preserve the current local deletion receipt and private bindings.
Recreation requires separate authorization. The desired hostnames and earlier
IP addresses below do not describe running test services.

The bounded provider-read recovery release passed 550 synthetic tests and 31
copied-launcher checks. Its live verification was a read-only plan; no live
fault injection or additional deletion was performed after that release.
Populated application recovery, production ownership transfer, and live
reboot/packet-filter validation remain separate checks.

Source: [published lifecycle and retry receipt](https://wiki.pocketcontext.com/#/passages/13gfmw1e5k9xiqs)
(requires WikiContext authentication). This summary reconciles the recorded
history; it is not a new cloud inventory. The dated entries below preserve
historical behavior, commands, pins and observations, superseded where stated.

## Verification history — starting 9 October 2026

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

## Live integration completed after OCI renewal

Full convergence succeeded in 43.13 seconds against the existing OCI test VPS.
It created `www.bigconfig.online`, retained the existing HTTP demo, verified both
applications, and verified public HTTPS. The profile-named GitHub environment
`pocketdeploy-oci-test` was created with restricted SSH authority. Website root
and `/up` returned HTTPS 200.

The first SMTP test exposed Ubuntu s-nail14.9.24's rejection of the obsolete
password variable under v15 compatibility. A no-send debug check and complete
Resend message inventory confirmed the failed attempt had not sent the test.
Fix `534e83dc13133b239d0a22ac55381e7a6c74e849` uses credentials in the private
temporary mailrc URL (never argv) and reports fixed safe error codes. All 217
tests and package builds passed. The single authorized retry succeeded in 4.299
seconds from the VPS: Resend accepted a test from
`mail@notifications.bigconfig.online` to the user-selected recipient. This proves
SMTP acceptance, not human receipt or inbox placement. No additional test mail
was sent.

Website workflow run
https://github.com/pocketcontext/pocketcontext-website/actions/runs/37983137138
passed validation, both native image builds, manifest publication and dynamically
discovered deployments to `once-v2` and `pocketdeploy-oci-test`, including public
health verification. Existing Colors infrastructure was unchanged.

The portable launcher pins `534e83dc13133b239d0a22ac55381e7a6c74e849` and passes
19 isolated checks, including missing SMTP recipient rejection before deployment
access and expired OCI token rejection in 0.17 seconds. Private credentials, state
and SSH keys remain ignored; no Vault snapshot was performed.

The final live plan succeeded in 17.836 seconds: compute/firewall retained without
changed fields, all five DNS records no-op, and the mutable website image tag
requires its normal converge-time digest check. SMTP/GitHub report reconciliation;
no resource replacement was proposed.

## Git configuration recovery and disposable GitHub keys

New Vault snapshots exclude colors.yml and GitHub deployment key files. Synthetic
recovery checks cover current and legacy manifests, matching caller/destination
configuration hashes, dirty Git provenance, missing configuration and reserved
path aliases. GitHub tests cover missing/partial keys, private/public mismatch,
additive authorization, failed secret publication, interrupted preparation and
failed pruning with retry. No live Vault backup or credential rotation was
performed for this change. Existing Vault history is retained.

All 240 tests passed, package source/wheel builds passed, and the portable skill
validated. Three invalid rotation-option combinations were rejected before
configuration access.

The portable launcher pins source commit
`d2c143bea2d5384a0098c89fe8aecb998726dd49`; all 19 copied-launcher checks passed
without cloud access.

## Live deployment retirement

The user authorized deletion of `pocketdeploy-oci-test` after refreshing OCI
authentication. The root test configuration now retains its boot volume;
destruction protection stayed enabled in Git and was disabled only for the
authorized delete command through its parameter override.

The dedicated deletion dry-run passed in 6.505 seconds. Full preflight checked
OCI, GitHub, DNS and host readiness. GitHub environment retirement, CI fencing,
clean shutdown of both managed applications and website DNS removal succeeded.
OCI instance termination took 93.3 seconds. Firewall deletion was accepted but
still visible immediately afterward, so the first run correctly returned
`deletion_pending`, preserving its record and disposable keys. A retry completed
in 5.151 seconds and removed both GitHub key files. A third run completed in
5.053 seconds with no deleted resources and no key files removed.

Independent provider reads verified compute/firewall absence, absence of the
`www.bigconfig.online` A record, and absence of the `pocketdeploy-oci-test` GitHub
environment. The website repository retains `once-v2`. The 50 GiB boot volume
remains AVAILABLE and recorded for recovery. All four email DNS records remain,
and Resend reports `notifications.bigconfig.online` and its records verified.
Private bindings, SQLite and operator/server SSH authority remain locally. No
pending deletion steps remain; unrelated historical operations were not cleared.
The test website is retired. No Vault upload, SMTP email, or production deployment
was performed during this deletion test.

All 277 synthetic tests pass, including expired OCI token rejection before any
provider command, ownership failures before mutations, boot-volume identity
drift and interrupted retirement. Package builds and skill validation pass.
The final boot-volume preflight hardening was tested synthetically after the
retaining live termination; the live retries used the final implementation.

Published source `247525cac79788dc2264f79f0f03debb7e82e16a` passed GitHub CI.
The launcher pins that commit and passes all 19 copied-launcher checks. Its live
post-deletion dry-run completed in 1.685 seconds, reporting compute/firewall
absent and retained SMTP, disk and shared networking, with protection enabled.

## Recovery after recreation with an obsolete demo hostname

A user-triggered converge recreated compute at `132.226.198.73`, then failed
while deploying the old `130.61.21.56.sslip.io` verification app. Docker events
showed initial container start followed by proxy-route and container removal;
the configured hostname still resolved to the deleted VPS. Pinned ONCE performs
public HTTP verification and removes an initial deployment on failure. The
original CLI diagnostic was suppressed, so this cause is supported by observed
state and upstream behavior rather than a retained original error message.

Removed the obsolete demo from root desired configuration. Under local deployment
and remote host locks, verified the exact host/UUID pending marker, no manifest
or retirement fence, only the proxy container, no demo volumes and an empty
proxy route table. The bundled proxy lists a header-only table (no --json flag).
Archived only the matching pending marker as recovery evidence. No volume or
unrelated pending marker was deleted.

Convergence then succeeded in 43.086 seconds, retaining the recreated compute,
reusing DNS/SMTP and deploying only `www.bigconfig.online`. Application and public
HTTPS checks returned 200, host pending operations were zero, and GitHub
environment `pocketdeploy-oci-test` was recreated with disposable authority.
The exact failed local application step was resolved as superseded by the
successful convergence; unrelated historical operations remain unchanged.

Application diagnostics now expose fixed operation codes and guidance for image
pull/inspection, ONCE deploy/update, stop/removal, verification and health errors,
without raw subprocess output or credentials. All 280 tests and source/wheel
builds passed. No SMTP test email or Vault backup was performed.

## Full owned-resource deletion (2026-10-09)

Changed deletion to remove the owned sending key/domain, all website/email DNS,
active and historical boot volumes, and generated operator/server/GitHub keys.
Removed the boot-retention configuration option. External networking, management
credentials, Git configuration, SQLite receipts and Vault history remain.

Live preflight verified the active and historical boot volumes through recorded
IDs and exact OCI attachment history. Both inherited the instance's compute-role
tags; the controller now recognizes this same-deployment inheritance and migrates
verified disks to explicit boot-volume tags. Historical generated SSH files were
explicitly registered from the preceding authorized initialization/convergence
history; arbitrary imported keys are not implicitly adopted.

The live delete removed the GitHub test environment, quiesced the website, revoked
the Resend sending key, deleted its domain and all five DNS records, then
terminated the VPS. OCI firewall disappearance lagged its delete response; the
first run stopped with a resumable deletion_pending result after 142.006 seconds.
Retry completed in 33.189 seconds, including both 50 GiB boot volumes and seven
SSH key/trust files. Provider absence was verified before resource records were
removed. Local verification found zero resources and zero SSH files, while
configuration/private bindings/state remained. A repeat delete completed in
5.611 seconds with no changes. The production once-v2 environment remained.

Added bounded firewall disappearance polling to handle this OCI delay within one
run. Synthetic regression coverage verifies delayed disappearance and preserves
retry state on timeout. The full suite passes 310 tests; package builds and skill
validation pass. No SMTP email or Vault backup was performed.

Recreation provisioned a new VPS/firewall/boot volume and generated fresh SSH
keys. Host setup completed successfully (201.2 seconds including readiness).
The sending domain was recreated as 72952a40-0daf-4033-8f21-64835415ad2f and all
five DNS records were recreated. Resend verification remained pending across
three convergence retries, including the final 23.145-second attempt. Read-only
comparison found all four email records matching Resend requirements at
Cloudflare, both authoritative nameservers and resolver 1.1.1.1. This is an
external verification blocker; application delivery, HTTPS checks and GitHub
test-environment recreation have not yet run. Resume with converge after Resend
reports verified. Do not describe the replacement website as live yet.

Published source 363e79dd27e53293f4a12a6f53bab4c51c39134b and launcher pin
9932c4ebd2b6d90565a1446759c2726aff78a09d both passed GitHub CI. The portable
launcher passed 19 checks, including fast local expired-token rejection.

### Recreation completed (2026-10-10)

Resend subsequently verified the sending domain and all four email DNS records.
Resumed convergence completed in 47.462 seconds, creating the website application
and GitHub environment pocketdeploy-oci-test. The replacement VPS is
130.61.212.72. Public HTTPS verification returned 200 for www.bigconfig.online;
the application was running and healthy, with zero pending host operations.
The full owned-resource delete/recreate cycle is now complete. No test email
or Vault backup was performed during this completion.
