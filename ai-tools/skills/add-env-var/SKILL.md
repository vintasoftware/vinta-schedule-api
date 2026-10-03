---
name: add-env-var
description: Add a new environment variable end-to-end in the Vinta Schedule API. Covers every layer the var must reach — `.env.example`, `.env.docker.example`, Django settings, the ECS deploy (Terraform `container_environment` for config, the Secrets Manager key lists for credentials), the CI workflow, and the AGENTS.md env section. Use when adding a new secret, API key, feature toggle, or third-party integration credential. Skip for renames (use rename-env-var pattern) and for removals.
---

# Add Environment Variable

Adding an env var to this project touches **at least five files**. The runtime crosses two local surfaces (host and container), and the deployed environments run on AWS ECS/Fargate, which reads env vars from two different places. Skip a layer and something breaks:

- Missing in `.env.docker.example` → the container won't start.
- Missing in `vinta_schedule_api/settings/` → the value is silently `None`.
- Missing in the CI workflow env → lint and tests fail on push.
- Missing in Terraform → the deployed app crashes at startup (required var) or runs on the default (optional var).

## Before adding

1. **Is it really an env var?** Constants that never change between environments belong in `vinta_schedule_api/settings/base.py` or app-level constants modules. Tenant-scoped knobs belong on the `Organization` model.
2. **Is it a secret?** If yes, never commit a real value. Example files use placeholders / `apikey` / `test`.
3. **What's its scope?** Process-wide (most), per-request (use settings + middleware), per-tenant (use Organization fields).

## Decision questions

Answer before editing:

- **Var name.** UPPER_SNAKE_CASE. Prefix by provider when third-party (`TWILIO_AUTH_TOKEN`, `GOOGLE_CLIENT_ID`). No `DJANGO_` prefix unless it's a Django-recognized setting (e.g. `DJANGO_SETTINGS_MODULE`).
- **Required or optional?** Required → reading code uses `config("VAR", cast=...)` with no default. Optional → provide a sensible default. This choice also decides which Terraform list a secret goes in (see step 4).
- **Type.** `str` (default), `int`, `bool`, or `Csv()` for lists. `decouple` handles the casts.
- **Secret or config?** This decides where the deployed value lives:
  - **Config** (feature flags, domains, tuning knobs, anything safe to read in a plan diff) → the ECS task definition `environment`, built from `local.container_environment` in `infrastructure/modules/app-platform/ecs.tf`.
  - **Secret** (API keys, tokens, passwords, DSNs) → the one Secrets Manager secret per environment, keyed by the lists in `infrastructure/modules/app-platform/secrets.tf`.
- **Deployed-only?** Some vars (`SENTRY_DSN`, `SMTP_*`) only make sense when deployed. They go in Terraform and NOT in `.env.example`.
- **Local-only?** Some vars (`PYTHONBREAKPOINT`, `FLOCI_ENDPOINT`, `CELERY_TASK_ALWAYS_EAGER`) only make sense locally. They go in `.env.example` / `.env.docker.example` and NOT in Terraform.
- **AWS credentials?** Never add `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` to the deployed environment. boto3 reads the environment before the task role, so setting them overrides the role that grants S3 and SQS access.

## Checklist

For a typical var `MY_NEW_VAR` that's both local + deployed:

1. **`.env.example`** — append the var with a placeholder value matching the host-surface convention. Hostnames here point at `localhost`. Example: `MY_NEW_VAR=test` (for secrets) or `MY_NEW_VAR=http://localhost:1234` (for endpoints).

2. **`.env.docker.example`** — append the same var, but with the container-surface value. Hostnames here point at docker-compose service names (e.g. `redis://result:6379` instead of `redis://localhost:6379`). Example: `MY_NEW_VAR=test` (secrets are usually the same) or `MY_NEW_VAR=http://floci:4566` (endpoints differ).

3. **`vinta_schedule_api/settings/base.py`** (or the appropriate per-env settings file) — read the var via `decouple`:

   ```python
   from decouple import config

   MY_NEW_VAR = config("MY_NEW_VAR", cast=str)            # required str
   MY_NEW_VAR = config("MY_NEW_VAR", default="fallback")  # optional with default
   MY_NEW_VAR_TIMEOUT = config("MY_NEW_VAR_TIMEOUT", cast=int, default=30)
   MY_NEW_VAR_ENABLED = config("MY_NEW_VAR_ENABLED", cast=bool, default=False)
   ```

   Place the setting in the section that fits its purpose (third-party integration block, security block, etc.). If multiple settings files use it (`base.py` + `production.py` differ), put it in `base.py` and override in the more specific file only when needed.

4. **Terraform — the deployed value.** Pick the branch from the decision questions.

   **Config var** — add it to the `container_environment` map in `infrastructure/modules/app-platform/ecs.tf`. Every container (web, worker, beat, release) gets the same map. Values must be strings, so wrap non-strings with `tostring(...)` / `join(",", ...)`.
   - When the value differs per environment, add a typed variable to `infrastructure/modules/app-platform/variables.tf` (follow `account_phone_verification_enabled`: `description`, `type`, `default`, `nullable = false`) and set it in the `inputs` block of `infrastructure/environments/<env>/terragrunt.hcl` for each environment that needs a non-default value.
   - `extra_environment` (in an environment's `terragrunt.hcl` inputs) exists for one-off values in one environment. Don't use it for a var the app relies on everywhere; put that in the module.

     ```hcl
     # ecs.tf, inside local.container_environment
     MY_NEW_VAR_ENABLED = tostring(var.my_new_var_enabled)
     ```

   **Secret var** — add the key name (never a value) to one list in `infrastructure/modules/app-platform/secrets.tf`:
   - `required_secret_keys` when settings read it with **no default**. Django cannot import without it.
   - `default_optional_secret_keys` when settings read it **with a default**. An environment that doesn't use it can drop it via `disabled_secret_keys` in its `terragrunt.hcl`.
   - `extra_secret_keys` (an environment input) only for a key one environment needs and the module shouldn't own.

   Then add the key to the matching list under **1. Fill in the app secret** in `infrastructure/README.md`.

   **Deploy order matters for secrets.** Terraform seeds the secret once and then ignores changes, so the existing secret in each environment will NOT gain the new key on apply. ECS fails the *whole task* when a task definition names a key the secret lacks: no container starts, and the deploy reports `retrieved secret from Secrets Manager did not contain json key MY_NEW_VAR`. Before the deploy that ships the new task definition, an operator must run `infrastructure/scripts/sync-app-secret-keys.sh` (dry run, then `--apply`) against each environment and fill in the real value. **Don't run it yourself.** It writes to a deployed environment. Put it in the PR description as a pre-deploy step instead.

   Terraform runs through Terragrunt + Scalr (see `infrastructure/README.md`). Never run `terragrunt apply` from an agent session; the PR is the hand-off.

5. **`.github/workflows/main.yml`** — append the var with a placeholder value safe for CI to the **workflow-level `env:` block at the top of the file**. That is the single place: every job and step inherits from it, and the only per-job / per-step override is `DJANGO_SETTINGS_MODULE`. Pattern:

   ```yaml
   MY_NEW_VAR: 'FAKE_VAR_FOR_CI'
   ```

   A required var (no default) must be here, or `manage.py check --deploy` and the test shards fail at settings import. For secrets that must be real in CI (e.g. an integration test that hits a sandbox), wire from `${{ secrets.MY_NEW_VAR }}` and add the secret in the repo settings.

6. **`AGENTS.md`** — append the var name to the **Environment Variables** section's code-fence listing. No value, just the name. When it has non-obvious behavior (a rollout gate, a fail-closed default), add a bullet below the fence like the existing `ACCOUNT_PHONE_VERIFICATION_ENABLED` one. A deployed-only var goes into the matching sentence instead: the ECS task definition `environment` list for config, or the Secrets Manager list for credentials.

7. **Consumer code** — import settings (`from django.conf import settings`) and read `settings.MY_NEW_VAR`. Never `os.environ` / `os.getenv` outside `vinta_schedule_api/settings/`.

8. **Tests** — if the var has integration-test consequences, add fixtures in `conftest.py` that override it (`@pytest.fixture(autouse=True)` + `settings.MY_NEW_VAR = "..."` via `pytest-django`'s `settings` fixture, or `monkeypatch.setenv` for env-level overrides). For unit tests, `pytest.ini`'s `--ds=vinta_schedule_api.settings.test` keeps test-time defaults predictable; add the var to `vinta_schedule_api/settings/test.py` if its test default differs from `base.py`.

## Pitfalls

- **Forgetting `.env.docker.example`.** The container surface fails silently if the var is only in `.env.example`. Symptom: works on host (`uv run` outside docker), breaks inside `make bash`.
- **Forgetting Terraform.** Local dev and CI pass; the first staging deploy fails. A required var crashes the release task at settings import, and the deploy stops before any serving container is replaced. An optional var silently runs on its default.
- **Putting a secret in `container_environment`.** That map lands in the task definition in plain text and shows up in every plan diff. Credentials go in `secrets.tf`.
- **Shipping a new secret key without syncing the secret first.** ECS starts no container at all for that service. See the deploy-order note in step 4.
- **Putting a secret in the wrong list.** A key in `required_secret_keys` that settings actually read with a default can never be dropped per environment. A key in `default_optional_secret_keys` that settings read with no default makes `disabled_secret_keys` crash the container.
- **Reading from `os.environ` in app code.** Settings module is the single read point. Direct env reads bypass the cast + default machinery and produce string values where ints / bools were expected.
- **Re-declaring the var inside a job or step in `main.yml`.** The workflow-level `env:` block already reaches every job (`checks`, the sharded `test` matrix, `deploy-staging`). A job- or step-level copy silently shadows it and is one more place to update.
- **Putting a secret value in `.env.example`.** The example file is committed. Use `test` / `apikey` / a placeholder, never the real value.
- **Re-using `DJANGO_SETTINGS_MODULE` for app config.** Don't shadow framework env var names.
- **Adding the var to `base.py` and forgetting that `test.py` needs a deterministic override.** Tests run with `vinta_schedule_api.settings.test` by `pytest.ini` flag; if the var triggers behavior that breaks deterministic tests (e.g. live HTTP), give it a safe test-time default in `test.py`.

## Verification

Run the [outer gate](../../../AGENTS.md#outer-gate) — must pass. Skill-specific extras:

```bash
# Settings module loads with the new var
docker compose run --rm -e DJANGO_SETTINGS_MODULE=vinta_schedule_api.settings.local api uv run python -c "import django; django.setup(); from django.conf import settings; print(getattr(settings, 'MY_NEW_VAR', None))"

# Production-settings check passes with the new var injected
docker compose run --rm -e DJANGO_SETTINGS_MODULE=vinta_schedule_api.settings.production -e MY_NEW_VAR=fake api uv run python manage.py check --deploy

# Terraform still formats and parses (when terraform is installed)
terraform fmt -check -recursive infrastructure/modules

# Docker surface still boots
make down && make up && docker compose logs api | head -50
```

Check each of these in the diff:
- [ ] `.env.example` updated.
- [ ] `.env.docker.example` updated (matching key, container-surface value).
- [ ] `vinta_schedule_api/settings/base.py` (or specific file) reads via `decouple.config`.
- [ ] Config var: `container_environment` in `infrastructure/modules/app-platform/ecs.tf` updated, plus a variable in `variables.tf` and `infrastructure/environments/<env>/terragrunt.hcl` inputs when it varies per environment.
- [ ] Secret var: key added to the right list in `infrastructure/modules/app-platform/secrets.tf` and to `infrastructure/README.md`; the PR description names the pre-deploy `sync-app-secret-keys.sh` step.
- [ ] `.github/workflows/main.yml` workflow-level `env:` block includes the new var (once — no job- or step-level copies).
- [ ] `AGENTS.md` Environment Variables section lists the var.
- [ ] Consumer code reads `settings.MY_NEW_VAR`, not `os.environ`.
