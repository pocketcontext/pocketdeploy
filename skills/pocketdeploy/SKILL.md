---
name: pocketdeploy
description: Configure and operate PocketDeploy OCI VPS deployments from colors.yml using the portable Python CLI, including plans, convergence, SSH, deletion and VaultContext recovery. Use for deployment operations, not application business data.
---

# PocketDeploy

Use the bundled `pocketdeploy` launcher, which runs an immutable package commit
through uv. Copy it into a deployment repository and make it executable, or run
it by absolute path. It discovers `colors.yml` from the current directory upward;
`-f /path/to/colors.yml` selects an explicit deployment. Its own installation
location does not select the deployment. Read
[configuration.md](references/configuration.md) before editing desired state.

Use the deployment's `devenv.nix` for Python, uv, OCI CLI, OpenSSH and the
VaultContext CLI. The portable launcher installs Python dependencies; it does
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
./pocketdeploy plan
./pocketdeploy status
./pocketdeploy converge
./pocketdeploy ssh
./pocketdeploy ssh --ssh-command 'uname -m'
./pocketdeploy delete --dry-run
```

`plan` and `create --dry-run` validate and read live OCI resources and, for an
existing host, application state through SSH. They can create a local lock;
they do not provision resources or generate keys. They require suitable tools,
authentication and application bindings. `status` and `describe` return the same
safe observed inventory without resolving application secrets. They are live
reads, not offline validation. A plan is not proof of completed convergence.

`create` and `converge` run the same DAG: keys → OCI → host → applications →
verification. Cloud/app mutations must stay within the user's authorized scope;
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
networking, DNS, SMTP and Vault data are not deleted.

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
key pairs, known hosts and a consistent SQLite snapshot. File versions upload
first; state uploads last with exact references. `.envrc` and code come from Git.
Vault encrypts remote copies, not local files. A backup failure can follow a
successful deployment: preserve local state and retry `vault-save` after fixing
Vault access; do not rerun infrastructure changes just to retry the backup.

Restore into an empty directory for verification. Existing destinations require
explicit `--overwrite`. Restore neither executes private files nor deploys.
Stop the old operator before handover, review non-secret desired state, and run
`./pocketdeploy plan -f /absolute/path/to/recovery-check/colors.yml` before
mutations, with the required bindings supplied by your trusted shell. An interrupted multi-file
restore must be repeated; publication is not atomic. Keep independent encrypted
recovery material if VaultContext depends on the infrastructure being recovered.
