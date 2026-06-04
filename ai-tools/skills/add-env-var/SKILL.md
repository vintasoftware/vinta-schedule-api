---
name: add-env-var
description: Add a new environment variable end-to-end in the Vinta Schedule API. Covers every layer the var must reach — `.env.example`, `.env.docker.example`, Django settings, Render production envVarGroups, CI workflow, AGENTS.md env section. Use when adding a new secret, API key, feature toggle, or third-party integration credential. Skip for renames (use rename-env-var pattern) and for removals.
---

# Add Environment Variable

Adding an env var to this project touches **at least four files** because the runtime crosses two surfaces (host + container) and the deploy is on Render. Skipping any layer breaks something: missing in `.env.docker.example` → container won't start; missing in `render.yaml` → production crashes on startup; missing in `vinta_schedule_api/settings/` → silently `None`; missing in CI workflow env → linting / tests fail on push.

## Before adding

1. **Is it really an env var?** Constants that never change between environments belong in `vinta_schedule_api/settings/base.py` or app-level constants modules. Tenant-scoped knobs belong on the `Organization` model.
2. **Is it a secret?** If yes, never commit a real value. Example files use placeholders / `apikey` / `test`.
3. **What's its scope?** Process-wide (most), per-request (use settings + middleware), per-tenant (use Organization fields).

## Decision questions

Answer before editing:

- **Var name.** UPPER_SNAKE_CASE. Prefix by provider when third-party (`TWILIO_AUTH_TOKEN`, `GOOGLE_CLIENT_ID`). No `DJANGO_` prefix unless it's a Django-recognized setting (e.g. `DJANGO_SETTINGS_MODULE`).
- **Required or optional?** Required → reading code must use `decouple.config('VAR', cast=...)` with no default. Optional → provide a sensible default.
- **Type.** `str` (default), `int`, `bool` (cast=`decouple.Csv()` for lists). `decouple` handles the casts.
- **Production only?** Some vars (`SENTRY_DSN`, `SMTP_*`) make sense only in production — they go in `render.yaml` envVarGroups, NOT in `.env.example`.
- **Local-only?** Some vars (`PYTHONBREAKPOINT`, `FLOCI_ENDPOINT`) make sense only locally — they go in `.env.example` / `.env.docker.example`, NOT in `render.yaml`.

## Checklist

For a typical var `MY_NEW_VAR` that's both local + production:

1. **`.env.example`** — append the var with a placeholder value matching the host-surface convention. Hostnames here point at `localhost`. Example: `MY_NEW_VAR=test` (for secrets) or `MY_NEW_VAR=http://localhost:1234` (for endpoints).

2. **`.env.docker.example`** — append the same var, but with the container-surface value. Hostnames here point at docker-compose service names (e.g. `redis://result:6379` instead of `redis://localhost:6379`). Example: `MY_NEW_VAR=test` (secrets are usually the same) or `MY_NEW_VAR=http://floci:4566` (endpoints differ).

3. **`vinta_schedule_api/settings/base.py`** (or the appropriate per-env settings file) — read the var via `python-decouple-typed`:

   ```python
   from decouple import config

   MY_NEW_VAR = config("MY_NEW_VAR", cast=str)            # required str
   MY_NEW_VAR = config("MY_NEW_VAR", default="fallback")  # optional with default
   MY_NEW_VAR_TIMEOUT = config("MY_NEW_VAR_TIMEOUT", cast=int, default=30)
   MY_NEW_VAR_ENABLED = config("MY_NEW_VAR_ENABLED", cast=bool, default=False)
   ```

   Place the setting in the section that fits its purpose (third-party integration block, security block, etc.). If multiple settings files use it (`base.py` + `production.py` differ), put it in `base.py` and override in the more specific file only when needed.

4. **`render.yaml`** — add the var under the appropriate envVarGroup. For secrets, set `sync: false` (Render hides the value from public manifest). For values shared across services, use the existing groups (`python-services`, `integrations-credentials`); create a new group only if the var belongs to a new logical bundle.

   ```yaml
   envVarGroups:
     - name: integrations-credentials
       envVars:
         - key: MY_NEW_VAR
           sync: false           # for secrets
         - key: MY_NEW_OPTIONAL
           value: "default"      # for non-secret defaults
   ```

5. **`.github/workflows/main.yml`** — append the var to every step that runs `manage.py`, `pytest`, `ruff`, or `check --deploy` with a placeholder value safe for CI. Pattern: under each `env:` block for those steps, add:

   ```yaml
   MY_NEW_VAR: 'FAKE_VAR_FOR_CI'
   ```

   For secrets that must be real in CI (e.g. an integration test that hits a sandbox), wire from `${{ secrets.MY_NEW_VAR }}` and add the secret in the repo settings.

6. **`ai-tools/AGENTS.md`** — append the var name to the **Environment Variables** section's listing. No value, just the name. Production-only vars go to the production-vars sentence below the main code fence.

7. **Consumer code** — import settings (`from django.conf import settings`) and read `settings.MY_NEW_VAR`. Never `os.environ` / `os.getenv` outside `vinta_schedule_api/settings/`.

8. **Tests** — if the var has integration-test consequences, add fixtures in `conftest.py` that override it (`@pytest.fixture(autouse=True)` + `settings.MY_NEW_VAR = "..."` via `pytest-django`'s `settings` fixture, or `monkeypatch.setenv` for env-level overrides). For unit tests, `pytest.ini`'s `--ds=vinta_schedule_api.settings.test` keeps test-time defaults predictable; add the var to `vinta_schedule_api/settings/test.py` if its test default differs from `base.py`.

## Pitfalls

- **Forgetting `.env.docker.example`.** The container surface fails silently if the var is only in `.env.example`. Symptom: works on host (`uv run` outside docker), breaks inside `make bash`.
- **Forgetting `render.yaml`.** Local dev works; first prod deploy crashes at startup. Render gives no warning at link time — the var just isn't there.
- **Reading from `os.environ` in app code.** Settings module is the single read point. Direct env reads bypass the cast + default machinery and produce string values where ints / bools were expected.
- **Skipping `.github/workflows/main.yml` env blocks.** CI runs `ruff` + `pytest` + `manage.py check --deploy` + `makemigrations --check` — each step has its own `env:` block. Forgetting one means CI fails on push for the env var, not the actual change.
- **Putting a secret value in `.env.example`.** The example file is committed. Use `test` / `apikey` / a placeholder, never the real value.
- **Re-using `DJANGO_SETTINGS_MODULE` for app config.** Don't shadow framework env var names.
- **Adding the var to `base.py` and forgetting that `test.py` needs a deterministic override.** Tests run with `vinta_schedule_api.settings.test` by `pytest.ini` flag; if the var triggers behavior that breaks deterministic tests (e.g. live HTTP), give it a safe test-time default in `test.py`.

## Verification

Run the [outer gate](../../AGENTS.md#outer-gate) — must pass. Skill-specific extras:

```bash
# Settings module loads with the new var
DJANGO_SETTINGS_MODULE=vinta_schedule_api.settings.local uv run python -c "import django; django.setup(); from django.conf import settings; print(getattr(settings, 'MY_NEW_VAR', None))"

# Production-settings check passes with the new var injected
DJANGO_SETTINGS_MODULE=vinta_schedule_api.settings.production MY_NEW_VAR=fake uv run python manage.py check --deploy

# Docker surface still boots
make down && make up && docker compose logs api | head -50
```

Check each of these in the diff:
- [ ] `.env.example` updated.
- [ ] `.env.docker.example` updated (matching key, container-surface value).
- [ ] `vinta_schedule_api/settings/base.py` (or specific file) reads via `decouple.config`.
- [ ] `render.yaml` envVarGroups updated (or `sync: false` for secrets).
- [ ] `.github/workflows/main.yml` env blocks for `ruff`, `pre-commit`, `makemigrations`, `check --deploy`, `pytest` all include the new var.
- [ ] `ai-tools/AGENTS.md` Environment Variables section lists the var.
- [ ] Consumer code reads `settings.MY_NEW_VAR`, not `os.environ`.
