# CalendarGroup Implementation Plan

## Goal

Introduce a `CalendarGroup` aggregate that lets a tenant define **availability sections**, each holding a *pool* of `Calendar`s, so a single booking can be made by selecting **one (or more) calendar from each section** while guaranteeing all selected calendars are simultaneously available.

### Driving example (clinic appointments)

A patient books an `Appointment`. The clinic has:

- **Section "Physicians"** — pool of physician personal calendars.
- **Section "Rooms"** — pool of resource calendars (consult rooms).

When the patient picks a time slot, the system must show only slots where **at least one physician AND at least one room** are free, and the booking persists which physician + which room were chosen.

## Naming proposal

The user proposed `CalendarGroup` + `availability sections`. Suggested final names:

| Concept | Name | Rationale |
|---|---|---|
| The aggregate template | `CalendarGroup` | Matches user's term; describes the bookable composite. |
| A required slot inside the group, holding a pool of candidate calendars | `CalendarGroupSlot` | "Section" is ambiguous (UI section?); "Slot" reads as "a role to fill" — more precise for the booking domain. Alternative: `CalendarGroupRequirement`. |
| Membership of a `Calendar` in a slot's pool | `CalendarGroupSlotMembership` (M2M `through`) | Standard Django convention. |
| A booked event tied to a group | reuse `CalendarEvent` + new `CalendarEventGroupSelection` | Avoids forking event model; selections are per-slot side records. |

> **Decision point for the user**: confirm `CalendarGroupSlot` vs `CalendarGroupSection`. Plan uses `Slot` below; rename is mechanical.

---

## Models

All new models live in [calendar_integration/models.py](calendar_integration/models.py) and inherit from `OrganizationModel` for multi-tenancy.

### 1. `CalendarGroup`

```python
class CalendarGroup(OrganizationModel):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    # No recurrence/no scheduling fields — the group is a template, not an event.

    objects = CalendarGroupManager()

    slots: "RelatedManager[CalendarGroupSlot]"
    events: "RelatedManager[CalendarEvent]"  # via reverse FK added below

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "name"),
                name="calendargroup_unique_name_per_org",
            ),
        ]
```

### 2. `CalendarGroupSlot`

```python
class CalendarGroupSlot(OrganizationModel):
    group = OrganizationForeignKey(
        CalendarGroup, on_delete=models.CASCADE, related_name="slots",
    )
    name = models.CharField(max_length=255)  # e.g. "Physicians", "Rooms"
    description = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    # How many calendars from the pool MUST be picked when booking.
    # Default 1 — clinic case picks 1 physician + 1 room.
    # Allow >1 for cases like "two nurses required".
    required_count = models.PositiveSmallIntegerField(default=1)

    calendars: "models.ManyToManyField[Calendar, CalendarGroupSlotMembership]" = models.ManyToManyField(
        Calendar,
        through="CalendarGroupSlotMembership",
        through_fields=("slot", "calendar"),
        related_name="group_slots",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("group", "name"),
                name="calendargroupslot_unique_name_per_group",
            ),
        ]
        ordering = ("order", "id")
```

### 3. `CalendarGroupSlotMembership`

```python
class CalendarGroupSlotMembership(OrganizationModel):
    slot = OrganizationForeignKey(
        CalendarGroupSlot, on_delete=models.CASCADE, related_name="memberships",
    )
    calendar = OrganizationForeignKey(
        Calendar, on_delete=models.CASCADE, related_name="group_slot_memberships",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("slot", "calendar"),
                name="calendargroupslotmembership_unique",
            ),
        ]
```

### 4. Extension to `CalendarEvent`

Add an optional FK so events can be tagged as "this booking belongs to this group":

```python
# inside CalendarEvent
calendar_group = OrganizationForeignKey(
    "CalendarGroup",
    on_delete=models.PROTECT,  # don't allow deleting a group with bookings
    null=True,
    blank=True,
    related_name="events",
)
```

> The event's existing `calendar_fk` continues to point at one of the selected calendars (the "primary" — per slot ordering, typically the first slot's pick). All other selected calendars are recorded via the model below. The existing `ResourceAllocation` is event-centric and not group-aware, so we keep it untouched and use the new model for group selections.

### 5. `CalendarEventGroupSelection`

Records which calendars were chosen for each slot of a grouped booking.

```python
class CalendarEventGroupSelection(OrganizationModel):
    event = OrganizationForeignKey(
        "CalendarEvent", on_delete=models.CASCADE, related_name="group_selections",
    )
    slot = OrganizationForeignKey(
        CalendarGroupSlot, on_delete=models.PROTECT, related_name="selections",
    )
    calendar = OrganizationForeignKey(
        Calendar, on_delete=models.PROTECT, related_name="group_selections",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("event", "slot", "calendar"),
                name="calendareventgroupselection_unique",
            ),
        ]
```

Invariants enforced in the service layer (with DB checks where feasible):
- Every `event` with `calendar_group` set has selections covering each of its group's slots, with `count(selections per slot) >= slot.required_count`.
- Each selected `calendar` belongs to that slot's membership pool.
- `event.calendar_fk` is one of the selected calendars (the "primary"; default = pick from the lowest-`order` slot).

---

## Manager / QuerySet additions

### `CalendarGroupQuerySet` ([calendar_integration/querysets.py](calendar_integration/querysets.py))

Method signature mirrors the existing `CalendarQuerySet.only_calendars_available_in_ranges` (querysets.py:212-282) for consistency.

```python
class CalendarGroupQuerySet(BaseOrganizationModelQuerySet):
    def only_groups_bookable_in_ranges(
        self,
        ranges: Iterable[tuple[datetime.datetime, datetime.datetime]],
    ) -> "CalendarGroupQuerySet":
        """
        Returns groups where, for every range, every slot has at least
        `required_count` calendars from its pool that are available
        (per `CalendarQuerySet.only_calendars_available_in_ranges`).
        """
```

**Strategy** (single SQL round-trip per range, composed with `Q`):

For each `range_i = (start_i, end_i)`:
- Compute the set `available_calendar_ids_i` = `Calendar.objects.only_calendars_available_in_ranges([range_i]).values("id")` — already-tested logic.
- For each slot `s`, the slot is satisfied iff `count(s.calendars ∩ available_calendar_ids_i) >= s.required_count`.
- The group is satisfied for this range iff **all** its slots are satisfied.

Implementation sketch (uses `Exists` + `Subquery` + a `HAVING`-style annotation):

```python
def only_groups_bookable_in_ranges(self, ranges):
    if not ranges:
        return self.none()

    qs = self
    for start, end in ranges:
        available_ids_subq = (
            Calendar.objects
            .only_calendars_available_in_ranges([(start, end)])
            .values("id")
        )

        # Per-slot count of available calendars from its membership pool.
        # A group is bookable when NO slot is "under-staffed" for this range.
        unsatisfied_slot_exists = CalendarGroupSlot.objects.filter(
            group_fk_id=OuterRef("id"),
        ).annotate(
            available_in_slot=Count(
                "memberships",
                filter=Q(memberships__calendar_fk_id__in=Subquery(available_ids_subq)),
                distinct=True,
            ),
        ).filter(available_in_slot__lt=F("required_count"))

        qs = qs.filter(~Exists(unsatisfied_slot_exists))
    return qs
```

> **Note on recurring events**: `Calendar.objects.only_calendars_available_in_ranges` already accounts for recurring events via `annotate_recurring_occurrences_on_date_range`. By delegating, we inherit recurrence correctness for free. We should also add `*_with_bulk_modifications` parity in a follow-up — out of scope for v1.

### `CalendarGroupSlotQuerySet`

Add convenience helpers (used by services and GraphQL resolvers):

```python
class CalendarGroupSlotQuerySet(BaseOrganizationModelQuerySet):
    def with_available_calendars_in_range(
        self, start: datetime.datetime, end: datetime.datetime,
    ) -> "CalendarGroupSlotQuerySet":
        """Annotate `available_calendar_ids` (list[int]) per slot for a single range."""

    def is_satisfied_in_range(self, start, end) -> "CalendarGroupSlotQuerySet":
        """Filter slots where len(available calendars) >= required_count."""
```

### Managers

Add `CalendarGroupManager`, `CalendarGroupSlotManager`, `CalendarGroupSlotMembershipManager`, `CalendarEventGroupSelectionManager` in [calendar_integration/managers.py](calendar_integration/managers.py), each subclassing `BaseOrganizationModelManager` and exposing the queryset methods (mirroring the `CalendarManager.only_calendars_available_in_ranges` proxy at managers.py:109-118).

---

## Service layer

New module: `calendar_integration/services/calendar_group_service.py`. Mirrors the boundaries of `calendar_service.py`.

### Dataclasses (in `services/dataclasses.py`)

```python
@dataclass
class CalendarGroupSlotInputData:
    name: str
    calendar_ids: list[int]
    required_count: int = 1
    description: str = ""
    order: int = 0

@dataclass
class CalendarGroupInputData:
    name: str
    description: str = ""
    slots: list[CalendarGroupSlotInputData] = dataclass_field(default_factory=list)

@dataclass
class CalendarGroupSlotSelectionInputData:
    slot_id: int
    calendar_ids: list[int]  # length must equal slot.required_count

@dataclass
class CalendarGroupEventInputData(CalendarEventInputData):
    """CalendarEventInputData + per-slot calendar selections."""
    group_id: int
    slot_selections: list[CalendarGroupSlotSelectionInputData] = dataclass_field(default_factory=list)
```

### `CalendarGroupService`

Public surface (each method wrapped in `@transaction.atomic()` like `CalendarService.create_event` at calendar_service.py:1005):

| Method | Purpose |
|---|---|
| `create_group(data: CalendarGroupInputData) -> CalendarGroup` | Validates: every `calendar_id` belongs to the org, no slot has an empty pool, no calendar duplicated within a single slot. Allows the same calendar to appear in multiple slots (a physician *can* also be a "supervisor" in another slot — domain choice; flag with a `clean()` warning if undesired). |
| `update_group(group_id, data) -> CalendarGroup` | Reconciles slots/memberships. Refuses to remove a calendar from a slot if there are **future** bookings selecting it (or requires a `force=True` flag — pick one in API design). |
| `delete_group(group_id)` | Refuses if any future bookings exist (`PROTECT` FK supports this). |
| `get_group_events(group_id, start, end) -> QuerySet[CalendarEvent]` | `CalendarEvent.objects.filter(calendar_group_fk_id=group_id)` over the range, including recurrence expansion via existing `annotate_recurring_occurrences_on_date_range`. |
| `check_group_availability(group_id, ranges) -> dict[range, dict[slot_id, list[Calendar]]]` | Returns, per requested range, per slot, which calendars from the pool are available. Use `CalendarQuerySet.only_calendars_available_in_ranges` per range; intersect with each slot's pool. Empty list for a slot ⇒ that range is not bookable. |
| `find_bookable_slots(group_id, search_window, duration, slot_step="15min") -> list[BookableSlotProposal]` | Walks `search_window` in `slot_step` increments and returns the timeslots where every slot is satisfied. (v1 may be Python-side; optimize later with a SQL window-based generator.) |
| `create_grouped_event(data: CalendarGroupEventInputData) -> CalendarEvent` | (1) Validate `group_id` belongs to org. (2) Validate selections cover each slot with `len(calendar_ids) >= slot.required_count`. (3) Validate every selected calendar is in its slot's pool. (4) Validate every selected calendar is available for `(start_time, end_time)` via `Calendar.objects.only_calendars_available_in_ranges`. (5) Pick "primary" calendar = first selection of lowest-`order` slot; set `event.calendar_fk = primary`. (6) Delegate to `CalendarService.create_event` for the primary calendar (so existing side-effects, permissions, adapter sync run unchanged). (7) Set `event.calendar_group = group`. (8) Bulk-create `CalendarEventGroupSelection` rows. (9) For non-primary selected calendars: create `BlockedTime` entries linked back to the event (or, for personal calendars with their own provider, create child events via the existing **bundle** mechanism in `_create_bundle_event` — see calendar_service.py:1037). Decision below. |

#### Multi-calendar persistence: `BlockedTime` vs. bundle child events

Two viable strategies; pick one and document:

**Option A — Reuse the existing bundle mechanism** (preferred if it covers our needs).
- Pros: Already syncs to external providers (Google/Microsoft), already handles RSVP, attendees, and recurring updates uniformly.
- Cons: Bundles are currently designed around a single "bundle calendar" parent (see `ChildrenCalendarRelationship` at models.py:151-166). We'd need to either (a) auto-create a `CalendarType.BUNDLE` parent per group + selection, or (b) extend bundle creation to accept an ad-hoc set of child calendars per booking.

**Option B — `BlockedTime` on non-primary selections, `CalendarEvent` only on primary**.
- Pros: Simple, no schema churn on bundles. Other calendars show "busy" without polluting their event lists.
- Cons: Non-primary owners don't see the event details on their calendar; harder to model RSVPs for selected physicians.

**Recommendation**: Start with **Option B** for v1 (smaller blast radius, no bundle refactor). Add a follow-up to migrate to bundle-based representation once bundles support per-event ad-hoc children.

---

## API (GraphQL)

[calendar_integration/graphql.py](calendar_integration/graphql.py) is the API surface. Add:

### Types

```python
@strawberry_django.type(CalendarGroup)
class CalendarGroupGraphQLType:
    id: strawberry.auto
    name: strawberry.auto
    description: strawberry.auto
    slots: list["CalendarGroupSlotGraphQLType"]
    created: datetime.datetime
    modified: datetime.datetime

@strawberry_django.type(CalendarGroupSlot)
class CalendarGroupSlotGraphQLType:
    id: strawberry.auto
    name: strawberry.auto
    description: strawberry.auto
    order: strawberry.auto
    required_count: strawberry.auto
    calendars: list[CalendarGraphQLType]

@strawberry_django.type(CalendarEventGroupSelection)
class CalendarEventGroupSelectionGraphQLType:
    id: strawberry.auto
    slot: CalendarGroupSlotGraphQLType
    calendar: CalendarGraphQLType
```

Add `group_selections: list[...]` and `calendar_group: CalendarGroupGraphQLType | None` to `CalendarEventGraphQLType`.

### Queries

- `calendar_group(id) -> CalendarGroup`
- `calendar_groups(filters) -> list[CalendarGroup]`
- `calendar_group_availability(group_id, ranges) -> CalendarGroupAvailabilityGraphQLType`
- `calendar_group_bookable_slots(group_id, search_window, duration, slot_step) -> list[BookableSlotProposalType]`

### Mutations (in [calendar_integration/mutations.py](calendar_integration/mutations.py))

- `create_calendar_group(input: CalendarGroupInput) -> CalendarGroup`
- `update_calendar_group(id, input) -> CalendarGroup`
- `delete_calendar_group(id) -> bool`
- `create_calendar_group_event(input: CalendarGroupEventInput) -> CalendarEvent`

All mutations enforce permissions via `CalendarPermissionService` (extend it with `can_manage_calendar_group(group)` — likely "user is org admin" or "owns at least one calendar in the group").

---

## API (Internal REST)

The project also exposes a Django REST Framework viewset-based API for internal (session-authenticated) clients, under `calendar_integration/views.py`, `calendar_integration/serializers.py`, `calendar_integration/filtersets.py` and `calendar_integration/routes.py`. We add `CalendarGroup` endpoints here so internal UIs don't have to go through GraphQL.

### Virtual models ([calendar_integration/virtual_models.py](calendar_integration/virtual_models.py))

Add `CalendarGroupSlotMembershipVirtualModel`, `CalendarGroupSlotVirtualModel`, `CalendarGroupVirtualModel`, and `CalendarEventGroupSelectionVirtualModel` mirroring the existing pattern (so `VirtualModelSerializer` can prefetch efficiently). Extend `CalendarEventVirtualModel` with `group_selections` and `calendar_group` relations.

### Serializers ([calendar_integration/serializers.py](calendar_integration/serializers.py))

- `CalendarGroupSlotMembershipSerializer` — read-only nested representation (`id`, `calendar`).
- `CalendarGroupSlotSerializer` — nested slot representation with `id`, `name`, `description`, `order`, `required_count`, and a writable `calendar_ids: list[int]` alongside the read-only `calendars` field.
- `CalendarGroupSerializer` — `id`, `name`, `description`, nested `slots`, timestamps. On `create`/`update`, delegates to `CalendarGroupService.create_group` / `update_group`, converting nested slot payloads to `CalendarGroupInputData`.
- `CalendarEventGroupSelectionSerializer` — read-only, `id`, `slot`, `calendar`.
- `CalendarGroupEventCreateSerializer` — write-only input for creating a grouped event. Fields: `group_id`, `title`, `description`, `start_time`, `end_time`, `timezone`, `slot_selections: list[{slot_id, calendar_ids: list[int]}]`, `attendances`, `external_attendances`. On `save()`, delegates to `CalendarGroupService.create_grouped_event` and returns the full `CalendarEvent`. The response is serialized with `CalendarEventSerializer`.
- `CalendarGroupAvailabilityRangeSerializer` / `CalendarGroupSlotAvailabilitySerializer` — output for the availability action.
- `BookableSlotProposalSerializer` — output for the bookable-slots action.

Service errors (`CalendarGroupValidationError`, `CalendarGroupSlotInUseError`, `CalendarGroupHasFutureEventsError`) are translated to DRF `ValidationError` with a per-exception `non_field_errors` payload.

### Permissions ([calendar_integration/permissions.py](calendar_integration/permissions.py))

Add `CalendarGroupPermission`: authenticated user with an active `OrganizationMembership` matching the group's organization. For object-level access we check that the user owns at least one calendar in the group (via `CalendarOwnership`) — mirroring the "likely permission model" noted above. Org-admin override is a follow-up.

### FilterSet ([calendar_integration/filtersets.py](calendar_integration/filtersets.py))

`CalendarGroupFilterSet` — `name` (icontains), `calendar` (ModelChoiceFilter — returns groups whose slots include that calendar).

### ViewSet ([calendar_integration/views.py](calendar_integration/views.py))

`CalendarGroupViewSet(VintaScheduleModelViewSet)`:

- Standard CRUD: `list`, `retrieve`, `create`, `update`, `partial_update`, `destroy`. CRUD methods mirror the webhook-mutations pattern — the serializer delegates to `CalendarGroupService`. `destroy` raises `ValidationError` when the service raises `CalendarGroupHasFutureEventsError`.
- Custom actions:
  - `POST /calendar-groups/{id}/events/` (`url_path="events"`, `url_name="create-event"`) — accepts `CalendarGroupEventCreateSerializer`; returns the created `CalendarEvent`.
  - `GET /calendar-groups/{id}/events/?start_datetime=…&end_datetime=…` (`url_path="events"`, `url_name="list-events"`, `methods=["GET"]`) — lists grouped events in range. Note that a single `@action` can accept both `GET` and `POST`; we'll keep them split for clarity.
  - `POST /calendar-groups/{id}/availability/` — body is `{ "ranges": [{"start_time", "end_time"}, ...] }`, returns per-range, per-slot availability. `POST` to avoid long query-string range payloads.
  - `GET /calendar-groups/{id}/bookable-slots/?search_window_start&search_window_end&duration_seconds&slot_step_seconds` — returns `[BookableSlotProposal]`.

Each action authenticates the underlying `CalendarService`/`CalendarGroupService` via `initialize_without_provider(organization=user.organization_membership.organization)` — the grouped-event flow needs provider adapters when the primary calendar is Google/Microsoft, so the viewset uses `authenticate(account=social_account, organization=…)` when a matching `SocialAccount` exists for the primary calendar's provider.

### Routes ([calendar_integration/routes.py](calendar_integration/routes.py))

Register `CalendarGroupViewSet` under `r"calendar-groups"` with basename `CalendarGroups`.

### Testing

- `calendar_integration/tests/test_views_calendar_group.py`:
  - CRUD happy paths (`list`, `retrieve`, `create`, `update`, `destroy`).
  - `create` validation surfaces from the service (duplicate slot name, cross-org calendar, empty pool).
  - `destroy` blocked by `CalendarGroupHasFutureEventsError`.
  - `events` POST: creates grouped event, returns `CalendarEvent` payload, persists `CalendarEventGroupSelection` rows.
  - `events` GET: returns events scoped to the group within the range.
  - `availability` POST returns per-range slot availability.
  - `bookable-slots` GET returns proposals.
  - Object-level permission blocks users who don't own any of the group's calendars.

---

## Migrations

Single new migration `calendar_integration/migrations/00XX_calendar_group.py` containing:

1. `CalendarGroup`
2. `CalendarGroupSlot`
3. `CalendarGroupSlotMembership`
4. `CalendarEventGroupSelection`
5. Add nullable `CalendarEvent.calendar_group_fk` (and `_id` shadow per the project's `OrganizationForeignKey` convention — see querysets.py:192-210 for the `_fk` rewrite pattern in `update()`).

No data backfill needed — existing events have no group.

---

## Testing

Follow the project pattern: `pytest`, `model_bakery`, fixtures mirroring [calendar_integration/tests/test_querysets.py](calendar_integration/tests/test_querysets.py).

### New test files

1. `calendar_integration/tests/test_calendar_group_models.py`
   - `CalendarGroup`, `CalendarGroupSlot`, membership uniqueness, `__str__`.
   - Constraint violations (duplicate slot name in group, duplicate calendar in slot).

2. `calendar_integration/tests/test_calendar_group_querysets.py`
   - `only_groups_bookable_in_ranges`:
     - Single range, all slots have ≥1 available → group included.
     - Single range, one slot has 0 available → group excluded.
     - Multi-range, group bookable in some but not all → excluded.
     - Slot with `required_count=2` but only 1 available → excluded.
     - Mix of managed (`AvailableTime`-driven) and unmanaged (event/blocked-driven) calendars across slots — leverage fixtures from `test_querysets.py:600-640`.
     - Recurring events block availability correctly (reuse recurring fixtures).
   - `with_available_calendars_in_range` annotation correctness.

3. `calendar_integration/tests/services/test_calendar_group_service.py`
   - `create_group` validation: cross-org calendars rejected; empty slot rejected.
   - `check_group_availability` returns expected per-slot calendar lists.
   - `find_bookable_slots` returns no slots when any section is fully booked.
   - `create_grouped_event` happy path: persists `CalendarEventGroupSelection` rows, sets `event.calendar_group`, picks primary correctly, creates `BlockedTime` (Option B) on non-primary calendars.
   - `create_grouped_event` validation: rejects selection of unavailable calendar, rejects under-filled slot, rejects calendar not in slot pool.
   - `update_group` refuses to evict a calendar that has future selections.
   - `delete_group` blocked by `PROTECT` when bookings exist.

4. `calendar_integration/tests/test_mutations.py` — add cases for the four new mutations.

5. `calendar_integration/tests/test_calendar_group_graphql.py` — query-shape tests against the public schema.

### Coverage target

Match the surrounding modules (the recent `ae3a39b` commit "Improves coverage" suggests this is enforced — keep new code at the same threshold).

---

## Rollout / sequencing

Land in this order; each step is independently mergeable.

1. **PR 1 — schema + querysets** (no behavior change for existing flows)
   - Add models + migration.
   - Add `CalendarGroupQuerySet.only_groups_bookable_in_ranges` + tests.
   - Add managers and admin registration.

2. **PR 2 — service layer**
   - Add `CalendarGroupService` (CRUD + `check_group_availability` + `find_bookable_slots`).
   - Tests.

3. **PR 3 — grouped event creation**
   - Add `create_grouped_event`, `CalendarEventGroupSelection` integration, primary-calendar selection logic.
   - Tests.
   - Decide Option A vs B; document in service docstring.

4. **PR 4 — GraphQL surface**
   - Types, queries, mutations, permission checks.
   - Schema snapshot test (if the project has one).

5. **PR 5 — Internal REST API**
   - Virtual models, serializers, filterset, permission class, viewset with CRUD + `events` (GET/POST), `availability` (POST), `bookable-slots` (GET) actions.
   - Route registration.
   - Full viewset test suite.

6. **PR 6 — follow-ups (separate tickets)**
   - `*_with_bulk_modifications` parity for groups.
   - Bundle-based persistence for non-primary selections (Option A) if Option B proves insufficient.
   - Performance: replace Python-loop `find_bookable_slots` with a SQL `generate_series`-based approach for large windows.
   - `CalendarPermissionService.can_manage_calendar_group` + org-admin override for the REST permission class.

7. **PR 7 — Org-admin role + `can_manage_calendar_group` override**
   - **Prerequisite**: introduce the notion of "elevated permissions within an organization" on [organizations/models.py](organizations/models.py). This is org-level scope, not CalendarGroup-specific, but every CalendarGroup permission path sits on top of it.
     - Add an `OrganizationRole` `TextChoices` with `MEMBER` (default) and `ADMIN`. A flat two-role model is enough for the current needs; richer hierarchies (owner/admin/member) can be layered later without another migration.
     - Add a `role` field to `OrganizationMembership` with `default=OrganizationRole.MEMBER`.
     - Migration: additive only, so existing memberships keep working as members.
     - `OrganizationMembership.is_admin` convenience property, plus a `User.is_organization_admin(organization)` helper so call-sites don't reach for `.role ==` strings.
     - Wire `OrganizationService.create_organization` so the creator's membership is created with `ADMIN` (an org must always have at least one admin — the person who made it).
     - `accept_invitation` keeps defaulting to `MEMBER` — unchanged.
     - Note: granting/revoking admin (API or UI affordance) is out of scope for this PR. For now admins are created via `create_organization` or directly through the Django admin / data migration.
   - **Consumer change** — in [calendar_integration/services/calendar_permission_service.py](calendar_integration/services/calendar_permission_service.py):
     - Early-return `True` from `can_manage_calendar_group` when the user has an admin membership in the group's organization:
       ```python
       membership = getattr(user, "organization_membership", None)
       if (
           membership
           and membership.organization_id == group.organization_id
           and membership.is_admin
       ):
           return True
       # existing ownership-based check…
       ```
     - Because PR6 funneled the manage-group decision through this one method, the REST `CalendarGroupPermission`, GraphQL resolvers that call it, and any future service-layer callers inherit the override for free.
   - **Tests**:
     - `is_admin` property + `User.is_organization_admin` helper: true for admin memberships, false for member/missing.
     - `can_manage_calendar_group`: admin in same org → True (even when not a calendar owner); admin in a different org → False; demoted back to member → False.
     - `CalendarGroupPermission.has_object_permission` returns True for an admin who doesn't own any pool calendar — integration check against one REST endpoint.
     - `OrganizationService.create_organization`: creator membership has `role=ADMIN`; invitation-accepted membership has `role=MEMBER`.

8. **PR 8 — future follow-ups (separate tickets)**
   - Admin grant/revoke API: a way to promote/demote users without going through the Django admin. Likely `OrganizationService.set_membership_role(...)` + matching REST/GraphQL surface, gated to existing admins.
   - Decide whether GraphQL CalendarGroup mutations should also enforce `can_manage_calendar_group` (today they skip the check and rely on org scoping alone). Once the admin role exists the decision is easier because "org admin" is a cheap, reliable signal.

---

## Open questions for the user

1. **Naming**: confirm `CalendarGroupSlot` over `CalendarGroupSection` (or pick another).
2. **Multi-calendar persistence**: confirm Option B (`BlockedTime` on non-primary selections) for v1.
3. **`required_count` semantics**: is "exactly N" or "at least N" desired? Plan assumes exactly N selected at booking time, but the slot is *satisfied* by ≥N being available. Worth confirming for the clinic case (probably exactly 1 physician + exactly 1 room).
4. **Permission model**: who can create/manage groups? Org admins only, or any calendar owner? (Affects `CalendarPermissionService` extension.)
5. **Calendar sharing across slots**: should a single calendar be allowed in multiple slots of the same group? Plan currently allows it; flag if it should be banned.
