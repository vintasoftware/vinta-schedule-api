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
    production/
      env.hcl                          # region + environment slug
      storage/terragrunt.hcl           # production storage stack
```

## One-time Scalr setup

1. Create a Scalr **environment** (the value for `SCALR_ENVIRONMENT`).
2. Create a workspace named `vinta-schedule-environments-production-storage`
   (matches the auto-derived name in `terragrunt.hcl`), backend type **CLI/Terragrunt**.
3. Add AWS credentials to the workspace (shell vars `AWS_ACCESS_KEY_ID` /
   `AWS_SECRET_ACCESS_KEY` for an admin/deployer principal, **not** the app
   user this stack creates).

## Run

```bash
export SCALR_HOSTNAME=example.scalr.io
export SCALR_ENVIRONMENT=<your-scalr-environment>
terraform login "$SCALR_HOSTNAME"        # stores the API token

cd infrastructure/environments/production/storage
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
