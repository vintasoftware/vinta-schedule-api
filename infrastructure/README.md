# Infrastructure — AWS via Terragrunt + Scalr

**One stack per environment, one Scalr workspace per environment.** Each
environment's `terragrunt.hcl` points at `modules/environment`, which composes
the two modules below into a single Terraform state — so one Scalr run applies
all of it:

| Module | Composed as | What it owns |
|---|---|---|
| `modules/s3-cloudfront` | `module.storage` | Media + static S3 buckets, their CloudFront distributions, the signed-URL key pair, and a long-lived IAM user (a leftover from the Render deploy — see [Storage IAM user](#storage-iam-user)). |
| `modules/app-platform` | `module.app` | Everything the API itself runs on: VPC, ALB, ECS/Fargate services, RDS, ElastiCache, SQS, Secrets Manager, ECR, and the GitHub Actions deploy role. |

The bucket names and CDN hostnames flow from `module.storage` into `module.app`,
so the two can no longer disagree and there is no apply-this-one-first rule.

State and runs are backed by [Scalr](https://scalr.io) via the Terraform
Cloud/Enterprise-compatible `remote` backend.

> Coming from the old two-workspace layout? Read
> [Migrating the staging workspace](#migrating-the-staging-workspace) before the
> first apply.

## Layout

```
infrastructure/
  root.hcl                             # Scalr backend + AWS providers (default + aws.dns)
  modules/
    environment/                       # composes the two below -- the only root module
    s3-cloudfront/                     # buckets + CDN
    app-platform/                      # the runtime platform
  environments/
    staging/
      env.hcl                          # region, DNS role, workspace name
      terragrunt.hcl                   # <- the Scalr working directory
      .terraform.lock.hcl
    production/
      env.hcl                          # not applied yet
      terragrunt.hcl
      .terraform.lock.hcl
  scripts/
    migrate-storage-state.sh           # one-off: two workspaces -> one
```

> Only **staging** is applied. The production files exist and are complete, but
> read [Before applying production](#before-applying-production) first — one input
> has to be filled in by hand.

## Architecture

```
                       Route 53 (DNS account, cross-account role)
                                    |
                    api.schedule-staging.vintasoftware.com
                                    |
   internet ──▶ ALB (public subnets, ACM cert, 80 ⇒ 301 ⇒ 443)
                                    |
   ┌────────────────────── private subnets ──────────────────────┐
   │  ECS Fargate                                                │
   │    web      gunicorn, behind the ALB          (FARGATE)     │
   │    worker   celery worker                     (FARGATE_SPOT)│
   │    beat     celery beat / redbeat             (FARGATE_SPOT)│
   │    release  migrate + collectstatic, run once per deploy    │
   │                                                             │
   │  RDS Postgres        ElastiCache (TLS + AUTH)               │
   └──────────────────────────┬──────────────────────────────────┘
                              │
                     NAT gateway ──▶ Google / Microsoft / Stripe /
                                     MercadoPago / Twilio / SES-SMTP
```

Nothing but the ALB has a public address. The ECS tasks reach the internet — and
the AWS APIs they do not have a VPC endpoint for — through the NAT gateway. S3 has
a free gateway endpoint, which matters more than it sounds: ECR keeps image layers
in S3, so without it every task start would pull the whole image through NAT at
per-GB rates.

### Why SQS is the broker and ElastiCache is not

Celery brokers over **SQS**; ElastiCache is only the result backend, redbeat's
schedule store, and django-defender's throttle counter. Three consequences worth
knowing, all handled in `settings/production.py`:

- **`visibility_timeout` is the real task timeout.** `CELERY_TASK_ACKS_LATE` is on,
  so a message is deleted only when its task finishes. If the timeout expires
  first, SQS hands the same task to a second worker and it runs twice. The
  Terraform variable `sqs_visibility_timeout_seconds` and the container's
  `CELERY_SQS_VISIBILITY_TIMEOUT` are set from the same value for this reason.
- **No remote control, no events.** SQS has no fanout exchange, so
  `celery inspect`, `celery control` and flower have nothing to talk to. Both are
  disabled explicitly; CloudWatch Logs is where you look instead.
- **A poison task lands in the DLQ.** After `sqs_max_receive_count` deliveries the
  message moves to `<name>-celery-dlq` rather than looping. Anything sitting there
  is a task that failed repeatedly.

## One-time Scalr setup

1. Create a Scalr **environment** (the value for `SCALR_ENVIRONMENT`).
2. Create one Scalr workspace **per environment**. The name is read from
   `environments/<env>/env.hcl` (`scalr_workspace`), so it has to match whatever
   you named it in Scalr:

   | Environment | Workspace |
   |---|---|
   | staging | `VintaScheduleStaging` |
   | production | `VintaScheduleProduction` |

   Backend type **CLI / Terragrunt**.
3. Set each workspace's **Working Directory** to the environment folder — the one
   holding both `terragrunt.hcl` and `env.hcl`:
   - `infrastructure/environments/staging`
   - `infrastructure/environments/production`
4. Configure variables (next section).

## Variables to configure in Scalr

Module inputs come from the Terragrunt `inputs` block and are injected as
`TF_VAR_*`, so there are **no Terraform variables to set in Scalr**. Scalr only
needs **AWS credentials**, as **shell (environment) variables** on the workspace
(or environment):

| Variable | Value | Sensitive |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | deployer access key | yes |
| `AWS_SECRET_ACCESS_KEY` | deployer secret | yes |
| `AWS_DEFAULT_REGION` | `us-east-1` (optional; the provider already sets region) | no |

- The **deployer** needs rights over everything the stack creates: the VPC and
  its security groups, the ALB, ECS, ECR, RDS, ElastiCache, SQS, Secrets
  Manager, the SSM deploy parameter, CloudWatch log groups, and the IAM roles
  plus the GitHub OIDC provider. The deployer created for the storage-only
  stack has none of that and fails its first `app` plan on
  `ec2:DescribeAvailabilityZones` — attach the three documents in
  [policies/](policies/README.md), which grant exactly those actions and no
  more.
- **Preferred over static keys:** attach a Scalr **Provider Configuration** for
  AWS (OIDC / role delegation) to the environment — no long-lived keys stored.

Two non-workspace settings authenticate Terragrunt to Scalr itself (set in your
shell / CI, never committed):

| Setting | How |
|---|---|
| `SCALR_TOKEN` | `terraform login <SCALR_HOSTNAME>`, or a CI env var |
| `SCALR_HOSTNAME` / `SCALR_ENVIRONMENT` | shell env vars, read by `root.hcl` |

**These workspaces are VCS-connected and run remotely**, which settles two
things: the AWS credentials above MUST live in Scalr (nothing reads your local
shell), and applies are triggered by pushing to this repository rather than
from your machine — see [Run](#run).

## Cross-account DNS (Route 53)

`vintasoftware.com` lives in a **different AWS account** than the buckets,
CloudFront and the ALB. ACM certs and load balancers are created in the deploy
account; the Route 53 records (ACM validation + the alias records) are written via
an **aliased `aws.dns` provider that assumes a role in the DNS account**
(`dns_role_arn` in each `env.hcl`).

Set up once:

1. **In the DNS account**, create the role Terraform assumes (replace
   `DEPLOY_ACCOUNT_ID` and `ZONE_ID`):

   ```bash
DNS_ROLE=vinta-schedule-dns-deployer
DEPLOY_ACCOUNT_ID=261390480437   # account with the buckets/CloudFront/ALB + deployer
ZONE_ID=Z2BH7RSHN2OFNV           # vintasoftware.com zone, in DNS account 310361226925

aws iam create-role --role-name "$DNS_ROLE" \
  --assume-role-policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{\"Effect\":\"Allow\",
      \"Principal\":{\"AWS\":\"arn:aws:iam::${DEPLOY_ACCOUNT_ID}:root\"},
      \"Action\":\"sts:AssumeRole\"}]}"

aws iam put-role-policy --role-name "$DNS_ROLE" --policy-name route53-records \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[
      {\"Effect\":\"Allow\",
      \"Action\":[\"route53:ChangeResourceRecordSets\",\"route53:ListResourceRecordSets\",\"route53:GetHostedZone\",\"route53:ListTagsForResource\"],
      \"Resource\":\"arn:aws:route53:::hostedzone/${ZONE_ID}\"},
      {\"Effect\":\"Allow\",
      \"Action\":[\"route53:ListHostedZones\",\"route53:ListHostedZonesByName\",\"route53:GetChange\"],
      \"Resource\":\"*\"}]}"
   ```

2. **Allow each deployer to assume it** (run with deploy-account creds, per env):

   ```bash
ENV=staging   # then repeat with ENV=production
aws iam put-user-policy --user-name "vinta-schedule-${ENV}-deployer" \
  --policy-name assume-dns-role \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sts:AssumeRole\",
      \"Resource\":\"arn:aws:iam::310361226925:role/vinta-schedule-dns-deployer\"}]}"
   ```

3. Set `dns_role_arn` in each `environments/<env>/env.hcl` to that role's ARN.

## Migrating the staging workspace

Staging was applied while `storage` and `app` were separate root modules, so
`VintaScheduleStaging` holds the storage resources at top-level addresses
(`aws_s3_bucket.media`). The composed module knows those same resources as
`module.storage.aws_s3_bucket.media`, so the state has to be renamed once before
the first apply of this layout. Nothing in AWS is touched — the rename only
rewrites addresses inside the state.

```bash
export SCALR_HOSTNAME=example.scalr.io
export SCALR_ENVIRONMENT=<your-scalr-environment>
terraform login "$SCALR_HOSTNAME"

# 1. see what would move (dry run is the default)
infrastructure/scripts/migrate-storage-state.sh

# 2. move it -- a state backup is written into the environment folder first
infrastructure/scripts/migrate-storage-state.sh --apply

# 3. check
cd infrastructure/environments/staging && terragrunt plan
```

That plan should create the app-platform resources and report **no changes** to
the buckets, the CloudFront distributions, the signing key or the storage IAM
user. If it wants to replace any of those, stop and push back the backup the
script wrote (the script prints the exact command).

Re-running the script against an already-migrated state is a no-op.

Then, in Scalr, point `VintaScheduleStaging`'s **Working Directory** at
`infrastructure/environments/staging`. The `VintaScheduleStagingApp` workspace
this repo used to name holds no state — the app stack was never applied through
it — so it can be deleted. If it turns out to hold state, don't run the script:
two populated states have to be reconciled by hand instead.

## Run

> **Terraform version:** the Scalr `remote` backend only accepts Terraform
> `<= 1.5.99` (1.6+ is BSL and rejected). Use **1.5.7** locally — pinned in
> `infrastructure/.terraform-version`. With tfenv: `tfenv install 1.5.7`. Newer
> Terraform fails `init` with "Please downgrade Terraform to <= 1.5.99".
>
> **Not OpenTofu.** Terragrunt runs `tofu` by default when it finds one on
> PATH, and OpenTofu 1.12 refuses to write to a workspace pinned at 1.5.7
> ("version mismatch … `-ignore-remote-version`"). `root.hcl` therefore sets
> `terraform_binary = "terraform"` and constrains the version, so both traps
> now fail loudly or not at all. If a folder was already initialised by `tofu`,
> delete its `.terragrunt-cache` before the next run.

> **Apply happens in Scalr, not from your shell.** The workspaces are connected
> to this repository, and a VCS-connected workspace refuses a CLI apply:
>
> ```
> Error: Apply not allowed for workspaces with a VCS connection
> ```
>
> So `terragrunt apply` — and anything carrying `-replace` or `-target` — is not
> how this gets applied. Push the branch and let the workspace run it. What does
> work locally is `init`, `plan` (it opens a speculative run), `output`, and the
> `state` subcommands.

```bash
export SCALR_HOSTNAME=example.scalr.io
export SCALR_ENVIRONMENT=<your-scalr-environment>
terraform login "$SCALR_HOSTNAME"        # stores the API token

cd infrastructure/environments/staging
terragrunt init
terragrunt plan                          # speculative run, safe to repeat
```

One Scalr run covers the buckets, the CDN and the runtime platform. Terraform
orders them itself: the task role's S3 policy reads the bucket names out of
`module.storage`, and that dependency edge is what used to be a manual
apply-storage-first rule.

### Forcing one resource to be recreated

`-replace` needs an apply, so it is unavailable here. Drop the resource from
state instead and let the next Scalr run create it — `state` commands are
allowed:

```bash
cd infrastructure/environments/staging
terragrunt run -- state rm module.app.aws_acm_certificate.api
```

Nothing in AWS is destroyed by that; the old object is simply no longer
managed, so delete it by hand afterwards if it costs money or occupies a name
the new one needs.

### Provider lock files must cover every platform

`terragrunt init` on your laptop records a checksum only for the platform it ran
on. Scalr runs `linux_amd64` and installs providers from its own cache, where the
registry `zh:` checksums don't apply — so a Mac-only lock file makes every Scalr
run fail at `init` with "the local package ... doesn't match any of the checksums
previously recorded".

After any provider version change, re-lock for all platforms and commit the
result:

```bash
cd infrastructure/environments/staging
# `run --` passes the flags through; terragrunt would otherwise reject -platform.
terragrunt run -- providers lock \
  -platform=linux_amd64 \
  -platform=darwin_arm64 \
  -platform=darwin_amd64
```

## After the first apply

> **The services will be unhealthy until the first deploy, and that is expected.**
> The task definitions point at `<ecr-repo>:latest`, and the repository is empty
> until GitHub Actions pushes an image. ECS will keep failing to pull, the
> deployment circuit breaker will keep rolling back, and the first successful
> deploy fixes it. Do steps 1 and 2 below, then push to `main`.

### 1. Fill in the app secret

The stack creates **one** Secrets Manager secret holding every credential the
containers read, as a flat JSON object. Terraform seeds it once — it knows
`DATABASE_URL` and `REDIS_URL`, and generates `SECRET_KEY` and `SALT_KEY` — then
stops managing the value (`ignore_changes`), so operators own it from then on.

Everything else is seeded **empty** and has to be filled in before the app is
usable. Keys awaiting a value:

`SENTRY_DSN`, `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`, `TWILIO_ACCOUNT_SID`, `TWILIO_API_KEY_SID`,
`TWILIO_API_KEY_SECRET`, `TWILIO_AUTH_TOKEN`, `TWILIO_NUMBER`,
`TWILIO_DEFAULT_BROADCAST_NUMBERS`, `MERCADOPAGO_ACCESS_TOKEN`,
`MERCADOPAGO_WEBHOOK_SECRET`, `MERCADOPAGO_PUBLIC_KEY`, `STRIPE_SECRET_KEY`,
`STRIPE_WEBHOOK_SECRET`, `STRIPE_PUBLISHABLE_KEY`, `AWS_CLOUDFRONT_KEY_ID`,
`AWS_CLOUDFRONT_KEY`.

The last two come from the storage module, which now lives in the same state:

```bash
cd infrastructure/environments/staging
terragrunt output cloudfront_key_id             # -> AWS_CLOUDFRONT_KEY_ID
terragrunt output -raw cloudfront_private_key   # -> AWS_CLOUDFRONT_KEY (full PEM)
```

Edit the secret in the console (Secrets Manager → the secret named by
`terragrunt output app_secret_name` → *Retrieve/Edit*), or from the CLI:

```bash
aws secretsmanager get-secret-value --secret-id vinta-schedule-staging/app \
  --query SecretString --output text > /tmp/app-secret.json
# edit /tmp/app-secret.json, then:
aws secretsmanager put-secret-value --secret-id vinta-schedule-staging/app \
  --secret-string file:///tmp/app-secret.json
```

ECS reads the secret when a task **starts**, so a changed value reaches the app
only on the next deploy or `aws ecs update-service --force-new-deployment`.

> **`DATABASE_URL` and `REDIS_URL` do not auto-update.** Because Terraform stops
> managing the value, a restored database or rebuilt cache means pasting the new
> URL in by hand — `terragrunt output database_url` / `redis_url` print them.

### 2. Point GitHub Actions at the deploy role

```bash
terragrunt output github_deploy_role_arn
```

Set it as the repository **variable** (not secret) `AWS_DEPLOY_ROLE_ARN_STAGING`
under Settings → Secrets and variables → Actions → Variables. The workflow's
`deploy-staging` job assumes it over OIDC — no access keys anywhere.

The role's trust policy pins `repo:<owner>/<repo>:ref:refs/heads/main`, so a
workflow run from a fork or a feature branch cannot assume it. Nothing else is
needed: cluster names, service names, subnets and security groups all come from
the SSM parameter the stack writes.

### 3. Verify

```bash
curl -i https://api.schedule-staging.vintasoftware.com/healthz/
```

## Day-to-day operations

**A shell in a running task** (the only way into the private subnets without a
bastion — `enable_execute_command` is on for every service):

```bash
aws ecs execute-command --cluster vinta-schedule-staging \
  --task "$(aws ecs list-tasks --cluster vinta-schedule-staging \
             --service-name vinta-schedule-staging-web \
             --query 'taskArns[0]' --output text)" \
  --container web --interactive --command "/bin/bash"
# then: python manage.py shell   /   python manage.py dbshell
```

**Logs:** `/ecs/vinta-schedule-staging/{web,worker,beat,release}` in CloudWatch.

**Roll back to a previous image:** re-run the deploy workflow on the older commit,
or point the services at an earlier task-definition revision:

```bash
aws ecs update-service --cluster vinta-schedule-staging \
  --service vinta-schedule-staging-web --task-definition vinta-schedule-staging-web:42
```

**Inspect the dead-letter queue:** `terragrunt output celery_dlq_url`, then
`aws sqs receive-message --queue-url <url>`.

## Before applying production

1. **`github_oidc_provider_arn` must be filled in.** An AWS account holds exactly
   one GitHub OIDC provider, and staging already creates it. Copy
   `terragrunt output github_oidc_provider_arn` from staging into
   `environments/production/terragrunt.hcl`, or the apply fails on a duplicate.
2. Create the `VintaScheduleProduction` workspace with its working directory set
   to `infrastructure/environments/production`. Production has never been
   applied, so there is no state to migrate — unlike staging.
3. Add a `deploy-production` job to `.github/workflows/main.yml`, pointed at
   `/vinta-schedule/production/deploy` and a `AWS_DEPLOY_ROLE_ARN_PRODUCTION`
   variable. It is deliberately absent today: pushing to `main` should not deploy
   production without a gate (a GitHub environment with required reviewers, or a
   tag trigger) that is a decision for whoever turns production on.
4. Fill in the production app secret, as in step 1 above.

## Cost notes

The knobs that actually move the bill, roughly largest first:

| Thing | Lever | Note |
|---|---|---|
| NAT gateway | `single_nat_gateway` | ~$32/mo + data. One is the default; the second would only buy AZ redundancy for *outbound* traffic. |
| RDS | `db_instance_class`, `db_multi_az` | `db.t4g.micro`, single-AZ on staging. Multi-AZ doubles it. |
| Fargate | `*_cpu` / `*_memory` / `*_desired_count` | Worker and beat run on `FARGATE_SPOT` (`use_fargate_spot_for_workers`, ~30% cheaper); web stays on-demand. |
| ElastiCache | `cache_node_type`, `cache_node_count` | `cache.t4g.micro`, one node. `cache_engine` defaults to `valkey`, which AWS prices below Redis OSS. |
| ALB | — | Fixed hourly charge; unavoidable for a public HTTPS endpoint. |
| CloudWatch Logs | `log_retention_days` | 14 days. |
| SQS | — | Effectively free at this volume; long polling (`receive_wait_time_seconds = 20`) keeps idle workers from billing a request per second. |

Interface VPC endpoints for ECR/Logs/SQS/Secrets Manager are deliberately *not*
created: at ~$7/month each per AZ they cost more than the NAT data they'd save at
this traffic level.

## Storage IAM user

`modules/s3-cloudfront` still creates an IAM user and access key. It exists
because Render was not on AWS and needed static credentials. **The ECS tasks do
not use it** — they get S3 access from their task role, and no
`AWS_ACCESS_KEY_ID` is set anywhere in the task definitions precisely so boto3
resolves that role instead. The user can be removed from the storage module once
nothing else depends on those keys.

## Notes

- The CloudFront signing key pair, the database password, the cache AUTH token and
  the generated `SECRET_KEY` / `SALT_KEY` all live in Terraform state — keep the
  Scalr state secured. Rotating `SALT_KEY` makes every already-encrypted field
  unreadable.
- `storage_cors_allowed_origins` governs direct browser uploads to the media
  bucket; `cors_allowed_origins` governs the API's own CORS headers. They are
  separate inputs on purpose — the app module and the storage module each get
  their own list.
