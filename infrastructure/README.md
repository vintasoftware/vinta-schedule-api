# Infrastructure — S3 + CloudFront (Terragrunt + Scalr)

Provisions the storage layer the Django app needs in production:

- **media bucket** — private S3 bucket, served only through a CloudFront
  distribution that **requires signed URLs** (django-storages signs them).
- **static bucket** — private S3 bucket, served through a public CloudFront
  distribution (no signing).
- **IAM user + access key** — long-lived credentials the app uses to upload
  objects (Render is not on AWS, so no instance role).
- **CloudFront signing key pair** — RSA key; the private key goes into Render as
  `AWS_CLOUDFRONT_KEY`, the public key id as `AWS_CLOUDFRONT_KEY_ID`.

State and runs are backed by [Scalr](https://scalr.io) via the Terraform
Cloud/Enterprise-compatible `remote` backend.

## Layout

```
infrastructure/
  terragrunt.hcl                       # root: Scalr backend + AWS provider
  modules/s3-cloudfront/               # the reusable module
  environments/
    staging/
      env.hcl                          # region + environment slug
      storage/terragrunt.hcl           # staging storage stack
    production/
      env.hcl                          # region + environment slug
      storage/terragrunt.hcl           # production storage stack (not applied yet)
```

> Only **staging** is applied for now. The production env files exist but are
> left unapplied until production goes live.

## One-time Scalr setup

1. Create a Scalr **environment** (the value for `SCALR_ENVIRONMENT`).
2. Create a workspace per stack, named to match the auto-derived name in
   `terragrunt.hcl` (`<project>-<path-with-slashes-as-dashes>`):
   - staging → `vinta-schedule-environments-staging-storage`
   - production → `vinta-schedule-environments-production-storage`

   Backend type **CLI / Terragrunt**.
3. Configure variables (next section).

## Variables to configure in Scalr

The module inputs (`project_name`, `environment`, `aws_region`,
`cors_allowed_origins`, ...) come from the Terragrunt `inputs` block and are
injected as `TF_VAR_*` — so there are **no Terraform variables to set in
Scalr**. Scalr only needs **AWS credentials**, set as **shell (environment)
variables** on the workspace (or environment):

| Variable | Value | Sensitive |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | deployer access key | yes |
| `AWS_SECRET_ACCESS_KEY` | deployer secret | yes |
| `AWS_DEFAULT_REGION` | `us-east-1` (optional; provider already sets region) | no |

- The **deployer** is an admin/CI IAM principal — **not** the app IAM user this
  stack creates. Its policy must allow `s3:*`, `cloudfront:*`, and the `iam:`
  actions to create a user + access key + inline policy
  (`CreateUser`, `CreateAccessKey`, `PutUserPolicy`, plus their Get/Delete/List
  counterparts).
- **Preferred over static keys:** attach a Scalr **Provider Configuration** for
  AWS (OIDC / role delegation) to the environment — no long-lived keys stored.

Two non-workspace settings authenticate Terragrunt to Scalr itself (set in your
shell / CI, never committed):

| Setting | How |
|---|---|
| `SCALR_TOKEN` | `terraform login <SCALR_HOSTNAME>`, or a CI env var |
| `SCALR_HOSTNAME` / `SCALR_ENVIRONMENT` | shell env vars, read by `terragrunt.hcl` |

**Execution mode matters:**
- **Remote** (runs execute inside Scalr) → the AWS shell vars MUST live in Scalr.
- **CLI / local** (`terragrunt apply` on your machine; Scalr stores state only) →
  AWS creds come from your local shell; set nothing AWS-related in Scalr.

## Run

```bash
export SCALR_HOSTNAME=example.scalr.io
export SCALR_ENVIRONMENT=<your-scalr-environment>
terraform login "$SCALR_HOSTNAME"        # stores the API token

cd infrastructure/environments/staging/storage
terragrunt init
terragrunt plan
terragrunt apply
```

## Wire outputs into Render

The `aws-storage` env var group in `render.yaml` has these as `sync: false`
(set manually in the Render dashboard). Pull each value:

```bash
terragrunt output media_bucket_name        # -> AWS_MEDIA_BUCKET_NAME
terragrunt output static_bucket_name       # -> AWS_STATIC_BUCKET_NAME
terragrunt output media_cloudfront_domain  # -> AWS_MEDIA_S3_CUSTOM_DOMAIN
terragrunt output static_cloudfront_domain # -> AWS_STATIC_S3_CUSTOM_DOMAIN
terragrunt output cloudfront_key_id        # -> AWS_CLOUDFRONT_KEY_ID
terragrunt output aws_access_key_id        # -> AWS_ACCESS_KEY_ID

terragrunt output -raw cloudfront_private_key  # -> AWS_CLOUDFRONT_KEY (full PEM)
terragrunt output -raw aws_secret_access_key   # -> AWS_SECRET_ACCESS_KEY
```

`AWS_MEDIA_S3_ENDPOINT_URL` and the `AWS_*_REGION` vars are already set to
`us-east-1` in `render.yaml`; change them there if you move the region.

## Notes

- The signing key pair lives in Terraform state — keep the Scalr state secured.
  Rotating it means re-issuing `AWS_CLOUDFRONT_KEY` / `AWS_CLOUDFRONT_KEY_ID`.
- `cors_allowed_origins` defaults to the production frontend; widen it only if
  another origin needs direct uploads.
- Reuse the module for staging: add `environments/staging/{env.hcl,storage/}`.
