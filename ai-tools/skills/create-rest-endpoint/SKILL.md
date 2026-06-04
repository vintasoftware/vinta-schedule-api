---
name: create-rest-endpoint
description: Add a REST endpoint to the Vinta Schedule API using DRF ViewSets, the project's `VintaScheduleModelViewSet` base, organization-scoped permissions, serializer-driven virtual models, optional django-filter filtersets, and drf-spectacular OpenAPI schema export. Wires the viewset, serializer, permissions, filterset, route registration in `<app>/routes.py`, and regenerates `schema.yml`. Use for internal REST endpoints consumed by the project's frontends or first-party integrations. For external-integration data, use [create-graphql-public-query](../create-graphql-public-query/SKILL.md).
---

# Create REST Endpoint

Background — load-bearing rules live in [AGENTS.md](../../AGENTS.md): [Multi-Tenancy](../../AGENTS.md#multi-tenancy) (every tenant-scoped queryset goes through `filter_by_organization`), [Dependency Injection](../../AGENTS.md#dependency-injection-di_core) (services via DI, never direct import), [Django Virtual Models](../../AGENTS.md#django-virtual-models) (serializer → virtual model when N+1 risk), [Custom Managers and Querysets](../../AGENTS.md#custom-managers-and-querysets) (no complex querysets inline).

This skill covers the DRF wire-up: viewset in `<app>/views.py`, serializer in `<app>/serializers.py`, permission in `<app>/permissions.py`, filterset in `<app>/filtersets.py`, route registration in `<app>/routes.py`. All routes are gathered by `DefaultRouter` in `vinta_schedule_api/urls.py`.

The OpenAPI schema export (`schema.yml`) is gated by a pre-commit hook + CI. After endpoint changes, regenerate the schema or the commit fails.

## Decision questions

1. **CRUD or custom action?**
   - Full CRUD on a model → `ModelViewSet` subclass via `VintaScheduleModelViewSet`.
   - Read-only listing → `ReadOnlyModelViewSet`.
   - One-off action (POST without a model) → `APIView` or a `@action` on an existing viewset.
2. **Which model does it expose?**
   - Tenant-scoped (`OrganizationModel`) → use `Model.objects.filter_by_organization(...)` in `get_queryset()`.
   - Shared → standard queryset; still confirm with the team.
3. **What auth + permission classes?** Existing pattern: per-resource permission class in `<app>/permissions.py` (e.g. `CalendarAvailabilityPermission`) layered on top of DRF's session / JWT auth.
4. **Does the serializer need a virtual model?** If the response includes nested or aggregate data that would N+1, yes — define it in `<app>/virtual_models.py` and reference it from the serializer.
5. **Filtering / search / ordering?** Use `django-filter` via a `FilterSet` in `<app>/filtersets.py`. DRF native `filter_backends` (`SearchFilter`, `OrderingFilter`) for search / ordering.
6. **Custom actions (`@action`)?** Each requires its own `@extend_schema` to populate OpenAPI correctly.
7. **Does the endpoint expose business logic?** Then services owned by DI. Inject via `Provide["service_name"]` in the viewset's `__init__` (rare) or in the serializer's create/update.

## Checklist

For a new endpoint `calendar-summaries` in `calendar_integration`:

1. **Define the serializer in `calendar_integration/serializers.py`:**
   ```python
   import django_virtual_models as v
   from rest_framework import serializers

   from calendar_integration.models import Calendar
   from calendar_integration.virtual_models import CalendarSummaryVirtualModel


   class CalendarSummarySerializer(serializers.ModelSerializer):
       event_count = serializers.IntegerField(read_only=True)
       last_event_end_time = serializers.DateTimeField(read_only=True)

       class Meta:
           model = Calendar
           fields = ("id", "name", "calendar_type", "event_count", "last_event_end_time")
           read_only_fields = fields
           virtual_model = CalendarSummaryVirtualModel
   ```

   Notes:
   - `virtual_model = ...` opts into `django-virtual-models` for queryset optimization.
   - Read-only response → `read_only_fields = fields`. Write endpoints declare writable fields explicitly.
   - Validation on write goes in `validate()` / `validate_<field>` methods, not in the view.

2. **Add the virtual model in `calendar_integration/virtual_models.py`** (only when the queryset would N+1):
   ```python
   import django_virtual_models as v

   from calendar_integration.models import Calendar


   class CalendarSummaryVirtualModel(v.VirtualModel):
       class Meta:
           model = Calendar
           deferred_fields = ["description"]   # don't fetch fields the serializer doesn't expose
   ```

3. **Add the permission class in `calendar_integration/permissions.py`** (if a new resource class):
   ```python
   from rest_framework.permissions import BasePermission


   class CalendarSummaryPermission(BasePermission):
       def has_permission(self, request, view) -> bool:
           if not request.user.is_authenticated:
               return False
           # Must belong to an organization
           return getattr(request.user, "organization_membership", None) is not None
   ```

   For most cases, reuse an existing permission class — only add a new one when the resource class is genuinely new.

4. **Add the filterset in `calendar_integration/filtersets.py`** (optional):
   ```python
   import django_filters

   from calendar_integration.models import Calendar


   class CalendarSummaryFilterSet(django_filters.FilterSet):
       calendar_type = django_filters.CharFilter(lookup_expr="iexact")
       has_events = django_filters.BooleanFilter(method="filter_has_events")

       class Meta:
           model = Calendar
           fields = ("calendar_type",)

       def filter_has_events(self, queryset, name, value):
           return queryset.filter(events__isnull=not value).distinct()
   ```

5. **Add the viewset in `calendar_integration/views.py`:**
   ```python
   from drf_spectacular.utils import OpenApiParameter, extend_schema
   from rest_framework import status
   from rest_framework.decorators import action
   from rest_framework.response import Response

   from calendar_integration.filtersets import CalendarSummaryFilterSet
   from calendar_integration.models import Calendar
   from calendar_integration.permissions import CalendarSummaryPermission
   from calendar_integration.serializers import CalendarSummarySerializer
   from common.utils.view_utils import VintaScheduleModelViewSet


   @extend_schema(tags=["Calendar Summaries"])
   class CalendarSummaryViewSet(VintaScheduleModelViewSet):
       """ViewSet exposing per-calendar event-count summaries."""

       permission_classes = (CalendarSummaryPermission,)
       queryset = Calendar.objects.all()
       serializer_class = CalendarSummarySerializer
       filterset_class = CalendarSummaryFilterSet
       http_method_names = ("get", "head", "options")   # read-only

       def get_queryset(self):
           user = self.request.user
           if not user.is_authenticated:
               return Calendar.objects.none()
           org_id = user.organization_membership.organization_id
           return Calendar.objects.filter_by_organization(org_id).with_summary()

       @extend_schema(
           parameters=[
               OpenApiParameter(name="event_type", required=False, type=str, location="query"),
           ],
           responses={200: CalendarSummarySerializer(many=True)},
       )
       @action(detail=False, methods=["get"], url_path="trending")
       def trending(self, request):
           qs = self.get_queryset().order_by("-event_count")[:10]
           return Response(self.get_serializer(qs, many=True).data, status=status.HTTP_200_OK)
   ```

   Notes:
   - `VintaScheduleModelViewSet` (from `common/utils/view_utils.py`) is the project base — provides the virtual-models hook + project-wide conventions. Use it.
   - `get_queryset()` hydrates the organization scope. The manager's `filter_by_organization` raises if missing — that's the safety net.
   - `with_summary()` is a queryset method on the manager, not inline annotation in the view.
   - `http_method_names` restricts allowed verbs.
   - `@extend_schema` provides OpenAPI metadata. Required for `@action` methods so drf-spectacular sees the param + response shape. Apply at the viewset level for tags / shared params.

6. **Register the route in `calendar_integration/routes.py`:**
   ```python
   from common.types import RouteDict

   from .views import (
       ...,
       CalendarSummaryViewSet,
   )


   routes: list[RouteDict] = [
       ...,
       {
           "regex": r"calendar-summaries",
           "viewset": CalendarSummaryViewSet,
           "basename": "CalendarSummaries",
       },
   ]
   ```

   The root URL conf (`vinta_schedule_api/urls.py`) imports per-app `routes` lists and registers each with the `DefaultRouter`. No edit needed at the project root.

7. **Regenerate the OpenAPI schema:**
   ```bash
   uv run python manage.py spectacular --color --file schema.yml
   ```

   Pre-commit hook (`backend-schema` in `.pre-commit-config.yaml`) verifies the schema is up-to-date. Run it before committing or CI fails.

8. **Tests** in `calendar_integration/tests/test_calendar_summary_endpoint.py`:
   - Authenticated user with org membership receives a 200 + serialized list.
   - Anonymous user receives 401.
   - User of org A receives only org A's data (cross-tenant isolation).
   - Filterset works: `?calendar_type=PRIMARY` filters correctly.
   - Custom `@action`: `/calendar-summaries/trending/` returns the right shape.
   - Virtual model prevents N+1: assert query count is bounded (use `django_assert_max_num_queries`).

## Pitfalls

(Multi-tenancy bypass + DI bypass + complex-inline-queryset are covered upstream — see [AGENTS.md → Multi-Tenancy](../../AGENTS.md#multi-tenancy) and the `reviewer` agent's BLOCKER classes. Skill-specific pitfalls below.)

- **Skipping `@extend_schema` on a `@action`.** drf-spectacular generates a generic / wrong schema for the custom endpoint; clients break on regen.
- **Forgetting `virtual_model = ...` on the serializer.** Reads still N+1 against the underlying queryset.
- **Not running `spectacular --file schema.yml` before commit.** Pre-commit hook fails. CI fails. PR blocked.
- **Permission class that doesn't check organization membership.** Endpoint returns 200 for any authenticated user, including users without an org context — and `get_queryset()` then crashes on `user.organization_membership.organization_id` instead of returning empty.
- **`http_method_names` left at default for read-only endpoints.** POST / PUT / DELETE return 405, but the schema advertises them. Restrict explicitly.
- **Filterset registered but not wired** — `filterset_class` attribute must be on the viewset, and `django_filters.rest_framework.DjangoFilterBackend` must be in DRF's `DEFAULT_FILTER_BACKENDS`. Check `vinta_schedule_api/settings/base.py` before adding.
- **Custom `@action` URL collides with an existing route.** Router resolves to first match; second route silently dead. Test routing.

## Verification

Run the [outer gate](../../AGENTS.md#outer-gate) — must pass. Skill-specific extras:

```bash
# Schema regenerated + diff is exactly the new endpoint
uv run python manage.py spectacular --color --file schema.yml
git diff schema.yml

# Endpoint reachable (smoke check)
DJANGO_SETTINGS_MODULE=vinta_schedule_api.settings.local uv run python manage.py shell -c "
from rest_framework.test import APIClient
from users.factories import UserFactory
client = APIClient()
user = UserFactory().create_user()
client.force_authenticate(user)
print(client.get('/api/calendar-summaries/').status_code)
"

# Scoped tests
uv run pytest <app>/tests/test_<name>_endpoint.py -vs
```

Spot-checks:
- [ ] Viewset inherits from `VintaScheduleModelViewSet` (not raw `ModelViewSet`).
- [ ] `get_queryset()` calls `Model.objects.filter_by_organization(...)` for tenant-scoped data.
- [ ] Permission class checks organization membership.
- [ ] Serializer wires `virtual_model = ...` when the response would N+1.
- [ ] Filterset declared in `<app>/filtersets.py` and `filterset_class` set on the viewset.
- [ ] `@extend_schema` decorates every `@action` and the viewset class itself (for tags / shared params).
- [ ] Route registered in `<app>/routes.py`.
- [ ] `schema.yml` regenerated; pre-commit hook passes.
- [ ] Tests cover: happy path, auth failure, tenant isolation, filtering, custom actions, N+1 bound.
- [ ] No business logic in the viewset (services injected via DI for any non-trivial work).
- [ ] No use of `start_time_tz_unaware` / `end_time_tz_unaware` in queryset filters (use generated `start_time` / `end_time`).
