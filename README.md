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
    -   run `make bash` to open an interactive shell and then run `poetry add {dependency}` to add the dependency. If the dependency should be only available for development user append `-G dev` to the command.
    -   After updating the desired file(s), run `make update_deps` to update the containers with the new dependencies
        > The above command will stop and re-build the containers in order to make the new dependencies effective
        

### API Schema

We use the [`DRF-Spectacular`](https://drf-spectacular.readthedocs.io/en/latest/readme.html) tool to generate an OpenAPI schema from our Django Rest Framework API. The OpenAPI schema serves as the backbone for generating client code, creating comprehensive API documentation, and more.

The API documentation pages are accessible at `http://localhost:8000/api/schema/swagger-ui/` or `http://localhost:8000/api/schema/redoc/`.

## LocalStack S3 Configuration

This project uses [LocalStack](https://localstack.cloud/) to provide a local AWS S3-compatible service for development instead of MinIO. LocalStack offers better AWS compatibility and is widely used for local AWS service emulation.

### Setup

The docker-compose.yml is already configured to use LocalStack. After running `make up`, you need to initialize the S3 bucket:

1. **Wait for LocalStack to be ready** (usually takes a few seconds after `make up`)

2. **Initialize the S3 bucket** using one of these methods:

   **Option A: Using the provided script**
   ```bash
   ./scripts/init_localstack.sh
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

   **Option C: Using the Python script**
   ```bash
   make bash
   python scripts/init_localstack.py
   ```

3. **Verify the setup**
   ```bash
   # List buckets
   aws --endpoint-url=http://localhost:4566 s3 ls
   
   # You should see: vinta_schedule
   ```

### Configuration Details

- **Endpoint**: `http://localhost:4566` (LocalStack's default port)
- **Access Key**: `test` (LocalStack's default)
- **Secret Key**: `test` (LocalStack's default)
- **Region**: `us-east-1`
- **Bucket Name**: `vinta_schedule`

The configuration automatically switches between LocalStack (development) and AWS S3 (production) based on the `USE_LOCALSTACK` setting in your local settings.

### Troubleshooting

- **"NoSuchBucket" errors**: Make sure you've run the initialization script after starting the containers
- **Connection errors**: Ensure LocalStack container is running with `docker-compose ps`
- **Access denied**: LocalStack uses `test`/`test` as default credentials in development

## Production Deployment

### Setup

This project comes with an `render.yaml` file, which can be used to create an app on Render.com from a GitHub repository.

Before deploying, please make sure you've generated an up-to-date `poetry.lock` file containing the Python dependencies. This is necessary even if you've used Docker for local runs. Do so by following [these instructions](#setup-the-backend-app).

After setting up the project, you can init a repository and push it on GitHub. If your repository is public, you can use the following button:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

If you are in a private repository, access the following link replacing `$YOUR_REPOSITORY_URL$` with your repository link.

-   `https://render.com/deploy?repo=$YOUR_REPOSITORY_URL$`

Keep reading to learn how to configure the prompted environment variables.

#### `ALLOWED_HOSTS`

Chances are your project name isn't unique in Render, and you'll get a randomized suffix as your full app URL like: `https://vinta_schedule_api-a1b2.onrender.com`.

But this will only happen after the first deploy, so you are not able to properly fill `ALLOWED_HOSTS` yet. Simply set it to `*` then fix it later to something like `vinta_schedule_api-a1b2.onrender.com` and your domain name like `example.org`.

#### `ENABLE_DJANGO_COLLECTSTATIC`

Default is 1, meaning the build script will run collectstatic during deploys.

#### `AUTO_MIGRATE`

Default is 1, meaning the build script will run collectstatic during deploys.

### Build script

By default, the project will always run the `render_build.sh` script during deployments. This script does the following:

1.  Build the api
2.  Run Django checks
3.  Run `collectstatic`
4.  Run Django migrations

### Celery

As there aren't free plans for Workers in Render.com, the configuration for Celery workers/beat will be commented by default in the `render.yaml`. This means celery won't be available by default.

Uncommenting the worker configuration lines on `render.yaml` will imply in costs.

### SendGrid

To enable sending emails from your application you'll need to have a valid SendGrid account and also a valid verified sender identity. After finishing the validation process you'll be able to generate the API credentials and define the `SENDGRID_USERNAME` and `SENDGRID_PASSWORD` environment variables on Render.com.

These variables are required for your application to work on Render.com since it's pre-configured to automatically email admins when the application is unable to handle errors gracefully.

### Media storage

Media files integration with S3 or similar is not supported yet. Please feel free to contribute!

### Sentry

[Sentry](https://sentry.io) is already set up on the project. For production, add `SENTRY_DSN` environment variable on Render.com, with your Sentry DSN as the value.

You can test your Sentry configuration by deploying the boilerplate with the sample page and clicking on the corresponding button.

## Linting

-   At pre-commit time (see below)
-   Manually with `poetry run ruff` and `npm run lint` on project root.
-   During development with an editor compatible with ruff and ESLint.

## Pre-commit hooks

### If you are using DevContainers:

-   On project root, run `make bash` to open an interactive shell and then run `poetry run pre-commit install` to enable the hook into your git repo. The hook will run automatically for each commit done through your devcontainer.

### If you have the python dependencies installed locally

Run `poetry run pre-commit install` to enable the hook into your git repo. The hook will run automatically for each commit done.

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
