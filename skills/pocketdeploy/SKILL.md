---
name: pocketdeploy
description: Configure and operate PocketDeploy OCI, DigitalOcean and Google Cloud VPS deployments from colors.yml using the portable Python CLI, including plans, convergence, SSH, deletion and VaultContext recovery. Use for deployment operations, not application business data.
---

# PocketDeploy

Use the bundled `pocketdeploy` launcher, which runs an immutable package commit
through uv. Copy it into a deployment repository and make it executable, or run
it by absolute path. It uses `colors.yml` only in the current working directory;
`-f /path/to/colors.yml` selects an explicit deployment. Its own installation
location does not select the deployment. Read
[configuration.md](references/configuration.md) before editing desired state.

Use the deployment's `devenv.nix` for Python, uv, OCI CLI, Google Cloud CLI, OpenSSH and the
VaultContext CLI plus pinned Cloudflare and Resend tools. s-nail runs on the deployment
host and is installed during host setup when a managed application enables SMTP;
it is not a local dependency. The portable launcher installs Python dependencies; it does
not install external tools or authenticate to providers. Working-tree package
development uses `uv run pocketdeploy` from the package checkout.

Keep `.envrc` and non-secret `colors.yml` tracked at the repository root.
Keep `.envrc.private`, `.colors.sqlite` and its journals/lock, `.ssh/`, snapshots
and generated output ignored alongside them. SQLite can contain resolved
secrets and is plaintext locally. Do not print or inspect private bindings,
keys, raw cloud responses, Docker metadata or ONCE labels. Use the CLI's safe
inventory output. Ask for missing parameter names or whether they are set,
never their values. The CLI does not source private files; a trusted shell or
reviewed `.envrc` supplies environment variables.

## Operating a deployment

Preserve the existing profile, resource identities and user changes. For a new
deployment, start with the configuration reference's example and fresh state;
do not copy the package repository's live test identity. A profile is durable;
resource names are descriptive, while recorded IDs and deployment UUID tags
establish ownership. Existing networks/subnets remain externally managed.
For DigitalOcean or Google Cloud, read [providers.md](references/providers.md).
Provider changes require fresh deployment state; they do not migrate applications.
The new backends are source features pending a new portable launcher release.
Google Cloud and DigitalOcean have disposable live verification receipts.
Use `uv run pocketdeploy` from the source checkout for them.

```sh
# New deployment with Vault recovery configured
./pocketdeploy init
./pocketdeploy vault-save
./pocketdeploy converge
./pocketdeploy vault-save

# Routine update
./pocketdeploy plan
./pocketdeploy status
./pocketdeploy converge
./pocketdeploy vault-save
./pocketdeploy ssh
./pocketdeploy ssh --ssh-command 'uname -m'
./pocketdeploy delete --dry-run
```

`init` prepares a local deployment UUID, SQLite state, SSH client and host keys,
known hosts and an empty `.envrc.private` if absent. It makes no cloud or Vault
calls, does not resolve application bindings and preserves existing files. A
healthy repeat is idempotent; missing authority for an existing instance still
requires recovery. Save this initial authority before provisioning, then save again after
creation to checkpoint the resource identities.

`plan` and `converge --dry-run` validate and read live compute-provider resources and, for an
existing host, application state through SSH. They can create a local lock;
they do not provision resources or generate keys. They require suitable tools,
authentication and application bindings. `status` returns
safe observed inventory without resolving application secrets. They are live
reads, not offline validation. `state.last_vault_backup` is the last locally
acknowledged Vault document/version/save time, or null; older receipts may lack a
time. It makes no Vault call and does not establish freshness. A restored
snapshot may contain a prior receipt. A plan is not proof of completed convergence.

`converge` runs the DAG: preflight → keys → compute → host → service DNS →
SMTP verification → SMTP credentials → applications → HTTPS verification → GitHub.
SMTP stages are skipped when Resend is disabled. `create` is removed; there is no compatibility alias. Cloud/app mutations must stay within the user's authorized scope;
existing authorization applies without repeated permission requests. Expired
provider authentication requires renewal through the configured CLI identity,
not a different account or operator credentials. Report the failing stage and
safe diagnostic; do not expose suppressed provider output to troubleshoot.

Set `compute-require-existing-state: true` for an established deployment. Missing
state or keys must be restored. A missing recorded instance is not permission
to create another. Uncertain create outcomes require reconciliation of the
recorded operation and tagged resources before retrying; do not clear state to
bypass protection. `adopt --instance-id PROVIDER_ID` requires existing local state with
the matching deployment UUID and records the owned instance, firewall and boot
volume. Restore state first; adoption cannot reconstruct a lost identity and
does not transfer production ownership from another
manager. Instance replacement/resizing and storage drift are unsupported in v1.

Before ordinary SSH operations, the controller requires a verified replacement
for the host key distributed in provider metadata/user data. Convergence performs the rotation.
For an older deployment needing inspection or retirement without application
changes, run `pocketdeploy rotate-host-key`, then `pocketdeploy vault-save`.
Rotation changes SSH trust only and resumes with the same pending key after an
interruption. The replacement key and checkpoint live in private SQLite state;
a pre-rotation backup alone cannot recover them. Keep local state until the new
checkpoint is saved. Initial bootstrap still relies on the metadata key and is
not independently authenticated against an attacker who can both read metadata
and intercept first contact. Full convergence also installs persistent Docker
forwarding rules to block container access to IMDS; host-network/root processes
remain outside that restriction.

For SQLite/stateful apps, use `stop-first` and an adequate stop timeout. A durable
host pending marker blocks blind retries after uncertain delivery. Inspect the
application and data within the authorized recovery scope before intervention;
never bypass the marker or restart old code against a potentially migrated DB.
No automatic rollback or cross-host fencing is provided. Use one active operator
per deployment; the local lock does not coordinate different machines.

Deletion requires the intended deployment scope and deliberate removal of
`compute-prevent-destroy` protection. Review `delete --dry-run`, then run `delete`
within that authorization. Deletion destroys every recorded deployment-owned
boot volume, including previously retained disks after ownership verification.
It removes owned website/email DNS, the Resend sending key and domain, GitHub
environments and generated SSH keys. External networking, management credentials,
Git configuration, private bindings, SQLite receipts and Vault history remain.

## Output and automation

Default output is readable text on stdout, with progress and stage durations on
stderr. For machine parsing use `--json`, which emits one envelope containing
`schema_version: 1`, `command`, `ok`, and `result` on success or `error` on
failure. Read inventory fields under `result`, for example
`./pocketdeploy status --json | jq '.result.state.last_vault_backup'`.
Errors carry a safe `code`, `message` and failing `stage` when known. Check the
exit status as well as `ok`; do not parse human summaries or progress messages.

Use `--verbose` to diagnose slow plans or convergence: individual OCI and SSH
requests report timings and periodic still-waiting messages on stderr. It never
prints raw command arguments or provider output. `--json --verbose` preserves the
single JSON result on stdout. `--verbose` conflicts with `--quiet` (exit 2 before
work); neither option belongs in `colors.yml`.

With security-token authentication, cloud workflows check local token expiry
before invoking OCI. Follow the safe renewal instruction for expired tokens;
never print the token or substitute another identity. Local expiry validation
cannot detect every invalid or revoked credential. `init` and Vault workflows
remain independent of OCI authentication.

`--quiet` suppresses progress, not the result or text error diagnostics. Use
`./pocketdeploy plan --json --quiet` for a JSON result without controller progress;
launcher/runtime diagnostics may still appear on stderr. Exit codes are 0 for
success, 1 for operation failure, 2 for invalid usage and 130 for interruption.
Output does not switch format when redirected. `ssh` is the exception: it
passes through remote streams and exit status, appends nothing, and rejects
`--json`.

## Vault recovery

Use the installed VaultContext skill when authentication/unlock help is needed.
Only the user enters their passphrase interactively. Never retrieve it or pass
it in arguments. Reuse an existing unlocked session.

```sh
./pocketdeploy vault-save
./pocketdeploy vault-restore --document DOCUMENT_ID --version VERSION_ID \
  --destination /absolute/path/to/recovery-check
```

The recovery set includes `.envrc.private`, operator and host SSH key pairs,
known hosts and a consistent SQLite snapshot. File versions upload first; state
uploads last with exact references. `colors.yml`, `.envrc` and code come from Git.
The snapshot records the configuration content SHA256 and Git commit when
available. Commit desired configuration before saving or preserve local edits
separately; content hashes must match during recovery.
GitHub deployment keys are excluded. `converge` recreates missing pairs; use
`converge --rotate-github-keys` for deliberate rotation. Old host authority stays
until the new GitHub secret is accepted, then it is removed. Interrupted rotations
resume; queued jobs holding an old secret may need retrying. Read-only commands
never rotate keys.
Vault encrypts remote copies, not local files. Only explicit `vault-save` uploads
a checkpoint; provisioning, convergence, deletion and failed workflows never
back up automatically. Remove the retired `vault-save-after-run` field, even if
false. Save after mutations and failures that changed local state while a complete
recovery set exists. Successful deletion removes SSH keys: run `init` to generate
new local authority or recreate with `converge` before saving a new complete
checkpoint. Preserve the local deletion receipt until then. Until saving
succeeds, Vault may lack new resource identities and operation outcomes. Preserve
local state and reconcile cloud reality when recovering an older checkpoint.
Retry a failed save after fixing Vault access; do not rerun infrastructure changes
just to retry the backup.

Check out matching `colors.yml` into the recovery destination before restoring.
Both selected and destination configuration must match the checkpoint hash.
Configuration is never overwritten. Existing private destinations require
explicit `--overwrite`. Restore neither executes private files nor deploys.
Stop the old operator before handover, review non-secret desired state, and run
`./pocketdeploy plan -f /absolute/path/to/recovery-check/colors.yml` before
mutations, with the required bindings supplied by your trusted shell. An interrupted multi-file
restore must be repeated; publication is not atomic. Keep independent encrypted
recovery material if VaultContext depends on the infrastructure being recovered.

Older snapshots remain readable: their configuration is staged for hash
verification only, and their GitHub keys are skipped. Historical Vault documents
are retained. Retired configuration fields must be migrated before creating a
new recovery checkpoint.

`--help` always prints ordinary help text, including with `--json`. A JSON
usage error has `command: null` when argument parsing cannot identify a command.

Running `./pocketdeploy` with no arguments prints the same help as `--help`
to stdout and exits 0, without loading configuration or contacting providers.
Unknown commands and a lone `--json` remain usage errors (exit 2).

## Managed services

Read the managed-services configuration reference before Cloudflare, Resend or
GitHub operations. Use only the supplied management credentials; never copy them
into CI. Missing or conflicting ownership requires reconciliation. SMTP verification
is a separate DAG stage. For a domain that is not yet verified, it triggers
verification once and polls the recorded domain every ten seconds, then
automatically continues within the same convergence. Already verified domains
skip the trigger and polling.
Compute, host setup and DNS reconciliation are not repeated while waiting. SMTP
credentials and applications wait for verification.

Resend and Cloudflare allowlisted reads retry temporary connection failures,
timeouts, HTTP 429 throttling and HTTP 500/502/503/504 within the same DAG stage.
Use `--provider-read-timeout SECONDS` with `plan`, `converge` or `delete`, including dry runs (integer 0–3600, default 120).
The budget is per read and includes provider CLI retries, subprocess time and
backoff; at most five subprocess attempts are made. `0` disables PocketDeploy
retries, retaining normal request timeouts and provider CLI retry behavior.
Backoff uses full jitter with ceilings of 1, 2, 4 and 8 seconds; honor
available `Retry-After` without retrying early if it exceeds the remaining budget.
The pinned Cloudflare CLI omits error headers, so Cloudflare uses backoff without
`Retry-After`; Resend exposes it in structured errors. SMTP
reads also respect the verification deadline. Authentication, permissions,
certificate errors and exhausted usage quotas fail immediately. Mutations are
never blindly replayed. Recovery does not restart upstream DAG stages or change
OCI/GitHub retry behavior; unresolved mutation outcomes require reconciliation.

`converge --smtp-verification-timeout SECONDS` sets the verification wait budget
(integer seconds from 0 to 3600, default 600). `0` makes one immediate check
without polling. The option requires `converge` without `--dry-run` and belongs
on the command line, not in `colors.yml`. Stage start and completion, including
elapsed duration, go to stderr unless `--quiet` is set. Use `--verbose` for
periodic waiting updates. Authentication or domain identity
errors stop immediately. Timeout reports `smtp_verification_pending`; timeout and
interruption preserve resources and recovery state. Resolve the cause before
resuming convergence against the existing resources. Uncertain credential
creation requires operator recovery without blind retry.

GitHub environments default to the exact deployment profile. All repository
environments are active CD targets, so retire stale environments before enabling
discovery. GitHub setup is not atomic; avoid releasing while configuring it.
Use `smtp-test --to ADDRESS` only for an explicitly requested email test and a
known recipient. Report SMTP acceptance separately from delivery. Never send a
message as part of ordinary convergence. No SMTP inbox is provisioned.

## Deployment retirement

Review `./pocketdeploy delete --dry-run` before an authorized deletion. Set
`compute-prevent-destroy: false` explicitly. There is no boot-volume retention
option. The deletion DAG preflights compute, GitHub, DNS, SMTP and host ownership
and readiness before writes. It retires GitHub environments, fences queued CI,
and gracefully stops managed apps. The `delete-services` stage revokes the
sending key, deletes its domain and removes all owned DNS records. Compute
cleanup destroys the instance, owned boot volumes and firewall. Finally, local
operator, server and GitHub keys and known hosts are removed. Any failed remote
cleanup preserves recovery authority. Read-only preflight cannot prove all future
writes will be authorized.

External networking, management credentials, `.envrc.private`, Git configuration,
SQLite deletion receipts and Vault history remain. Do not bypass unclean-stop or
ownership failures. Inspect the failing stage and rerun `delete` to reconcile and
resume. Recreation starts with fresh keys and application storage. No automatic
backup or restoration is performed. Successful output lists removed and retained
resources.
