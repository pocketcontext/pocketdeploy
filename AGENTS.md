# PocketDeploy

Public Python deployment controller. Read README.md before changes. Keep live
configuration, credentials, SQLite databases/journals, keys, snapshots and plans
ignored. Never emit raw cloud responses, Docker metadata, ONCE labels or secret
values. Tests use synthetic data. OCI mutations require explicit user scope. Keep production adoption separate
from disposable verification; routine tests never access live resources. Use existing subnet/VCN, own only tagged test resources.
Run `uv sync --extra test` and `uv run pytest` before release. Blue is a pinned
library; no Terraform, Ansible or Clojure runtime. One operator at a time.
