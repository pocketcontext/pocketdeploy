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

Commands search for the nearest `colors.yml` from the caller's working directory
upward. `-f /path/to/colors.yml` explicitly selects a deployment. State and key
paths resolve relative to that configuration, never relative to the launcher.
The launcher does not load `.envrc.private`; use direnv or your trusted shell.
See [the skill](skills/pocketdeploy/SKILL.md) for operator instructions.

## Commands

```sh
pocketdeploy init
pocketdeploy vault-save
pocketdeploy create
pocketdeploy vault-save

# Routine update
pocketdeploy plan
pocketdeploy converge
pocketdeploy vault-save
pocketdeploy status
pocketdeploy describe
pocketdeploy ssh
pocketdeploy ssh --ssh-command 'uname -m'
pocketdeploy delete --dry-run
```

`init` prepares the local deployment UUID, SQLite state, SSH client and host
keys, known hosts and an empty `.envrc.private` if absent. It makes no cloud or
Vault calls, does not resolve application bindings and preserves existing files.
Repeating a healthy initialization is safe; missing authority for an existing
instance still requires recovery. Use `init → vault-save → create → vault-save` for a new deployment
when encrypted recovery is configured. The first snapshot preserves authority
before provisioning; the second records the resulting resource identities.

`create` and `converge` share a DAG: keys → OCI → host → applications → verify.
Each external mutation records intent first and the verified result afterward.
An unchanged converge makes no OCI mutations and no application replacement.
Host setup still verifies/enables services, and mutable image tags are pulled to
check whether their immutable digest changed. Prefer digest-pinned app images
for reproducibility. `plan` never creates cloud resources or keys; mutable image
tags may need verification at converge time. Local lock files may be created.

`status` and `describe` currently return the same safe inventory, operation and
host application status. They never print resolved environments, credentials,
raw Docker metadata, ONCE labels or cloud response bodies.
`state.last_vault_backup` reports the locally acknowledged document/version and
UTC `saved_at` (or null when no receipt is known). Older receipts may lack a
timestamp. This makes no Vault call and does not prove checkpoint freshness.
`ssh` is explicitly
interactive; commands you choose can print private information in your terminal.

`compute-prevent-destroy: true` is the default. To delete, deliberately set it to
false, review `delete --dry-run`, then run `delete`. Only recorded resources with
matching deployment UUID tags may be removed. `compute-retain-boot-volume: true`
is the default; retained disks remain recorded. Application data and recovery
files are not automatically purged. A disposable test may explicitly set false.
Deletion does not remove externally managed networking, DNS, SMTP or Vault data.

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

V1 rejects GitHub deployment-key publication, managed DNS/SMTP, ONCE automatic
backups and automatic updates. DNS/TLS configuration remains the operator's
responsibility. The local verification fixture uses HTTP with an explicit Host
header; this is not public TLS validation. Clearing an application's final
environment binding fails because the pinned ONCE CLI cannot express that update.
Full adoption of `once-pocketcontext-v2` is not implemented or performed.

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

Recovery files are the exact config used (restored as `colors.yml`),
`.envrc.private`, the SSH client key pair, server key pair and known hosts. They
must exist before a complete recovery set can be saved. Create an empty private
bindings file if the deployment needs none. File versions upload first; the
consistent SQLite snapshot uploads last and references the exact versions.
`colors.yml` is also included in Vault to pair the desired configuration with
its recovery checkpoint. `.envrc` and package code are recovered from Git.
The snapshot does not need to contain its own newly assigned Vault version ID.
Vault's file limit is 8 MiB. Vault operations are explicit: `create`, `converge`,
`delete` and failed workflows never save automatically or require Vault access.
The retired `vault-save-after-run` field is rejected, including when false; remove
it from existing configuration and remove `COLORS_PAR_VAULT_SAVE_AFTER_RUN`
from the operator environment. Older Vault snapshots may restore this retired
field; remove it from restored `colors.yml` before continuing.

Run `vault-save` after successful mutations and after failures that changed local
state. Until that succeeds, Vault contains an older checkpoint and may lack new
resource identities or operation outcomes. Preserve local state and reconcile
cloud reality when recovering from an older snapshot. If saving fails, fix Vault
access and retry `vault-save`; do not rerun deployment just to retry its backup.

Restore stages and validates all files before publishing them, with the database
last. Existing destinations require `--overwrite`. A filesystem interruption
between file replacements requires repeating restore; multi-file publication is
not atomic. Restore never executes private bindings or starts deployments.
Review the restored configuration and run `plan` before taking over. If the
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
