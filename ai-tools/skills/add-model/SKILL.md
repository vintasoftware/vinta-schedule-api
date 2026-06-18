---
name: add-model
description: Add a new Django model in the Vinta Schedule API, following the project's multi-tenancy contract (OrganizationModel inheritance, OrganizationForeignKey for tenant-scoped relations), custom manager + queryset pattern, admin registration, and factory for tests. Use when adding a tenant-scoped or shared model to any app (accounts, calendar_integration, organizations, payments, public_api, webhooks, etc.). Skip for ad-hoc through-tables that don't deserve a manager; use a plain `class Meta` model + comment for those.
---

# Add Model

See [AGENTS.md → Multi-Tenancy](../../AGENTS.md#multi-tenancy), [AGENTS.md → Custom Managers and Querysets](../../AGENTS.md#custom-managers-and-querysets), and [AGENTS.md → Django Virtual Models](../../AGENTS.md#django-virtual-models) for the load-bearing rules. This skill covers the model-shape mechanics around those rules.

## Decision questions

Answer before writing the model:

1. **Tenant-scoped or shared?**
   - Tenant-scoped (per-organization) → inherit from `OrganizationModel`. Almost everything user-facing.
   - Shared (one row across all orgs) → inherit from `django.db.models.Model` or a project base in `common/models.py`. Example: `Organization` itself, system-wide configuration, country / currency reference data.
   - In doubt → tenant-scoped. Sharing a model across tenants later is harder than tenant-isolating it later.

2. **Which app does it belong in?** Use the existing app whose domain the model belongs to. New top-level domain → new app, but confirm with the team before creating one.

3. **Which existing model does it relate to?** Walk the FK / OneToOne / M2M graph. For each relation:
   - Other side is tenant-scoped → use `OrganizationForeignKey` / `OrganizationOneToOneField` from `common/`. These generate the `<name>_fk` concrete column + the `<name>` `ForeignObject` join that includes the org clause.
   - Other side is shared → use stock `models.ForeignKey` / `models.OneToOneField`.
   - M2M to a tenant-scoped model → use the through= explicit-through-model pattern with `OrganizationForeignKey` on both sides.

4. **Does the model need a custom manager + queryset?** Default `OrganizationModel.objects` already enforces the org filter. You need a custom one when:
   - There are domain-specific filter methods to expose (`for_active(...)`, `with_availability(...)`).
   - Reads commonly need a specific `select_related` / `prefetch_related` annotation.
   - A virtual model for DRF serialization will rely on a specific shape.

5. **Will DRF / GraphQL serialize it?** Plan a virtual model in `<app>/virtual_models.py` to avoid N+1.

6. **Admin?** Most models should be registered. Tenant-scoped admin needs to handle the org filter.

7. **Will Celery tasks touch it?** Confirm the task signature includes the organization id and hydrates the org context before any query.

## Checklist

For a tenant-scoped model `Foo` in app `bars`:

1. **`bars/models.py`** — model definition:

   ```python
   from django.db import models

   from common.models import OrganizationModel
   from common.fields import OrganizationForeignKey

   from bars.managers import FooManager

   class Foo(OrganizationModel):
       name = models.CharField(max_length=200)
       parent = OrganizationForeignKey(
           "bars.Parent",
           on_delete=models.CASCADE,
           related_name="foos",
       )
       is_active = models.BooleanField(default=True)
       created_at = models.DateTimeField(auto_now_add=True)
       updated_at = models.DateTimeField(auto_now=True)

       objects = FooManager()

       class Meta:
           db_table = "bars_foo"
           constraints = [
               models.UniqueConstraint(
                   fields=["organization", "name"],
                   name="bars_foo_unique_org_name",
               ),
           ]
           indexes = [
               models.Index(fields=["organization", "is_active"]),
           ]

       def __str__(self) -> str:
           return f"{self.name} ({self.organization_id})"
   ```

   - **Always include `organization` in unique / index keys.** A `unique=True` on `name` alone collides across tenants and ruins the migration on the first cross-tenant duplicate.
   - **Always lead composite indexes with `organization_id`** — every tenant-scoped query starts with the org filter.

2. **`bars/querysets.py`** — queryset class (chainable methods):

   ```python
   from django.db.models import QuerySet


   class FooQuerySet(QuerySet["Foo"]):
       def active(self) -> "FooQuerySet":
           return self.filter(is_active=True)

       def for_parent(self, parent_id: int) -> "FooQuerySet":
           return self.filter(parent_id=parent_id)
   ```

3. **`bars/managers.py`** — manager (use `from_queryset` so chainable methods are also manager methods):

   ```python
   from common.managers import OrganizationManager

   from bars.querysets import FooQuerySet


   FooManager = OrganizationManager.from_queryset(FooQuerySet)
   ```

   `OrganizationManager` is the project base that raises on missing org filter. Subclass / `from_queryset` it — never plain `models.Manager`.

4. **`bars/admin.py`** — admin registration:

   ```python
   from django.contrib import admin

   from bars.models import Foo


   @admin.register(Foo)
   class FooAdmin(admin.ModelAdmin):
       list_display = ("name", "organization", "parent", "is_active", "created_at")
       list_filter = ("organization", "is_active")
       search_fields = ("name",)
       autocomplete_fields = ("organization", "parent")
   ```

5. **`bars/factories.py`** — model-bakery / factory_boy factory:

   ```python
   from model_bakery import baker

   from bars.models import Foo


   def foo_factory(organization, **overrides) -> Foo:
       return baker.make(
           Foo,
           organization=organization,
           **overrides,
       )
   ```

   Always require `organization` as a positional / keyword arg; never default it. Tests that forget to pass org should fail loudly.

6. **`bars/migrations/`** — generate the migration:

   ```bash
   docker compose run --rm api uv run python manage.py makemigrations bars
   ```

   Inspect the generated file. For tenant-scoped FKs, both the `<name>_fk` concrete column and the `<name>` `ForeignObject` should be present.

7. **`bars/virtual_models.py`** — only if the model surfaces through DRF and the default queryset would N+1. Add the virtual model wired to the serializer.

8. **`bars/tests/test_foo_models.py`** — at minimum:
   - Creating a `Foo` without an organization raises.
   - The manager's `.active()` / `.for_parent(...)` methods filter correctly.
   - The unique constraint allows the same `name` in two different orgs.
   - `__str__` doesn't crash.

## Pitfalls

- **`models.ForeignKey` to another tenant-scoped model.** Reads through `obj.parent` will not include the organization clause; queries leak across tenants under joins. Use `OrganizationForeignKey`.
- **Naming the concrete field manually instead of via `OrganizationForeignKey`.** The framework names the concrete column `<name>_fk`; manual naming desyncs the `ForeignObject` join.
- **Forgetting `organization` in a `UniqueConstraint`.** Migration applies fine on a single-tenant DB. Production has multiple tenants → first collision raises `IntegrityError` at runtime, not at migrate-time.
- **Custom manager that doesn't inherit `OrganizationManager`.** Loses the missing-org-filter guard. Easy to spot in review — every tenant-scoped model's manager must trace back to `OrganizationManager`.
- **Querying through the `<name>_fk` concrete column directly** (e.g. `.filter(parent_fk=42)`). This skips the org clause. Use `.filter(parent=parent_obj)` or `.filter(parent_id=42)` through the `ForeignObject`.
- **Touching `Model._meta.default_manager` to bypass the missing-filter check** "just for this script". Use a real manager method that takes the org explicitly.
- **Skipping the admin.** Admin pages are also tenant-aware via Django's session-scoped staff context; missing admin = no operational visibility.
- **Forgetting to add the index on `(organization, hot_column)`.** Every read starts with the org filter; queries that filter only by `hot_column` will full-scan a multi-tenant table.

## Verification

Run the [outer gate](../../AGENTS.md#outer-gate) — must pass. Skill-specific extras:

```bash
# Migration applies cleanly + rolls back
docker compose run --rm api uv run python manage.py migrate bars            # apply
docker compose run --rm api uv run python manage.py migrate bars <prev>     # rollback
docker compose run --rm api uv run python manage.py migrate bars            # re-apply

# Scoped tests
docker compose run --rm api uv run pytest bars/tests/ -n auto
```

Spot-checks:
- [ ] Model inherits from `OrganizationModel` (or explicitly justified shared).
- [ ] Every FK / OneToOne to a tenant-scoped target uses `OrganizationForeignKey` / `OrganizationOneToOneField`.
- [ ] Every unique constraint includes `organization`.
- [ ] Every composite index leads with `organization`.
- [ ] Custom manager inherits from / uses `OrganizationManager`.
- [ ] Querysets are chainable + composable.
- [ ] Admin registered with org-aware filters.
- [ ] Factory requires `organization` (no default).
- [ ] Test asserts missing-org behavior.
- [ ] If serialized: virtual model added.
