# Vinta Schedule API

## Running

### Tools

-   Setup [editorconfig](http://editorconfig.org/) and [ruff](https://github.com/astral-sh/ruff) in the text editor you will use to develop.

### Setup

-   Do the following:
    -   Create a git-untracked `local.py` settings file:
        `cp vinta_schedule_api/settings/local.py.example vinta_schedule_api/settings/local.py`
    -   Create a git-untracked `.env.example` file:
        `cp .env.example .env`

### If you are using Docker:

-   Open a new command line window and go to the project's directory
-   Run the initial setup:
    `make setup`
-   Create the migrations for `users` app:
    `make makemigrations`
-   Run the migrations:
    `make migrate`
-   Run the project:
    `make up`
-   Access `http://localhost:8000` on your browser and the project should be running there
    -   When you run `make up`, some containers are spinned up (backend, database, etc) and each one will be running on a different port
-   To access the logs for each service, run:
    `make logs <service name>` (either `api`, `db`, etc)
-   To stop the project, run:
    `make down`

#### Adding new dependencies

-   Open a new command line window and go to the project's directory
-   Update the dependencies management files by performing any number of the following steps:
    -   run `make bash` to open an interactive shell and then run `uv add {dependency}` to add the dependency. If the dependency should be only available for development user append `--dev` to the command.
    -   After updating the desired file(s), run `make update_deps` to update the containers with the new dependencies
        > The above command will stop and re-build the containers in order to make the new dependencies effective
        

### API Schema

We use the [`DRF-Spectacular`](https://drf-spectacular.readthedocs.io/en/latest/readme.html) tool to generate an OpenAPI schema from our Django Rest Framework API. The OpenAPI schema serves as the backbone for generating client code, creating comprehensive API documentation, and more.

The API documentation pages are accessible at `http://localhost:8000/api/schema/swagger-ui/` or `http://localhost:8000/api/schema/redoc/`.

## Billing

Subscriptions, plans, limits, entitlements, provider adapters (Stripe,
MercadoPago), dunning, and usage metering are implemented by the
[vinta-django-billing](https://github.com/vintasoftware/vinta-django-billing)
package, not by this repository. `payments/` is this project's configuration of
that engine — the resource/entitlement registry, the reseller hierarchy, the
notification bridge, and the plan catalog. See
[`docs/billing.md`](docs/billing.md) for the full picture, including how to
upgrade the pin and a list of known package gaps.

## Floci S3 Configuration

This project uses [Floci](https://github.com/floci-io/floci) to provide a local AWS S3-compatible service for development instead of MinIO. Floci is a free, open-source AWS emulator that needs no account, auth token, or feature gates, and starts in milliseconds.

### Setup

The docker-compose.yml is already configured to use Floci. After running `make up`, you need to initialize the S3 bucket:

1. **Wait for Floci to be ready** (usually takes a few seconds after `make up`)

2. **Initialize the S3 bucket** using one of these methods:

   **Option A: Using the provided script** (run by `make setup`)
   ```bash
   docker compose run --rm api python scripts/init_floci.py
   ```

   **Option B: Using AWS CLI directly**
   ```bash
   # Create bucket
   aws --endpoint-url=http://localhost:4566 s3 mb s3://vinta_schedule --region us-east-1
   
   # Set CORS configuration
   aws --endpoint-url=http://localhost:4566 s3api put-bucket-cors \
     --bucket vinta_schedule \
     --cors-configuration file://scripts/cors-config.json
   ```

3. **Verify the setup**
   ```bash
   # List buckets
   aws --endpoint-url=http://localhost:4566 s3 ls
   
   # You should see: vinta_schedule
   ```

### Configuration Details

- **Endpoint**: `http://localhost:4566` (Floci's default port)
- **Access Key**: `test`
- **Secret Key**: `test`
- **Region**: `us-east-1`
- **Bucket Name**: `vinta_schedule`

The configuration automatically switches between Floci (development) and AWS S3 (production) based on the `USE_FLOCI` setting in your local settings.

### Troubleshooting

- **"NoSuchBucket" errors**: Make sure you've run the initialization script after starting the containers
- **Connection errors**: Ensure the Floci container is running with `docker-compose ps`
- **Access denied**: Floci uses `test`/`test` as default credentials in development

## Production Deployment

The API runs on **AWS ECS/Fargate**, provisioned by Terraform under
[`infrastructure/`](infrastructure/) and applied through Terragrunt + Scalr. That
directory's [README](infrastructure/README.md) is the operational reference —
architecture, Scalr setup, cross-account DNS, secrets, day-to-day commands and the
cost levers. What follows is only the short version.

### What runs where

| Piece | Where |
|---|---|
| Django (gunicorn) | ECS Fargate service behind a public ALB, in private subnets |
| Celery worker | ECS Fargate service (Fargate Spot), private subnets |
| Celery beat | ECS Fargate service (Fargate Spot), one task, redbeat scheduler |
| Broker | Amazon SQS |
| Database | RDS Postgres, private, TLS enforced |
| Cache / result backend / redbeat store | ElastiCache, private, TLS + AUTH token |
| Media + static | S3 behind CloudFront (`infrastructure/modules/s3-cloudfront`) |
| Credentials | one AWS Secrets Manager secret per environment |

Only the load balancer is public. Outbound calls to Google, Microsoft, Stripe,
MercadoPago and Twilio leave through a NAT gateway.

### Deploys

Pushing to `main` runs the `deploy-staging` job in
[`.github/workflows/main.yml`](.github/workflows/main.yml), after lint, type
checks and the test suite pass. It:

1. assumes an AWS role over GitHub OIDC (no stored access keys),
2. builds the image and pushes it to ECR tagged with the commit SHA,
3. registers a new task-definition revision per service,
4. runs the **release task** — `migrate` then `collectstatic` — and waits for it,
5. only then points the web, worker and beat services at the new revision, and
   waits for them to stabilise.

Step 4 is the gate: a failed migration stops the deploy before any container
serving traffic has been replaced. The rollout logic lives in
[`scripts/deploy/ecs_deploy.sh`](scripts/deploy/ecs_deploy.sh); the workflow only
supplies the image and the SSM parameter naming the environment.

Production has no deploy job yet — see *Before applying production* in the
infrastructure README.

### Environment variables

Non-secret configuration is set on the ECS task definitions from Terraform inputs
(`infrastructure/environments/<env>/app/terragrunt.hcl`). Credentials live in one
Secrets Manager secret per environment, which ECS resolves key-by-key into
individual environment variables. Terraform seeds that secret once and then leaves
its value to operators.

Note that no `AWS_ACCESS_KEY_ID` is set in the deployed containers: boto3 checks
the environment before the container credential endpoint, so a static key would
shadow the ECS task role that grants S3 and SQS access.

### Email

`SMTP_HOST` / `SMTP_USERNAME` / `SMTP_PASSWORD` in the app secret configure
outbound mail. `DEFAULT_FROM_EMAIL` must be on a domain the SMTP provider is
verified to send for, or mail is rejected or spam-filed.

### Sentry

[Sentry](https://sentry.io) is already set up. Put your DSN in the `SENTRY_DSN`
key of the app secret. Each deploy tags events with `COMMIT_SHA`, so a stack trace
names the release it came from.

## Linting

-   At pre-commit time (see below)
-   Manually with `uv run ruff` and `npm run lint` on project root.
-   During development with an editor compatible with ruff and ESLint.

## Pre-commit hooks

### If you are using DevContainers:

-   On project root, run `make bash` to open an interactive shell and then run `uv run pre-commit install` to enable the hook into your git repo. The hook will run automatically for each commit done through your devcontainer.

### If you have the python dependencies installed locally

Run `uv run pre-commit install` to enable the hook into your git repo. The hook will run automatically for each commit done.

## Opinionated Settings

Some settings defaults were decided based on Vinta's experiences. Here's the rationale behind them:

### `DATABASES["default"]["ATOMIC_REQUESTS"] = True`

- Using atomic requests in production prevents several database consistency issues. Check [Django docs for more details](https://docs.djangoproject.com/en/5.0/topics/db/transactions/#tying-transactions-to-http-requests).

- **Important:** When you are queueing a new Celery task directly from a Django view, particularly with little or no delay/ETA, it is essential to use `transaction.on_commit(lambda: my_task.delay())`. This ensures that the task is only queued after the associated database transaction has been successfully committed.
  - If `transaction.on_commit` is not utilized, or if a significant delay is not set, you risk encountering race conditions. In such scenarios, the Celery task might execute before the completion of the request's transaction. This can lead to inconsistencies and unexpected behavior, as the task might operate on a database state that does not yet reflect the changes made in the transaction. Read more about this problem on [this article](https://www.vinta.com.br/blog/database-concurrency-in-django-the-right-way).

### `CELERY_TASK_ACKS_LATE = True`

- We believe Celery tasks should be idempotent. So for us it's safe to set `CELERY_TASK_ACKS_LATE = True` to ensure tasks will be re-queued after a worker failure. Check Celery docs on ["Should I use retry or acks_late?"](https://docs.celeryq.dev/en/stable/faq.html#faq-acks-late-vs-retry) for more info.

### Django-CSP

Django-CSP helps implementing Content Security Policy (CSP) in Django projects to mitigate cross-site scripting (XSS) attacks by declaring which dynamic resources are allowed to load.

In this project, we have defined several CSP settings that define the sources from which different types of resources can be loaded. If you need to load external images, fonts, or other resources, you will need to add the sources to the corresponding CSP settings. For example:
- To load scripts from an external source, such as https://browser.sentry-cdn.com, you would add this source to `CSP_SCRIPT_SRC`.
- To load images from an external source, such as https://example.com, you would add this source to `CSP_IMG_SRC`.

Please note that you should only add trusted sources to these settings to maintain the security of your site. For more details, please refer to the [Django-CSP documentation](https://django-csp.readthedocs.io/en/latest/).
