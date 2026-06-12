# Vinta Schedule API

A multi-tenant Django backend that powers calendar and scheduling features: external calendar provider integration (Google, Outlook), recurring-event expansion in Postgres, calendar bundles for cross-calendar event creation, organization-scoped data isolation, and a public GraphQL API. Not yet deployed — current target is development only.

## Project Overview

Single Django project, multiple installed apps under the repo root:

- `vinta_schedule_api/` — Django project (settings, urls, wsgi/asgi).
- `accounts/` — account-level concerns above per-user.
- `users/` — user model + factories + auth-adjacent code.
- `organizations/` — tenant model. Backbone of the multi-tenancy contract.
- `common/` — shared abstract models, custom managers / querysets / fields, view utils, raw-SQL migration framework.
- `di_core/` — dependency-injector containers; every service registers here.
- `calendar_integration/` — calendar provider integrations, recurrence calculation, calendar bundles, availability.
- `notifications/` — outbound notifications (via vintasend).
- `payments/` — payment integrations (MercadoPago).
- `public_api/` — GraphQL public API (Strawberry).
- `webhooks/` — inbound webhook handlers.
- `s3direct_overrides/` — overrides for `django-s3direct`.
- `vintasend_django_sms_template_renderer/`, `vintasend_twilio/` — vintasend SMS/Twilio glue.

Postgres is the only persistent store. Recurring-event occurrences are calculated dynamically by Postgres functions defined under `calendar_integration/migrations/sql/functions/`, exposed to the ORM via `calendar_integration/database_functions.py`. Celery (with `celery-redbeat`) handles async tasks; `RabbitMQ` is the broker and `Redis` the result backend in dev.

## Tech Stack

- **Django 6** (`pyproject.toml`) — primary framework.
- **Django REST Framework 3.16** + **drf-spectacular** — REST API + OpenAPI schema export (`schema.yml`, `schema-auth.yml`).
- **Strawberry GraphQL** + **strawberry-graphql-django** — public API at `public_api/`.
- **django-allauth 65** (with `socialaccount`, `mfa`) — auth, social login, MFA.
- **django-virtual-models** — queryset optimization driven by DRF serializers.
- **dependency_injector 4.47** — DI containers in `di_core/containers.py`.
- **django-fernet-encrypted-fields** — at-rest encrypted fields.
- **Celery 5.5** + **celery-redbeat** — async tasks + scheduled jobs.
- **psycopg 3** — Postgres driver.
- **uv** — Python package manager + lock (`uv.lock`).
- **ruff** — lint + format. Single source of truth; no Black, no Flake8, no isort separately.
- **mypy** with `django-stubs` + `djangorestframework-stubs` — typing.
- **pytest** + **pytest-django** + **pytest-xdist** + **pytest-cov** + **model-bakery** — testing.
- **pre-commit** — local hooks (`.pre-commit-config.yaml`): ruff, django-upgrade (5.0 target), local eslint/tsc/missing-migrations/spectacular-schema-export.
- **Docker Compose** (`docker-compose.yml`) — dev runtime: api, db (postgres:alpine), broker (rabbitmq:alpine), result (redis:alpine), floci (local AWS S3 emulator), mailpit.
- **Render** (`render.yaml`) — production target (not yet deployed).
- **Sentry** — error tracking (`sentry-sdk`); `SENTRY_DSN` env var.

## Common Commands

Two execution surfaces: **inside the container** (via `make` targets) and **on the host** (via `uv run` directly). Pre-commit hooks and CI use the host surface; daily app work uses the container surface.

```bash
# Container surface (Makefile targets)
make setup                # one-time: volumes, build, schema export, Floci init
make up                   # start api container + deps (db, broker, redis, floci, mailpit)
make up_with_workers      # also start celery worker + beat
make down                 # stop containers
make logs <service>       # tail logs
make bash                 # interactive shell in api container
make shell                # Django shell
make manage <args>        # python manage.py <args>
make makemigrations       # python manage.py makemigrations
make migrate              # python manage.py migrate
make test                 # pytest -n auto --reuse-db
make test_reset           # pytest -n auto (drops + recreates test DB)
make test_seq             # pytest --reuse-db (single process)
make test_cov             # pytest -n auto with HTML coverage at junit/test-results.html
make update_schema        # python manage.py spectacular --file schema.yml
make update_deps          # uv sync --no-install-project then rebuild

# Host surface (run before commit / during code review)
uv sync --frozen                              # install deps from uv.lock
uv run ruff check ./                          # lint (CI gate)
uv run ruff format ./                         # format
uv run mypy .                                 # type check (configured but not in CI)
uv run pytest -n auto                         # full suite
uv run pytest <app>/tests/ -n auto            # scoped suite
uv run pytest <path/to/test_file.py> -vs      # single file
uv run python manage.py check --deploy        # production-config check (CI gate)
uv run python manage.py makemigrations --check  # CI gate: no missing migrations
uv run python manage.py spectacular --file schema.yml  # regenerate OpenAPI schema
uv run pre-commit install                     # one-time: install git hooks
uv run pre-commit run --all-files             # run all hooks locally
```

### Outer gate

Canonical pre-commit verification chain. Every implementer / fixer / specialist agent runs this before declaring a phase done; every drafted skill's Verification section references it instead of restating.

```bash
uv run ruff check ./
uv run ruff format --check ./
uv run mypy .
uv run python manage.py makemigrations --check
uv run python manage.py check --deploy
uv run pytest -n auto
```

All six must pass. Skill-specific Verification blocks add commands on top (schema regenerate, migration apply + reverse, view introspection, etc.) — the outer gate stays constant.

## Code Style

- **Ruff is the source of truth.** Config in `pyproject.toml#[tool.ruff]`. Line length 100, indent 4, py3.13 target. Selected rule sets: `E`, `F`, `N`, `UP`, `B`, `S`, `BLE`, `A`, `DJ`, `I`, `G`, `INP`, `RUF`. Migrations + tests + settings + `__init__.py` have rule-set carveouts (see `[tool.ruff.lint.per-file-ignores]`).
- **isort sections** (configured under ruff): `future`, `standard-library`, `django`, `third-party`, `first-party`, `local-folder`. Two blank lines after imports.
- **Static typing required** on every function, method, and class. Use Python's type inference rather than redefining types when possible. `mypy` is configured with `django-stubs` + `djangorestframework-stubs` plugins.
- **Absolute imports only.** Imports at the top of the file unless there is a concrete reason to defer (typing.TYPE_CHECKING is the most common exception).
- **f-strings** for formatting. Comprehensions over `map` / `filter`. `is` / `is not` for `None`, `True`, `False` comparisons.
- **Custom exceptions** for error handling — never raise bare `Exception`.
- **logging** instead of `print` for any non-CLI output.
- **Docstrings** on public classes, methods, functions.
- **PEP 8** is the floor; ruff enforces specifics. Follow the existing style of the file you are editing.

## Architecture

### Dependency Injection (`di_core/`)

Every service registers in `di_core/containers.py`. Services receive dependencies via the container, not direct imports. When adding a new service:

1. Define it in `<app>/services/<name>.py` as a stateless class.
2. Register it in the appropriate provider in `di_core/containers.py`.
3. Inject it where consumed (views, GraphQL resolvers, other services) — do not import directly.

This is enforced by convention, not the type system; a PR that direct-imports a service is a review block.

### Services (`<app>/services/`)

- Stateless. All dependencies arrive via DI.
- Single responsibility — each service does one thing.
- Business logic lives here, **not** in views, serializers, querysets, or GraphQL resolvers.

### Custom Managers and Querysets

- Defined per app in `<app>/managers.py` + `<app>/querysets.py`.
- Querysets are chainable + composable. Managers expose domain-specific methods (e.g. `for_organization(...)`, `with_availability(...)`).
- **Hard rule:** never construct a complex queryset inline inside a service, view, or serializer. The query logic belongs on the manager / queryset. If you find yourself chaining 3+ filters or annotating across joins inline, move it to a manager method.
- **Hard rule:** for `OrganizationModel` subclasses, never bypass the model manager (no `Model._meta.default_manager.get_queryset()`, no raw `Model.objects.raw(...)` that skips the organization filter, no direct `cursor.execute` that reads from tenant tables without the organization clause). The manager raises if the organization filter is missing — bypassing it bypasses the safety net.
- Examples: `calendar_integration/managers.py`, `calendar_integration/querysets.py`.

### Django Virtual Models (`<app>/virtual_models.py`)

`django-virtual-models` optimizes ORM querysets based on the DRF serializer attached to a view / endpoint. Define a virtual model per serializer when the resulting queryset would otherwise N+1 or over-fetch. See the docs at https://github.com/vintasoftware/django-virtual-models.

### Custom Views (`common/utils/views_utils.py`)

Reusable view mixins live here. Prefer class-based views / viewsets and compose from these mixins.

### Public API — GraphQL (`public_api/`)

- Schema in `public_api/schema.py`.
- Queries in `public_api/queries.py`, mutations in `public_api/mutations.py`.
- Per-app GraphQL types live in `<app>/graphql.py` and are registered in `public_api/queries.py`.
- Per-app mutations live in `<app>/mutations.py` and are registered in `public_api/mutations.py`.
- Auth + authorization use `PublicAPIAuthService` and the permission classes in `public_api/permissions.py`.
- Resolvers receive services via DI.
- Paginate every list query.

### Raw SQL: Functions, Procedures, Triggers, Views, Materialized Views

Custom DB-defined code is versioned via the framework in `common/raw_sql_migration_managers.py`:

- **New structure:** create a directory named after the structure under the appropriate type directory; add `0001.sql` with the body. Then define a migration manager in `__init__.py` inheriting from the manager classes in `common/raw_sql_migration_managers.py`. Then create a Django migration whose `operations` calls `manager.migration(...)`.
- **Update an existing structure:** add the next-numbered SQL file (`0002.sql`, `0003.sql`, ...) and create a Django migration whose `operations` calls `manager.migrate(...)` referencing the new version name.
- Examples: `calendar_integration/migrations/sql/functions/calculate_recurring_events`, `.../get_event_occurrences_json`.

### Testing

- **Runner:** pytest + pytest-django. Forced settings module: `vinta_schedule_api.settings.test` (via `pytest.ini --ds`).
- **Layout:** tests live in each app's `tests/` directory. Subdirs by concern (e.g. `tests/services/`, `tests/tasks/`).
- **Fixtures:** pytest fixtures in `conftest.py` (root + per-app). Available globally: `user`, `user_password`, `auth_client`, `anonymous_client`, `di_container`.
- **Factories:** `<app>/factories.py` (e.g. `users/factories.py`, `calendar_integration/factories.py`); use `model_bakery` for ad-hoc objects.
- **Parallelism:** `pytest -n auto` is the default. The `--reuse-db` variants (`make test`, `make test_seq`) skip rebuild for speed.
- **Coverage:** `pytest --cov=. --cov-report=html:junit/test-results.html` (see `pytest.ini`). CI uploads to Codecov.
- Write unit, integration, and functional tests for new code.

## Multi-Tenancy

Tenancy is enforced at the manager layer via `organizations` + `common`. Every model that holds tenant-scoped data inherits from the `OrganizationModel` abstract base.

- **Foreign keys to tenant-scoped models** use `OrganizationForeignKey` / `OrganizationOneToOneField` (from `common/`). These fields create **two** Django fields under the hood: a concrete field named `<name>_fk` (the actual FK column) and a `ForeignObject` named `<name>` that joins on both the FK and the organization FK. Queries through `<name>` automatically include the tenant scope.
- Foreign keys to non-tenant-scoped models use stock Django fields (`models.ForeignKey`, `models.OneToOneField`).
- **Every query against an `OrganizationModel` subclass must filter by organization.** The custom manager raises an exception when the filter is missing — this is the safety net, not a recommendation. Bypassing the manager (raw cursor, `.objects.raw(...)` without the org clause, `_meta.default_manager` tricks) defeats it and lands tenant data in the wrong tenant.
- Services, views, and GraphQL resolvers must propagate the organization context end-to-end. Background tasks (Celery) must hydrate the organization context from task arguments — never from request state.

## Environment Variables

Two example files exist because tooling runs from two surfaces:

- **`.env.example` → `.env`** — used when running tooling on the **host** (`uv run`, `pre-commit`, `manage.py` outside the container). Hostnames point at `localhost` via the docker-compose published ports.
- **`.env.docker.example` → `.env.docker`** — used **inside containers** (`make up`, `make bash`). Hostnames point at the docker-compose service names.

Variables (no values shown; copy from the example file):

```
DJANGO_SETTINGS_MODULE
CELERY_BROKER_URL
REDIS_URL
DATABASE_URL
FLOCI_ENDPOINT
FLOCI_EXTERNAL_ENDPOINT
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
S3_BUCKET_NAME
PYTHONBREAKPOINT
SALT_KEY
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
TWILIO_ACCOUNT_SID
TWILIO_API_KEY_SID
TWILIO_API_KEY_SECRET
TWILIO_AUTH_TOKEN
TWILIO_NUMBER
TWILIO_DEFAULT_BROADCAST_NUMBERS
```

Production-only vars (set via Render `envVarGroups`): `SECRET_KEY`, `SENTRY_DSN`, `SMTP_HOST`/`USERNAME`/`PASSWORD`, `ALLOWED_HOSTS`, `SITE_DOMAIN`, `API_DOMAIN`, `DEFAULT_BCC_EMAILS`, AWS bucket / CloudFront settings, `ENABLE_DJANGO_COLLECTSTATIC`, `AUTO_MIGRATE`. Adding a new env var requires updates to both example files and `render.yaml` envVarGroups — see the `add-env-var` skill.

## Local Storage Emulation — Floci

`docker-compose.yml` runs Floci (free open-source AWS S3 emulator) at `http://localhost:4566`. `make setup` initializes the dev bucket via `scripts/init_floci.py`. Default credentials: `test` / `test`. The `USE_FLOCI` setting (in `local.py`) switches between Floci (dev) and real AWS S3 (production).

## Error Tracking — Sentry

`sentry-sdk` is wired in `vinta_schedule_api/settings/`. Set `SENTRY_DSN` in production. Local dev does not emit to Sentry unless the DSN is set.

## Deployment

Not yet deployed. `render.yaml` is configured for Render (free plan), but no environment has been provisioned. When the first deploy happens:

| Environment | Trigger | Notes |
|---|---|---|
| development (local) | `make up` | docker-compose stack |
| production (future) | push to `main` after Render link | `render_build.sh` runs `uv sync`, Django checks, `collectstatic`, `migrate` |

Celery worker + beat are commented in `render.yaml` (no free Render Worker plan). Uncommenting incurs cost.

## Opinionated Settings (carry-overs worth knowing)

- **`DATABASES["default"]["ATOMIC_REQUESTS"] = True`** — every request runs in a transaction. **Consequence:** Celery tasks queued from a view must use `transaction.on_commit(lambda: my_task.delay())` to avoid a race where the worker picks up the task before the request transaction commits. Reference: https://www.vinta.com.br/blog/database-concurrency-in-django-the-right-way.
- **`CELERY_TASK_ACKS_LATE = True`** — tasks must be idempotent. Re-queueing on worker failure is expected.
- **Django-CSP** — `django-csp` configures CSP headers. Loading external resources (images, fonts, scripts) requires adding the source to the corresponding `CSP_*_SRC` setting in `vinta_schedule_api/settings/`. Only add trusted sources.

## Calendar Integration — Specifics

### Recurring events

Occurrences are calculated **in Postgres**, not in Python, from the master event's `recurrence_rule` and its rule exceptions. Functions: `calendar_integration/migrations/sql/functions/calculate_recurring_events` + `get_event_occurrences_json`. ORM access via `calendar_integration/database_functions.py`.

### Timezones

- Start / end date-times are stored in **UTC but timezone-unaware** — the wall-clock time of the event in its own timezone.
- Timezone is stored separately as an IANA string.
- Editable fields: `start_time_tz_unaware`, `end_time_tz_unaware`. **Do not use these in queries** — comparing them across events in different timezones produces wrong results.
- Use the generated fields `start_time` and `end_time` for queries. They apply `convert_naive_utc_to_timezone` so DST is respected.

### Calendar Bundles

- A **bundle calendar** (`CalendarType.BUNDLE`) groups multiple child calendars via `ChildrenCalendarRelationship`. Events created on the bundle propagate to children.
- Exactly one child is the **primary** (`is_primary=True`) and hosts the actual external event.
- On `create_event()` against a bundle:
  - The primary calendar gets the real `CalendarEvent` (created via Google / Outlook).
  - Internal child calendars get representation `CalendarEvent`s linked via `bundle_primary_event`.
  - Other-provider child calendars get `BlockedTime` entries (blocks the slot without creating an event).
- Availability checks across child calendars consider bundle events automatically.
- For updates / deletes, call `update_event()` / `delete_event()` with the bundle primary event.
- `get_calendar_events_expanded()` deduplicates bundle events automatically.
- Primary calendar selection is **explicit** at bundle creation: pass `primary_calendar` to `create_bundle_calendar()`.

## PR + Commit Conventions

- **Branch naming:** `feature/<slug>` (also accepted: `fix/<slug>`, `chore/<slug>`).
- **Commit messages:** Conventional Commits (`feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`).
- **AI co-author trailers in commits are forbidden.** No `Co-Authored-By: Claude ...` / `Copilot ...` / etc. Commits must read as human-authored.
- **Staging:** explicit paths only (`git add path/to/file.py path/to/other.py`). **Never `git add -A` / `git add .`** — the repo root holds untracked `.env`, `.env.docker`, generated `schema.yml` / `schema-auth.yml`, `.coverage`, `mailpit-data/`, and other locals that `-A` will sweep in.
- **PRs:** agents may push the branch and open the PR (`gh pr create`). Title should match the Conventional Commits style. Body should describe the change and link the related plan / spec under `ai-plans/` when one exists.

## Key Documentation

- `README.md` — setup, Docker, Floci, Render, opinionated settings.
- `docs/README.md`, `docs/glossary.md`, `docs/concepts/` — domain concepts.
- `pyproject.toml` — ruff + mypy + coverage config.
- `pytest.ini` — pytest config (forced test settings).
- `render.yaml` — production deploy config.
- `docker-compose.yml` + `Dockerfile.development` — dev runtime.
- `.pre-commit-config.yaml` — local hook chain.
- `ai-plans/` — feature specs and implementation plans (canonical layout).
- `ai-tools/skills/` — project skills.
- `ai-tools/agents/` — sub-agent definitions.
