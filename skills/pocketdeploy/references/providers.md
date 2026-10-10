# Compute providers

Select `provider-compute: oci`, `digitalocean` or `gcp`. Each deployment has one
provider and one server. Local state and Vault checkpoints bind provider scope;
changing providers requires a new deployment, separate application-data restore
and DNS cutover. Existing OCI scope and configuration defaults are preserved.
Do not copy the root checkout's retired test identity into a new deployment.

DigitalOcean and Google Cloud have recorded live disposable deployment checks
(see the repository's `docs/verification.md`) and are included in the bundled
portable launcher. Older copied launchers require an explicit pin update.
Application mail delivery and populated data
recovery still require separate verification; both provider checks verify
SMTP TLS connectivity without sending.

## DigitalOcean

Start from `examples/colors-digitalocean.yml` in the package repository. Required
fields are `digitalocean-account-id` (the account UUID returned by `/v2/account`),
`digitalocean-region` and `digitalocean-vpc-id`. The VPC must already exist in the
chosen region. Defaults are `digitalocean-size: s-1vcpu-2gb` and
`digitalocean-image: ubuntu-24-04-x64`. The image selector resolves to a numeric
image identity saved in state. The boot disk belongs to the Droplet; separate
volumes, backups and snapshots are not managed by this backend.

Supply a DigitalOcean API token through `COLORS_PAR_DIGITALOCEAN_ACCESS_TOKEN`, or name
another environment variable with `digitalocean-token-env`. Store the token in
the trusted shell/private bindings, never in colors.yml. The backend uses HTTPS
directly and does not require doctl. The token needs account/VPC/image/size reads,
Droplet and Cloud Firewall lifecycle access, and deployment-tag management.
No API token is sent to the server, ONCE or GitHub.

The deployment UUID tag is created and recorded before its firewall and removed
after remote cleanup. Ownership uses that tag on the Droplet and recorded resource
IDs. A Cloud Firewall has no arbitrary ownership labels: its UUID-derived name,
exact deployment-tag selector and recorded firewall ID bind it to the deployment.
Extra targets, duplicate ownership claims and unrelated attached firewalls block
unsafe reconciliation. The controller creates a locked-password sudo SSH user
through cloud-init and uses the shared bootstrap host-key rotation.

Creation uncertainty is recorded before requests and reconciled against matching
cloud identities. If no matching result is visible, the controller does not issue
a second create blindly. Deletion waits for recorded resource disappearance;
uncertain deletion does not authorize replay or removal of local SSH authority.
Externally attached volumes or retained snapshots/backups require separate handling.

DigitalOcean documents default blocks on SMTP ports 25, 465 and 587. New examples
choose Resend port 2587 with STARTTLS. Verify connectivity on the intended Droplet;
the absence of 2587 from the documented block list is not a connectivity guarantee.

## Google Cloud

Start from `examples/colors-gcp.yml`. Required fields are `gcp-project`, `gcp-zone`,
`gcp-network` and `gcp-subnet`. Networks/subnets must already exist in that project;
the subnet must be in the zone's region. Compute Engine API enablement, billing,
quotas, organization policies and routes remain externally managed. This version
does not provision Shared VPC infrastructure.

Defaults are `gcp-machine-type: e2-small`, `gcp-image-project: ubuntu-os-cloud`,
`gcp-image-family: ubuntu-2404-lts-amd64`, `gcp-boot-disk-size-gb: 50` and
`gcp-boot-disk-type: pd-balanced`. Family discovery pins the exact image in state.
Alternatively set `gcp-image` to an exact image selfLink. Network/subnet names or
full same-project Compute selfLinks are accepted. Machine and disk type fields
are names. This initial configuration targets AMD64 Ubuntu 24.04.

Authenticate the Google Cloud CLI separately. The backend obtains an access token
with `gcloud auth print-access-token`, optionally selecting `gcp-account`, and
sends Compute API requests directly over HTTPS. Tokens and bootstrap request
bodies stay in process memory. The operator needs project/network/subnet/image
reads, instance/disk/firewall lifecycle access and operation inventory/read access.
It also reads aggregate instance inventory to detect shared firewall targets.
No service account is attached to the VM; operator credentials stay local.

Set `gcp-auth: application-default` to use credentials refreshed with
`gcloud auth application-default login`; the backend then obtains tokens through
`gcloud auth application-default print-access-token`. The default `gcp-auth: gcloud`
uses CLI account credentials. There is no automatic fallback between identities,
and `gcp-account` cannot be combined with Application Default Credentials.

The project must permit metadata SSH bootstrap; enabled project OS Login is
rejected. Instance metadata disables OS Login and inherited project SSH keys.
Organization policies can still prevent VM creation; the controller does not
change them. SSH host-key pinning and rotation follow the shared ONCE workflow.

The resource graph comprises a VM, an explicitly owned boot disk with automatic
VM-deletion cleanup disabled, and separate SSH/HTTP ingress firewall rules.
Firewall rules target the deployment's unique network tag. Empty allowed source
lists disable the corresponding rule. Other VPC/hierarchical firewall policies
remain effective; these rules do not establish exclusive network access.
Updates patch and verify each rule after its asynchronous operation completes;
SSH and HTTP rule updates are separate operations.

Operations record intent and request UUID before mutation, then persist the returned
operation identity before polling. A lost response is reconciled through operation
inventory using its request UUID and target identity; unresolved outcomes block
replay. Pending mutations retain their desired request fingerprint and reject
conflicting changes. Delete shuts down applications first, removes compute, then
its detached owned disk and firewall rules. Unknown ownership or attachments block
cleanup. Destroy protection remains enabled by default.

## SMTP transport

Use `smtp-port: 2587` and `smtp-security: starttls` for new configurations.
Existing configurations with no port preserve 465/implicit TLS. Allowed settings:

| Ports | smtp-security |
| --- | --- |
| 587, 2587 | starttls |
| 465, 2465 | implicit-tls |

The s-nail test requires TLS and validates certificates. ONCE passes only server,
port, username, password and sender to applications. Application TLS settings
remain application-specific: Fizzy defaults to STARTTLS and supports explicit
implicit TLS via `SMTP_TLS=true`. A successful host test does not prove application
mail delivery, and changing port alone does not change an application's TLS mode.
`smtp-test --to ADDRESS` sends a real email and is only for an explicitly requested
test. Ordinary plans and convergence do not send test messages.

## Provider references

- [DigitalOcean SMTP restrictions](https://docs.digitalocean.com/support/why-is-smtp-blocked/)
- [DigitalOcean Droplet API](https://docs.digitalocean.com/reference/api/reference/droplets/)
- [DigitalOcean Cloud Firewall API](https://docs.digitalocean.com/reference/api/reference/firewalls/)
- [Google Compute instances](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/insert)
- [Google firewall rules](https://docs.cloud.google.com/firewall/docs/firewalls)
- [Resend SMTP ports](https://resend.com/docs/send-with-smtp)
- [ONCE v0.3.3 SMTP environment](https://github.com/basecamp/once/blob/v0.3.3/internal/docker/application_settings.go)
- [Fizzy SMTP settings](https://github.com/basecamp/fizzy/blob/main/docs/docker-deployment.md#smtp-email)
