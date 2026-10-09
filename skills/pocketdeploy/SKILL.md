---
name: pocketdeploy
description: Configure and operate PocketDeploy OCI VPS deployments from colors.yml using the portable Python CLI, including plans, convergence, SSH, deletion and VaultContext recovery. Use for deployment operations, not application business data.
---

# PocketDeploy

Use the bundled `pocketdeploy` launcher, which runs an immutable package commit
through uv. Copy it into a deployment repository and make it executable, or run
it by absolute path. It uses `colors.yml` only in the current working directory;
`-f /path/to/colors.yml` selects an explicit deployment. Its own installation
location does not select the deployment. Read
[configuration.md](references/configuration.md) before editing desired state.

Use the deployment's `devenv.nix` for Python, uv, OCI CLI, OpenSSH and the
VaultContext CLI plus pinned Cloudflare, Resend and s-nail tools. The portable launcher installs Python dependencies; it does
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
establish ownership. Existing subnet/VCN resources remain externally managed.

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

`plan` and `converge --dry-run` validate and read live OCI resources and, for an
existing host, application state through SSH. They can create a local lock;
they do not provision resources or generate keys. They require suitable tools,
authentication and application bindings. `status` returns
safe observed inventory without resolving application secrets. They are live
reads, not offline validation. `state.last_vault_backup` is the last locally
acknowledged Vault document/version/save time, or null; older receipts may lack a
time. It makes no Vault call and does not establish freshness. A restored
snapshot may contain a prior receipt. A plan is not proof of completed convergence.

`converge` runs the DAG: preflight → keys → OCI → host → DNS/SMTP → applications →
HTTPS verification → GitHub. `create` is removed; there is no compatibility alias. Cloud/app mutations must stay within the user's authorized scope;
existing authorization applies without repeated permission requests. Expired
provider authentication requires renewal through the configured CLI identity,
not a different account or operator credentials. Report the failing stage and
safe diagnostic; do not expose suppressed provider output to troubleshoot.

Set `compute-require-existing-state: true` for an established deployment. Missing
state or keys must be restored. A missing recorded instance is not permission
to create another. Uncertain create outcomes require reconciliation of the
recorded operation and tagged resources before retrying; do not clear state to
bypass protection. `adopt --instance-id OCID` only recovers an already matching
UUID-tagged instance; it does not transfer production ownership from another
manager. Instance replacement/resizing and storage drift are unsupported in v1.

For SQLite/stateful apps, use `stop-first` and an adequate stop timeout. A durable
host pending marker blocks blind retries after uncertain delivery. Inspect the
application and data within the authorized recovery scope before intervention;
never bypass the marker or restart old code against a potentially migrated DB.
No automatic rollback or cross-host fencing is provided. Use one active operator
per deployment; the local lock does not coordinate different machines.

Deletion requires the intended deployment scope and deliberate removal of
`compute-prevent-destroy` protection. Review `delete --dry-run`, then run `delete`
within that authorization. Default boot-volume retention preserves recovery
material; disabling retention destroys the owned boot disk. Externally managed
networking and Vault data are not deleted. Owned website DNS and GitHub environments
are deleted; SMTP domain, sending key and email DNS are retained.

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

The recovery set includes `colors.yml`, `.envrc.private`, client and host SSH
key pairs, per-repository GitHub SSH key pairs, known hosts and a consistent SQLite snapshot. File versions upload
first; state uploads last with exact references. `.envrc` and code come from Git.
Vault encrypts remote copies, not local files. Only explicit `vault-save` uploads
a checkpoint; provisioning, convergence, deletion and failed workflows never
back up automatically. Remove the retired `vault-save-after-run` field, even if
false. Save after mutations and failures that changed local state. Until saving
succeeds, Vault may lack new resource identities and operation outcomes. Preserve
local state and reconcile cloud reality when recovering an older checkpoint.
Retry a failed save after fixing Vault access; do not rerun infrastructure changes
just to retry the backup.

Restore into an empty directory for verification. Existing destinations require
explicit `--overwrite`. Restore neither executes private files nor deploys.
Stop the old operator before handover, review non-secret desired state, and run
`./pocketdeploy plan -f /absolute/path/to/recovery-check/colors.yml` before
mutations, with the required bindings supplied by your trusted shell. An interrupted multi-file
restore must be repeated; publication is not atomic. Keep independent encrypted
recovery material if VaultContext depends on the infrastructure being recovered.

Older recovery checkpoints may restore `vault-save-after-run`; remove that
retired configuration field and its environment override before using them.

`--help` always prints ordinary help text, including with `--json`. A JSON
usage error has `command: null` when argument parsing cannot identify a command.

Running `./pocketdeploy` with no arguments prints the same help as `--help`
to stdout and exits 0, without loading configuration or contacting providers.
Unknown commands and a lone `--json` remain usage errors (exit 2).

## Managed services

Read the managed-services configuration reference before Cloudflare, Resend or
GitHub operations. Use only the supplied management credentials; never copy them
into CI. Missing or conflicting ownership requires reconciliation. SMTP verification
may need a later convergence after DNS propagation; uncertain credential creation
requires operator recovery without blind retry.

GitHub environments default to the exact deployment profile. All repository
environments are active CD targets, so retire stale environments before enabling
discovery. GitHub setup is not atomic; avoid releasing while configuring it.
Use `smtp-test --to ADDRESS` only for an explicitly requested email test and a
known recipient. Report SMTP acceptance separately from delivery. Never send a
message as part of ordinary convergence. No SMTP inbox is provisioned.
