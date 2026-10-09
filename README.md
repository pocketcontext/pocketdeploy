# PocketDeploy

Deploy an OCI VPS and ONCE applications with Python and CLI tools. Blue supplies
the workflow DAG; there is no Terraform, OpenTofu, Ansible or Clojure runtime.

`colors.yml` is desired configuration. A private SQLite file records identities,
operations, resolved settings and recovery checkpoints. VaultContext stores
versioned encrypted recovery sets. The package and the live OCI test deployment
share this repository. `.envrc` and `colors.yml` are tracked in Git. Private
bindings (`.envrc.private`), state (`.colors.sqlite`) and keys (`.ssh/`) live
alongside them and remain ignored. There is no `.private` directory.

## Setup

Install Nix and devenv, then:

```sh
devenv shell
uv sync --locked --extra test
pocketdeploy --help
```

`devenv.nix` provides Python 3.12, uv, OCI CLI, OpenSSH, Git, GitHub CLI, curl,
jq, SQLite and a pinned VaultContext CLI wrapper. `devenv.lock` and `uv.lock` pin the development environment and
Python dependencies. Blue is pinned to a tested Git commit. `direnv allow` is
optional; `.envrc` loads devenv and an optional ignored `.envrc.private`.
Nothing automatically sources private files during deployment or restoration.

The portable launcher `./pocketdeploy` is a symlink to the executable bundled in
`skills/pocketdeploy/`. Copy that executable into a deployment repository or onto
PATH; uv fetches its immutable PocketDeploy package pin and Python dependencies.
It runs the published package, even inside this source checkout. For local
development use `uv run pocketdeploy` or devenv’s `pocketdeploy` command.
External tools still come from devenv or your PATH. Installed packages also
provide the `pocketdeploy` command. Vault workflows require
an authenticated account and an interactive user-unlocked session. Outside
devenv, install `vaultcontext` separately or set its `vault-command` path.

For a new deployment, copy `examples/colors.yml` into its repository as
`colors.yml`. The root configuration in this checkout operates the existing test
deployment; do not reuse its identity for a new deployment. Replace the example
OCI identifiers with your existing compartment, public subnet and
availability domain. Configure your OCI CLI authentication profile; the default
auth mode is `security_token`. The existing subnet/VCN, internet gateway, routes
and subnet security lists remain externally managed. Inherited subnet rules can
grant access beyond the dedicated NSG; an NSG does not subtract those permissions.

## Portable skill and launcher

Install the skill for your agent from this repository:

```sh
npx skills add pocketcontext/pocketdeploy --skill pocketdeploy --agent codex --yes
```

Copy the installed skill's `pocketdeploy` executable into your deployment
repository and make it executable. Track that launcher alongside `colors.yml`
and `.envrc`. Its package commit is explicit in the script; updating the skill
alone does not change a previously copied launcher.

Commands use `colors.yml` in the caller's current working directory only;
parent directories are not searched. `-f /path/to/colors.yml` explicitly selects a deployment. State and key
paths resolve relative to that configuration, never relative to the launcher.
The launcher does not load `.envrc.private`; use direnv or your trusted shell.
See [the skill](skills/pocketdeploy/SKILL.md) for operator instructions.

## Commands

```sh
pocketdeploy init
pocketdeploy vault-save
pocketdeploy converge
pocketdeploy vault-save

# Routine update
pocketdeploy plan
pocketdeploy converge
pocketdeploy vault-save
pocketdeploy status
pocketdeploy ssh
pocketdeploy ssh --ssh-command 'uname -m'
pocketdeploy delete --dry-run
```

`init` prepares the local deployment UUID, SQLite state, SSH client and host
keys, known hosts and an empty `.envrc.private` if absent. It makes no cloud or
Vault calls, does not resolve application bindings and preserves existing files.
Repeating a healthy initialization is safe; missing authority for an existing
instance still requires recovery. Use `init → vault-save → converge → vault-save` for a new deployment
when encrypted recovery is configured. The first snapshot preserves authority
before provisioning; the second records the resulting resource identities.

`converge` runs a DAG: preflight → keys → OCI → host → DNS/SMTP → applications → HTTPS verification → GitHub.
Each external mutation records intent first and the verified result afterward.
An unchanged converge makes no OCI mutations and no application replacement.
Host setup still verifies/enables services, and mutable image tags are pulled to
check whether their immutable digest changed. Prefer digest-pinned app images
for reproducibility. `plan` never creates cloud resources or keys; mutable image
tags may need verification at converge time. Local lock files may be created.

`status` returns safe inventory, operation and host application status.
It never prints resolved environments, credentials,
raw Docker metadata, ONCE labels or cloud response bodies.
`state.last_vault_backup` reports the locally acknowledged document/version and
UTC `saved_at` (or null when no receipt is known). Older receipts may lack a
timestamp. This makes no Vault call and does not prove checkpoint freshness.
`ssh` is explicitly
interactive; commands you choose can print private information in your terminal.

`compute-prevent-destroy: true` is the default. To delete, deliberately set it to
false, review `delete --dry-run`, then run `delete`. Its dedicated DAG is:
preflight → retire GitHub environments → fence CI and stop applications → remove
website DNS → terminate compute and remove firewall → remove disposable GitHub
keys. Preflight checks all recorded targets and host readiness before any external
mutation; dry-run uses these same checks without provisioning drift checks.
Read permission checks cannot guarantee a later write will be authorized.

Only recorded resources with matching ownership may be removed. Host retirement
uses the same lock as deployment, fences queued CI commands and disables restart
before gracefully stopping all manifest-owned applications, including apps removed
from desired configuration. Unclean stops or ownership changes block deletion.
Retirement is permanent on that host; a retained disk is recovery material, not
an automatically restartable deployment. A stopped/terminating instance requires
matching shutdown evidence; inspect failures and rerun `delete` to resume.

`compute-retain-boot-volume: true` is the default; retained disks remain recorded.
Interrupted termination retains the policy recorded when it started. A disposable
test may explicitly set false. Sending domains, SMTP credentials, email DNS,
shared networking, operator/server SSH keys, local state and Vault data are
retained. Disposable GitHub key files are removed only after infrastructure
deletion succeeds. No backup is made automatically.

The result lists deleted and retained resources. Failures identify their stage
and previously completed stages; completed provider deletions are reconciled on
retry. GitHub jobs that already captured a retired environment may fail, but
host retirement prevents them from restarting applications. After full deletion,
`converge` can provision a new instance; retained disks are not automatically
reattached or restored.

## Command output

Commands print a readable summary to stdout by default. Use `--json` for
scripts: stdout contains exactly one JSON envelope with `schema_version: 1`,
`command`, `ok`, and `result` on success or `error` on failure. Error objects
contain a safe `code` and `message`, plus the failing `stage` when known.
Failures still exit nonzero. Output format does not change when redirected.
Existing scripts that read JSON must add `--json` and read the `result` field.

```sh
pocketdeploy status --json | jq '.result'
pocketdeploy plan --json --quiet > plan.json
pocketdeploy converge --quiet
pocketdeploy plan --verbose
```

Progress and stage durations go to stderr. `--quiet` suppresses progress, while
preserving the stdout result and text-mode error diagnostics. With `--json`,
controller errors are part of the stdout envelope; launcher/runtime diagnostics
may still reach stderr. Application failures identify a fixed operation code, such as
`application_image_pull` or `application_deploy`, with troubleshooting guidance.
ONCE deployment includes public hostname verification; a hostname containing an
old VPS address must be updated or removed after recreation. Raw subprocess
output remains suppressed. Exit codes are 0 for success, 1 for operation failure,
2 for invalid usage and 130 for interruption.

`ssh` passes through remote stdout/stderr and exit status without a result
footer. It rejects `--json`; capture remote command output directly if needed.

## Configuration

The flat Colors format and `COLORS_PAR_*` parameter namespace are preserved.
For example `COLORS_PAR_OCI_OCPUS` overrides `oci-ocpus`. Application bindings:

```yaml
once:
  applications:
    - host: wiki.example.com
      image: ghcr.io/example/wiki@sha256:REPLACE_WITH_DIGEST
      deploy-strategy: stop-first
      deploy-stop-timeout: 300
      auto_update: false
      auto_backup: false
      smtp: false
      manage-dns: false
      env:
        LITESTREAM_ENDPOINT: app-wikicontext-production-litestream-endpoint
```

The mapping references
`COLORS_PAR_APP_WIKICONTEXT_PRODUCTION_LITESTREAM_ENDPOINT`. Missing bindings
fail before provisioning. Values stay strings. Alternatively, an `env` list
contains literal `KEY=value` strings; there is no shell expansion. Never commit
literal secrets. Quote version-like values such as `"3.10"`; YAML otherwise
interprets them as numbers. Unknown configuration fields fail validation.

Core additions to the prior scaffold are `schema-version: 1`,
`state-file: .colors.sqlite`, optional `oci-image-id`, `once-version: v0.3.3`,
SSH file paths, and Vault settings. Old `compute-api-version`, `provider-backend`
and infrastructure-state `r2-*` fields are not supported. Application storage
parameter references can continue to point to existing R2 services.

Resource names default to `<profile>-once-compute` and
`<profile>-once-firewall`. A profile is durable; an ONCE upgrade does not rename
it. A separate UUID establishes controller identity and ownership tags. Exact
resource IDs, provider scope and lifecycle are stored in SQLite. Names alone
never authorize adoption or deletion. Existing application/shared resource names
are preserved; application lifetime is independent of a VPS lifetime.

`compute-require-existing-state: true` rejects missing state. Use it for an
established deployment. A recorded instance disappearing is an error, not an
instruction to create a replacement. Instance resizing/replacement and boot
storage drift fail explicitly in v1. Initial image discovery selects a compatible
Canonical Ubuntu 24.04 image and pins its OCID in state; `oci-image-id` can pin it
before creation. ONCE v0.3.3 is checksum-pinned for amd64 and arm64. Docker comes
from the Ubuntu distribution package repository; its package version is not pinned.

## State, interruption and ownership

The local database may contain secrets and is plaintext. Database, journals,
keys and snapshots stay ignored and private; SQLite uses full synchronous
rollback journaling. Schema/profile/provider scope are checked on open. Completed
operation history is bounded. CLI output is independently allowlisted.

Cloud calls are not database transactions. A lost create response leaves a
pending operation. The next run discovers a uniquely tagged resource and records
it. If no resource is visible, it refuses to issue another create blindly.
Operators must establish the prior request's outcome before recovery. Deletion
similarly records intent and can resume after the resource has disappeared.

`adopt --instance-id OCID` recovers an existing, matching, already UUID-tagged
instance into state. It deliberately does not take over an arbitrary production
server, rewrite another manager's tags or migrate Terraform state. Full production
ownership transfer remains a separate operation.

One active operator per deployment is supported. A local advisory lock prevents
overlap on one filesystem; Vault snapshots do not coordinate different machines.
Stop the old operator before restoring on another machine and always reconcile
cloud reality before mutations. Do not use a shared network filesystem for state.

## Application delivery

SSH client and server Ed25519 keys are generated inside the deployment's `.ssh/`
directory. Cloud-init receives the server key through private OCI stdin metadata;
SSH pins that public key, including the first connection. Missing keys for an
existing instance require restoration; they are not silently regenerated.

Application environment and configuration updates are reconciled, including
changes to an existing hostname. Secrets travel to the host over SSH stdin; ONCE's
current CLI requires host-local `--env` arguments, visible to sufficiently
privileged processes on that trusted host. Raw commands/output are not logged.

`rolling` is suitable for disposable/stateless services. `stop-first` resolves an
immutable image, disables the old restart policy, requires a clean graceful stop,
then updates and verifies one replacement with the same named volumes. Automatic
ONCE image updates are disabled. A durable host pending marker blocks blind
retry after uncertainty; inspect the app/data before recovery. There is no
automatic rollback after a possible database migration. Host locks do not fence
another host. Removed owned apps retain their data volumes.

ONCE automatic backups and automatic updates remain unsupported. Managed DNS uses
Cloudflare; managed sending domains use Resend. The local verification fixture uses HTTP with an explicit Host
header; this is not public TLS validation. Clearing an application's final
environment binding fails because the pinned ONCE CLI cannot express that update.
Full adoption of `once-pocketcontext-v2` is not implemented or performed.

## DNS, SMTP and GitHub delivery

Use `devenv shell` for pinned `cf`, `resend`, `gh` and `s-nail` tools. Cloudflare
1.0.0-beta.14 and Resend 2.23.0 dependencies are locked in
`tools/cloud-clis/package-lock.json` and built with a fixed Nix dependency hash;
s-nail 14.9.25 uses a fixed source hash. Cloudflare CLI is beta.

```yaml
provider-dns: cloudflare
cloudflare-zone-id: YOUR_32_CHARACTER_ZONE_ID
provider-smtp: resend
smtp-domain: notifications.bigconfig.online
smtp-from: mail@notifications.bigconfig.online
resend-region: eu-west-1
once:
  applications:
    - host: www.bigconfig.online
      image: ghcr.io/pocketcontext/pocketcontext-website:latest
      github: pocketcontext/pocketcontext-website
      manage-dns: true
      auto_update: false
      deploy-strategy: rolling
      smtp: true
```

Supply `CLOUDFLARE_API_TOKEN` and `RESEND_API_KEY` through your trusted shell.
Cloudflare needs zone read and DNS edit on the selected existing zone. Resend
needs domain and API-key management. Use authenticated `gh` with repository
administrator access for environment provisioning. These management credentials
are not sent to GitHub Actions or the VPS. A domain-scoped sending key is stored
privately in SQLite and transmitted to the host via SSH stdin.

The first implementation supports one Cloudflare zone and one sending domain per
deployment. Existing unowned DNS records, Resend domains and GitHub environments
require explicit recovery/ownership resolution; matching names never authorize
adoption. Website records are DNS-only to allow origin TLS verification. Domain
verification can be pending after DNS changes: rerun convergence after propagation.
A lost Resend domain/key creation response requires operator reconciliation, not
blind retries. Keys are reused; rotation is a separate operation.

GitHub environment names equal the durable deployment `profile`, with one app per
GitHub repository per deployment. The controller installs a restricted separate
SSH key, main-only branch policy, `SSH_PRIVATE_KEY` environment secret and
`SERVER_IP`, `SERVER_USER`, `SSH_KNOWN_HOSTS`, `SITE_URL`,
`POCKETDEPLOY_PROFILE`, `POCKETDEPLOY_DEPLOYMENT_ID` variables. SSH keys remain in
ignored `.ssh/github-<repository-hash>` files. `init` prepares these local keys,
but Vault snapshots exclude them. Missing or incomplete GitHub key pairs are
recreated by `converge`; unchanged runs reuse them. To rotate deliberately, run
`pocketdeploy converge --rotate-github-keys`. Rotation installs the new public key
alongside the old one, updates the GitHub secret, then removes the old authority.
Interrupted rotations resume with the pending key. Queued jobs that already read
the old secret may need retrying after rotation. `plan` and `status` never rotate
keys. Provisioning an environment and its
settings is not atomic; a release during setup may fail until convergence completes.

The website workflow discovers all existing environments after publishing its
image. Every environment is an active target, without an opt-in flag. Missing
settings fail the target; no environments skips deployment. The current workflow
uses no-command SSH to update the configured image tag. The restricted handler
also supports `deploy ghcr.io/owner/repo@sha256:DIGEST`; other commands are rejected.
Retiring an environment requires accounting for queued/running workflow snapshots.

```sh
pocketdeploy smtp-test --to YOUR_TEST_ADDRESS
```

This explicit command sends one message from `smtp-from` using s-nail on the VPS,
Resend port 465 and certificate-verified TLS. Credentials travel over SSH stdin
and use a temporary private configuration file, cleaned afterward. Success means
SMTP acceptance, not inbox delivery. Convergence never sends a test message.
A static website does not acquire email functionality from SMTP provisioning;
its enquiry backend must consume email settings separately. Outbound SMTP does
not create a receiving mailbox.

`create` has been removed without an alias; use `converge` for initial provisioning
and subsequent updates.

## Vault recovery

```yaml
vault-id: YOUR_DEDICATED_VAULT_ID
vault-command: vaultcontext
# Optional known state document, also remembered locally:
# vault-state-document-id: DOCUMENT_ID
```

After `vaultcontext login` and user-operated `vaultcontext unlock --timeout 3600`:

```sh
pocketdeploy vault-save
pocketdeploy vault-restore \
  --document DOCUMENT_ID --version VERSION_ID --destination /absolute/path/to/recovery-check
```

Recovery files are `.envrc.private`, the SSH operator key pair, server key pair
and known hosts, plus a consistent SQLite snapshot. They must exist before a
complete recovery set can be saved. Create an empty private bindings file if the
deployment needs none. File versions upload first; the SQLite snapshot uploads
last and references their exact versions.

`colors.yml`, `.envrc` and package code come from Git. The snapshot records the
configuration content SHA256 and Git commit when available; the content hash
is authoritative, so unrelated Git commits do not prevent recovery. Preserve
uncommitted configuration separately or commit it before saving: a commit ID
alone cannot recover local edits. GitHub deployment keys are disposable and are
excluded from Vault; convergence recreates missing keys using the recovered
operator SSH authority and GitHub administrator access.

Before restoring, check out matching `colors.yml` in the destination directory.
Both the selected configuration and destination configuration must match the
checkpoint hash. Restore never replaces configuration, even with `--overwrite`.
Older snapshots containing configuration and GitHub keys remain readable: their
configuration is staged only to verify its hash, and their GitHub keys are skipped.
Existing Vault documents and historical versions are not deleted.
The snapshot does not need to contain its own newly assigned Vault version ID.
Vault's file limit is 8 MiB. Vault operations are explicit: `converge`,
`delete` and failed workflows never save automatically or require Vault access.
The retired `vault-save-after-run` field is rejected, including when false; remove
it from existing configuration and remove `COLORS_PAR_VAULT_SAVE_AFTER_RUN`
from the operator environment. Older Git configurations may contain this retired field; migrate it before
creating a new recovery checkpoint.

Run `vault-save` after successful mutations and after failures that changed local
state. Until that succeeds, Vault contains an older checkpoint and may lack new
resource identities or operation outcomes. Preserve local state and reconcile
cloud reality when recovering from an older snapshot. If saving fails, fix Vault
access and retry `vault-save`; do not rerun deployment just to retry its backup.

Restore stages and validates all files before publishing them, with the database
last. Existing destinations require `--overwrite`. A filesystem interruption
between file replacements requires repeating restore; multi-file publication is
not atomic. Restore never executes private bindings or starts deployments.
Review the Git configuration and run `plan` before taking over. If the
restored config contains machine-specific executable paths, update them locally.

Keep independent encrypted recovery material if VaultContext itself depends on
the infrastructure being recovered. Vault encrypts remote copies, not the local
working database. Passphrases are entered privately by the user, never retrieved
from configuration or supplied in command arguments.

## Development

```sh
devenv shell
uv sync --locked --extra test
uv run pytest -q
uv build
uv run python scripts/test-launcher.py
```

Tests use synthetic providers and state. They cover interrupted creates/deletes,
ownership mismatches, immutable drift, private snapshot recovery, secret-safe
errors, SSH host trust, stop-first delivery and idempotent convergence. Cloud
mutation is never part of the default suite. Live verification results are in
`docs/verification.md` when available; they are distinct from mocked coverage.

Publish and test package changes before updating the Git commit in the skill
launcher. Pin a full published commit, never a moving branch. The root symlink
keeps one launcher payload; commit the new pin after testing a copied launcher
outside this checkout. The launcher integration check uses synthetic missing-state
fixtures and never accesses a cloud account.

`--help` always prints ordinary help text, including with `--json`. A JSON
usage error has `command: null` when argument parsing cannot identify a command.

Running `./pocketdeploy` with no arguments prints the same help as `--help`
to stdout and exits 0, without loading configuration or contacting providers.
Unknown commands and a lone `--json` remain usage errors (exit 2).

### Verbose diagnostics

Use `pocketdeploy plan --verbose` or `pocketdeploy converge --verbose` to see
individual OCI and SSH requests start, finish, and report elapsed time on
stderr. Long-running requests periodically report that they are still waiting.
Labels are authored by PocketDeploy; arguments, credentials and raw provider
responses are never printed. `--json --verbose` keeps one JSON result on stdout.
`--verbose` and `--quiet` conflict and exit 2 before deployment work begins.

For `oci-auth: security_token`, cloud workflows check the selected profile's
local token expiry before invoking OCI. An expired token fails with a safe
renewal instruction instead of waiting for a provider request. This local check
does not prove a token is otherwise valid; OCI may still reject revoked or
invalid credentials. `init` and Vault workflows do not require OCI credentials.
