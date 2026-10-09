# Configuration

PocketDeploy v1 accepts the flat Colors format and OCI only. Unknown fields are
rejected. Paths are relative to the selected `colors.yml` directory and must
stay inside it. Track desired configuration and `.envrc`; ignore credentials,
SQLite state and journals, `.ssh/`, snapshots and generated output.

## New deployment

Replace identifiers, hostname, image and operator CIDR before running a plan.
OCI IDs are identifiers, not credentials; review which metadata belongs in a
public repository. Use a fresh profile and state for a new deployment.

```yaml
schema-version: 1
profile: pocketdeploy-example
provider-compute: oci
state-file: .colors.sqlite
workdir: .colors
oci-config-file-profile: DEFAULT
oci-auth: security_token
oci-region: eu-frankfurt-1
oci-compartment-id: <compartment-ocid>
oci-subnet-id: <existing-public-subnet-ocid>
oci-availability-domain: <availability-domain>
oci-shape: VM.Standard.A1.Flex
oci-ocpus: 1
oci-memory-in-gbs: 6
oci-boot-volume-size-in-gbs: 50
compute-prevent-destroy: true
compute-require-existing-state: false
compute-retain-boot-volume: true
compute-ssh-sources: [203.0.113.10/32]
compute-http-sources: [0.0.0.0/0]
provider-dns: no-infra
provider-smtp: no-infra
once-version: v0.3.3
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

`profile` is a lowercase identifier of at most 40 characters, starting with a
letter. Default resource names are `<profile>-once-compute` and
`<profile>-once-firewall`. Keep the profile stable across package/ONCE upgrades.
Exact resource IDs and the independent deployment UUID live in SQLite. Provider
scope includes the OCI CLI profile, region, compartment and subnet; opening state
with a different scope fails.

The subnet/VCN, routing, internet gateway and subnet security lists must already
exist. The controller owns a dedicated NSG: SSH sources admit TCP 22, HTTP sources
admit TCP 80/443. Sources are IPv4 CIDR lists. Existing subnet rules can allow
additional access; an NSG cannot remove those permissions.

`oci-image-id` optionally pins the initial image. Otherwise creation chooses a
compatible Canonical Ubuntu 24.04 image and records its OCID. ONCE is
checksum-pinned to v0.3.3; changing `once-version` is unsupported. Docker uses
the Ubuntu distribution package. Optional `oci-boot-volume-vpus-per-gb` defaults
to 10. Resizing/replacing existing compute and boot storage is unsupported.

## Parameters and application settings

Known root fields accept `COLORS_PAR_*` overrides: for example,
`COLORS_PAR_OCI_OCPUS` overrides `oci-ocpus`. Application `env` maps container
variable names to flat parameter references. In the example,
`LITESTREAM_ENDPOINT` resolves from
`COLORS_PAR_APP_WIKICONTEXT_PRODUCTION_LITESTREAM_ENDPOINT`. Missing bindings fail
before provisioning. Values remain strings; do not commit the resolved values.

Alternatively, `env` can be a list of literal strings:

```yaml
env:
  - LOG_LEVEL=info
  - FEATURE_ENABLED=true
```

List entries split on the first `=`. There is no shell expansion; `$NAME` stays
literal. Do not use this syntax for committed secrets. Quote YAML version-like
strings such as `"3.10"`.

Each application needs a unique `host` and `image`. Prefer immutable image
digests. `deploy-strategy` defaults to `rolling`; use `stop-first` for stateful
services, with `deploy-stop-timeout` from 1–3600 seconds (default 300).
`health-path` defaults to `/`. `disable_tls: true` opts into HTTP; otherwise
prepare working public DNS/TLS. Optional `cpus` and `memory` are nonnegative
integers passed to ONCE. Automatic updates/backups must remain disabled.
Managed DNS/SMTP and GitHub delivery are described below. Application
storage bindings may reference existing R2 services, but Terraform-style state
backend settings (`provider-backend`, infrastructure `r2-*`,
`compute-api-version`) are not supported.

## Local authority and recovery

Defaults are `.ssh/id_ed25519` and `.ssh/id_ed25519.pub` for the client,
`.ssh/host_ed25519` and `.ssh/host_ed25519.pub` for the server, and
`.ssh/known_hosts` for server trust. The corresponding settings are
`ssh-private-key-file`, `ssh-public-key-file`, `ssh-host-private-key-file`,
`ssh-host-public-key-file` and `ssh-known-hosts-file`. `ssh-user` defaults to
`ubuntu`. Missing authority for an existing instance requires restoration;
do not generate replacement keys to bypass that check.

```yaml
vault-id: YOUR_DEDICATED_VAULT_ID
vault-command: vaultcontext
# Optional known state document, also remembered in SQLite:
# vault-state-document-id: DOCUMENT_ID
```

Run `init` to prepare local state, UUID, SSH keys, known hosts and an empty
`.envrc.private` if absent, without cloud or Vault calls. Saving requires all of
these recovery files. VaultContext must be authenticated and user-unlocked.
For a new deployment use `init → vault-save → converge → vault-save`; for updates
use `plan → converge → vault-save`.

Vault access happens only through explicit Vault commands. The retired
`vault-save-after-run` option is rejected even when false. No successful or failed
deployment automatically saves state. Save after mutations and after failures
that changed local state; until then, the last remote checkpoint may be stale.
Keep local state and reconcile cloud observations before recovery from an older
snapshot. The state document and version identify the exact saved checkpoint.
`colors.yml` and `.envrc` come from Git; Vault excludes configuration and GitHub
keys. Snapshots record configuration SHA256 and Git commit when available.
Restore requires matching selected and destination `colors.yml`; it never
replaces configuration. Legacy snapshots validate their staged configuration and
skip GitHub keys. Vault's file limit is 8 MiB. Local plaintext state remains private even though
its remote snapshot is encrypted.

## Output options

`--json`, `--quiet` and `--verbose` are command-line options, not `colors.yml` settings.
Text is the default. `--json` returns a versioned envelope with `schema_version`,
`command`, `ok` and either `result` or `error`; safe inventory fields are under
`result`. Progress and timings use stderr, and `--quiet` suppresses them.
Failure envelopes on stdout retain a nonzero exit status. SSH passes remote
streams and exit status through without a footer and does not accept `--json`.

`--verbose` adds safe per-request timings and periodic still-waiting messages on
stderr, including with `--json`. It never prints raw arguments or responses.
Combining `--verbose` with `--quiet` is a usage error (exit 2).

For `oci-auth: security_token`, cloud workflows check local token expiry before
starting OCI requests and fail with renewal guidance when it has expired.
This check does not authenticate the token or detect all revoked credentials.
Local `init` and explicit Vault workflows do not perform this OCI check.

The OCI CLI configuration comes from `OCI_CLI_CONFIG_FILE`, or `~/.oci/config`
when unset. `oci-config-file-profile` selects the profile (default `DEFAULT`).
An expired token returns `oci_token_expired`; refresh with
`oci session refresh --profile PROFILE --region REGION` using the same config
file. If refresh fails, use
`oci session authenticate --profile-name PROFILE --region REGION`. Guidance
resolves the region from `oci-region`, then `OCI_CLI_REGION`, then the selected
OCI profile. If none is known, it explicitly asks you to supply `<OCI_REGION>`.

## Managed services

Select `provider-dns: cloudflare` with `cloudflare-zone-id` (32 lowercase hex
characters). Select `provider-smtp: resend` with `smtp-domain`, full `smtp-from`
on that domain, and optional `resend-region` (default `eu-west-1`). Resend requires
managed Cloudflare DNS; one zone/domain per deployment is supported. Supply
`CLOUDFLARE_API_TOKEN` and `RESEND_API_KEY` through the trusted shell; keep values
outside colors.yml. Required management scopes: zone read/DNS edit and Resend
domain/API-key management. `gh` requires repository administrator access.

Applications opt into `manage-dns: true`, `smtp: true` and
`github: owner/repository`. GitHub images must belong to that repository on GHCR;
one app per repository is supported. The environment equals `profile`, and an
explicit `github-environment` may only repeat that exact profile. TLS must remain
enabled for GitHub and managed website DNS. The profile is durable, not a tool
version or a global fixed prefix.

Example sending identity: `smtp-domain: notifications.bigconfig.online` and
`smtp-from: mail@notifications.bigconfig.online`. Existing resources are not
adopted by name. SQLite stores provider IDs and sending credentials; `.ssh/`
stores operator, host and per-repository deployment keys. GitHub deployment keys
are excluded from Vault and recreated during convergence when missing. Use
`converge --rotate-github-keys` for explicit rotation; normal runs reuse keys.
`plan` and `status` never rotate them.

`converge` verifies DNS/SMTP, applications and HTTPS before publishing CI access.
Use explicit `smtp-test --to ADDRESS` for a single authorized test email. Deletion
retains SMTP domains, keys and email DNS while removing owned website records and
GitHub environments. Retire queued CI runs before deleting environments.
