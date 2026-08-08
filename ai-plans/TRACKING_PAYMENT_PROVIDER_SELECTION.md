# Payment Provider Selection — Tracking

## Implementation Progress

### Phase 1 — Add provider credential and default settings ✅ COMPLETE

**Completion date**: 2026-08-08

**Changes implemented**:
- Settings: Added `STRIPE_PUBLISHABLE_KEY`, `MERCADOPAGO_PUBLIC_KEY`, `DEFAULT_PAYMENT_PROVIDER`
- Settings: Added `"payment-provider": "120/min"` throttle scope
- Environment files: Updated `.env.example` and `.env.docker.example`
- Deployment config: Updated `render.yaml` with new env var groups
- CI: Updated `.github/workflows/main.yml` with fake values in all 5 job blocks
- Documentation: Updated `AGENTS.md` with env var descriptions
- Tests: Created `payments/tests/test_settings.py` with validation tests

**All gates passed**:
- `uv run ruff check ./` ✅
- `uv run ruff format --check ./` ✅
- `uv run pytest payments/tests/test_settings.py -vs` ✅
- `uv run python manage.py makemigrations --check` ✅
- `uv run python manage.py check --deploy` ✅
- `uv run pytest -n auto` ✅ (5162 tests passed)

### Phase 2 — Pin the provider on the BillingProfile ⏳ PENDING

### Phase 3 — Provider credentials endpoints ⏳ PENDING

### Phase 4 — Route provider calls through the resolved provider ⏳ PENDING
