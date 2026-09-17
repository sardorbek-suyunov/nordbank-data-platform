# infra/terraform

Terraform for the BigQuery sandbox: dataset, service account, IAM bindings and the storage
bucket used by the BigQuery load path.

Scope is limited to what the secondary dbt target needs. The local stack is managed by
Compose, not by Terraform.

Populated from M10.
