---
name: create-graphql-public-query
description: Add a query or mutation to the public GraphQL API at `public_api/` in the Vinta Schedule API. Wires up the strawberry-django type in `<app>/graphql.py`, registers the field on `public_api/queries.py` (or `mutations.py`), applies the project's auth + organization-scope permission classes, maps the field to a resource in `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING`, and uses DI-injected services for business logic. Use when exposing internal data to external integrations. Skip for internal-only data — those go through the REST surface (`create-rest-endpoint`).
---

# Create GraphQL Public Query

Background — load-bearing rules in [AGENTS.md](../../AGENTS.md): [Public API — GraphQL](../../AGENTS.md#public-api--graphql) (resolver shape, registration, auth via `PublicAPIAuthService`), [Multi-Tenancy](../../AGENTS.md#multi-tenancy) (every tenant query goes through `filter_by_organization`), [Dependency Injection](../../AGENTS.md#dependency-injection-di_core) (mutation services injected via DI).

This skill covers the per-field wiring: type in `<app>/graphql.py`, resolver in `public_api/queries.py` or `public_api/mutations.py` (or per-app classes registered there), permission classes, `FIELD_TO_RESOURCE_MAPPING` registration.

Auth + tenancy non-optional. Every public-API field passes through `IsAuthenticated` + `OrganizationResourceAccess`. Skip either → tenant leak.

## Decision questions

1. **Query or mutation?**
   - Read → query field on `Query`.
   - Write (create / update / delete / side-effect) → mutation field on `Mutation`.
2. **Where does the type live?**
   - New type → `<app>/graphql.py` as a `@strawberry_django.type(Model)` (auto-mapped to a model) or `@strawberry.type` (custom shape).
   - Existing type → reuse from `<app>/graphql.py`.
3. **What's the field name in the GraphQL schema?**
   - Strawberry converts `def calendar_events` → `calendarEvents`. Add the camelCase form to `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` so the permission class can map it to a `PublicAPIResources` enum value.
4. **Does this require a new `PublicAPIResources` entry?** If the field exposes a new resource class (not just a new view onto an existing one), add the enum value in `public_api/constants.py` and the resource-to-permission wiring in `public_api/permissions.py`.
5. **Does the resolver depend on a service?** Use DI via `dependency-injector` — never direct-import a service. See `public_api/mutations.py:get_mutation_dependencies` for the pattern.
6. **Pagination?** Every list query MUST paginate. Default: `offset: int = 0, limit: int = 100` with validation (`limit ∈ (0, 100]`).
7. **Filtering?** Optional positional args on the resolver. Use the manager's chainable queryset methods, not inline `.filter()` calls.

## Checklist

### Query field — read

For a new query `calendar_summary` exposing per-calendar event counts in `calendar_integration`:

1. **Add the GraphQL type in `calendar_integration/graphql.py`** (if a fresh shape is needed):
   ```python
   import strawberry
   import strawberry_django


   @strawberry.type
   class CalendarSummaryGraphQLType:
       calendar_id: int
       calendar_name: str
       event_count: int
       last_event_end_time: datetime.datetime | None
   ```

   For model-backed types, prefer `@strawberry_django.type(Model)` so `strawberry.auto` resolves fields against the model definition (see existing types in `calendar_integration/graphql.py`).

2. **Import the type into `public_api/queries.py`** alongside the existing imports.

3. **Add the resolver to `Query`** in `public_api/queries.py`:
   ```python
   @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
   def calendar_summaries(
       self,
       info: strawberry.Info,
       calendar_id: int | None = None,
       offset: int = 0,
       limit: int = 100,
   ) -> list[CalendarSummaryGraphQLType]:
       """Get per-calendar event count summaries for the caller's organization."""
       org = _get_org(info)

       if offset < 0:
           raise GraphQLError("Offset must be non-negative")
       if limit <= 0 or limit > 100:
           raise GraphQLError("Limit must be between 1 and 100")

       queryset = Calendar.objects.filter_by_organization(org.id).with_summary()
       if calendar_id is not None:
           queryset = queryset.filter(id=calendar_id)

       return list(queryset[offset : offset + limit])
   ```

   Notes:
   - **`@strawberry_django.field`** (not `@strawberry.field`) when the resolver returns a model-backed list — it cooperates with `DjangoOptimizerExtension` for N+1 elimination.
   - **`permission_classes=[IsAuthenticated, OrganizationResourceAccess]`** — non-negotiable. Even read-only public data requires both.
   - **`_get_org(info)`** — helper that pulls the SystemUser's organization from the request context. Defined in `public_api/queries.py`.
   - **`Calendar.objects.filter_by_organization(org.id)`** — every tenant-scoped query starts with this. Manager raises if the org filter is missing.
   - **No inline complex querysets.** `.with_summary()` lives on the manager / queryset, not at the call site.
   - Validation errors raise `GraphQLError` — clients receive a structured error payload.

4. **Register the field-to-resource mapping** in `public_api/permissions.py`:
   ```python
   FIELD_TO_RESOURCE_MAPPING: ClassVar[dict[str, str]] = {
       ...,
       "calendarSummaries": PublicAPIResources.CALENDAR,   # reuses existing resource
   }
   ```

   The camelCase key matches the GraphQL field name. If the resolver exposes a new resource class, add a new entry to `PublicAPIResources` in `public_api/constants.py` first.

5. **Add the queryset method** in `calendar_integration/querysets.py`:
   ```python
   class CalendarQuerySet(QuerySet["Calendar"]):
       def with_summary(self) -> "CalendarQuerySet":
           return self.annotate(
               event_count=Count("events"),
               last_event_end_time=Max("events__end_time"),
           )
   ```

6. **Optional: add a virtual model** in `calendar_integration/virtual_models.py` if the optimizer doesn't get it right out of the box.

7. **Tests** in `calendar_integration/tests/test_calendar_graphql.py`:
   - Authenticated + authorized request returns expected shape.
   - Authenticated + unauthorized (system user lacks resource permission) returns auth error.
   - Anonymous request returns auth error.
   - Cross-organization isolation: org A's system user never sees org B's calendars.
   - Pagination validation (`limit=0`, `limit=101`, `offset=-1` all raise).

### Mutation — write

Mutations follow the same shape, on `Mutation` in `public_api/mutations.py` (or in a per-app `Mutations` class registered there). Key differences:

1. **Decorator:** `@strawberry.mutation` (no permission classes — they're enforced via the mutation body or via a separate decorator).

   Existing mutations check permissions inside the resolver using `PublicAPIAuthService`. Match that pattern — don't introduce a new mechanism.

2. **Service injection** via `@inject` + `Provide["service_name"]`:
   ```python
   @dataclass
   class MutationDependencies:
       calendar_group_service: CalendarGroupService


   @inject
   def get_mutation_dependencies(
       calendar_group_service: Annotated[
           CalendarGroupService | None,
           Provide["calendar_group_service"],
       ] = None,
   ) -> MutationDependencies:
       required = [calendar_group_service]
       if any(d is None for d in required):
           raise GraphQLError("Missing required dependency")
       return MutationDependencies(
           calendar_group_service=cast(CalendarGroupService, calendar_group_service),
       )


   @strawberry.mutation
   def create_calendar_group(
       self,
       info: strawberry.Info,
       name: str,
       primary_calendar_id: int,
   ) -> CalendarGroupGraphQLType:
       deps = get_mutation_dependencies()
       org = _get_org(info)
       group = deps.calendar_group_service.create_calendar_group(
           organization_id=org.id,
           name=name,
           primary_calendar_id=primary_calendar_id,
       )
       return group
   ```

3. **Register the service in `di_core/containers.py`** if it's new. See [AGENTS.md](../../AGENTS.md) → Dependency Injection.

4. **Per-app mutation classes** — `<app>/mutations.py` defines a `@strawberry.type` class (e.g. `CalendarGroupMutations`), then `Mutation(CalendarGroupMutations, ...)` inherits it in `public_api/mutations.py`. Match the existing pattern.

5. **Test the off-path:** failed auth, failed service call (`GraphQLError` propagation), tenant isolation (org A's mutation can't touch org B's data).

## Pitfalls

(Multi-tenancy bypass, DI bypass, timezone-unaware filters are covered upstream — see [AGENTS.md → Multi-Tenancy](../../AGENTS.md#multi-tenancy), [Dependency Injection](../../AGENTS.md#dependency-injection-di_core), [Calendar Integration → Timezones](../../AGENTS.md#timezones), and the `reviewer` agent's BLOCKER classes. Skill-specific below.)

- **Missing `OrganizationResourceAccess` permission class.** Field becomes reachable without org-scoped auth. Tenant leak.
- **Field not registered in `FIELD_TO_RESOURCE_MAPPING`.** Either the permission class crashes (KeyError) or falls open (depending on implementation — read it first). Always register.
- **List query without pagination.** Unbounded result sets. Always cap at `limit ≤ 100`.
- **Returning ORM models from a `@strawberry.type` resolver** when the type isn't `@strawberry_django.type(Model)`. Strawberry can't serialize; runtime error.
- **N+1 in nested types.** `DjangoOptimizerExtension` covers most cases; cross-table aggregates need a virtual model.
- **Mutation that doesn't hydrate org context from the request.** Pass `organization_id` explicitly to the service; never read from session middleware inside the resolver.

## Verification

Run the [outer gate](../../AGENTS.md#outer-gate) — must pass. Skill-specific extras:

```bash
# Schema introspects cleanly + shows the new field
docker compose run --rm -e DJANGO_SETTINGS_MODULE=vinta_schedule_api.settings.local api uv run python manage.py shell -c "
from public_api.schema import schema
print(str(schema))
" | grep -i "calendarSummaries"

# REST schema regenerated if mixed surface
docker compose run --rm api uv run python manage.py spectacular --color --file schema.yml

# Scoped tests
docker compose run --rm api uv run pytest <app>/tests/test_<name>_graphql.py -vs
```

Spot-checks:
- [ ] Type defined in `<app>/graphql.py`.
- [ ] Resolver on `Query` / `Mutation` in `public_api/` (or per-app class registered there).
- [ ] `permission_classes=[IsAuthenticated, OrganizationResourceAccess]` on queries.
- [ ] `FIELD_TO_RESOURCE_MAPPING` updated with the camelCase field name.
- [ ] New `PublicAPIResources` entry if a new resource class was introduced.
- [ ] Manager's `filter_by_organization` (or equivalent) in the queryset construction.
- [ ] Complex query logic on manager / queryset, not inline.
- [ ] DI-injected services for mutations; never direct import.
- [ ] List queries paginate; `limit` ≤ 100.
- [ ] Tests cover: happy path, auth failure, tenant isolation, pagination edge cases.
- [ ] No use of `start_time_tz_unaware` / `end_time_tz_unaware` in resolver filters.
