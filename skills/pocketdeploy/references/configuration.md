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
vault-save-after-run: false
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
integers passed to ONCE. Automatic updates/backups and managed DNS/SMTP must
remain disabled. GitHub deploy-key publication is unsupported. Application
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
vault-save-after-run: true
# Optional known state document, also remembered in SQLite:
# vault-state-document-id: DOCUMENT_ID
```

Saving requires all recovery files, including `.envrc.private`; create an empty
private file when no bindings are needed. VaultContext must be authenticated and
user-unlocked. Configured convergence saves prepared authority/state before cloud
mutation and saves again after completion; failed runs also attempt a snapshot.
The recovery set's state document and version identify the exact checkpoint.
Vault's file limit is 8 MiB. Local plaintext state remains private even though
its remote snapshot is encrypted.
