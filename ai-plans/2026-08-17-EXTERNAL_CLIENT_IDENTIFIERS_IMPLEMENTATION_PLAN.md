# External Client Identifiers — Implementation Plan

Public-API consumers need a way to link our `CalendarEvent` and `ExternalAttendee` records back to
resources in *their* systems (a CRM deal, a ticket, a patient record). This plan adds a generic,
organization-scoped side table that carries `(system, identifier)` pairs pointing at those records,
exposes it on the public GraphQL API, the internal REST API, the Django admin and the outbound
event webhooks, and adds the public `updateCalendarEvent` mutation that identifier updates need.

There is no `..._SPEC.md` sibling for this feature. The decisions below were settled by
interrogation before drafting; every one of them is recorded in **Guiding Decisions** rather than
assumed.

## 1. Goals

1. Ship an organization-scoped `ExternalClientIdentifier` table that attaches any number of
   `(system, identifier)` pairs to a `CalendarEvent` or an `ExternalAttendee` through a
   `ContentType`-backed generic foreign key, with DB-enforced uniqueness in both directions.
2. Let public-API consumers set, replace and clear identifiers through the GraphQL API, and read
   them back on both event and external-attendee types.
3. Let consumers find records *by* identifier: filter arguments on the existing public GraphQL
   event queries and django-filter parameters on the internal REST event endpoint.
4. Include an event's identifiers in every outbound calendar-event webhook payload, so a consumer
   receiving a webhook can route it without a follow-up API call.
5. Add a public `updateCalendarEvent` mutation covering event metadata, internal attendees and
   external attendees — the surface identifier updates require, which the public API lacks today.

**Non-goals:**

- Renaming, repurposing or touching the existing `external_id` columns on `Calendar`,
  `CalendarEvent` or `BlockedTime`. Those are calendar-provider sync keys and stay exactly as they
  are — see **Guiding Decisions**.
- Attaching identifiers to any model other than `CalendarEvent` and `ExternalAttendee`. The table
  is generic; the *write surface* is allowlisted.
- A standalone external-attendee mutation or endpoint. Attendees are written through their event
  today and keep being written that way.
- Bulk-upsert of identifiers across many records in one call.
- A per-record cap on identifier count (see **Open Questions**).
- Exposing identifiers on `BlockedTime`, `AvailableTime`, `Calendar` or `CalendarGroup`.
- Backfilling anything. The table starts empty and no existing data maps into it.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Not a column on the two models** | `CalendarEvent.external_id` already exists at [models.py:1215](../calendar_integration/models.py#L1215) as a globally-unique provider sync key (Google/Microsoft event ids; `manual-<isoformat>-<n>` for internally created events), read-only in both the REST serializer and the GraphQL type. `Calendar.external_id` and `BlockedTime.external_id` mean the same thing. A consumer-owned reference cannot share that column or that name without conflating two unrelated identities. A side table also delivers what a column cannot: *several* identifiers per record. |
| **Generic FK, not the audit app's soft reference** | `ContentType` FK + `GenericForeignKey`, as specified. This is the first `GenericForeignKey` in the repo — the `audit` app deliberately uses a soft `subject_type`/`subject_id` string pair ([audit/models.py:77-81](../audit/models.py#L77-L81)) because audit rows must *outlive* their subject. Identifier rows must not: they are meaningless once the target is gone, so a real FK-backed relation with cascade is the right trade here, and it buys `GenericRelation` prefetch and the `.identified_object` accessor. |
| **Organization-scoped like everything else** | `SingleOrganizationModelMixin` + `SafeRelationNullInitMixin` + `BaseModel` + `OrganizationScopedManager`, matching every other model in `calendar_integration`. A `GenericForeignKey` **cannot** be an `OrganizationSafeForeignKey`, so nothing at the DB level guarantees the target row shares the identifier row's organization. The service layer validates it on every write and every read filters by organization — this is the one org-safety invariant in this feature that is code-enforced rather than schema-enforced, and it is called out again in **Risk & Rollout Notes**. |
| **One identifier per system per record** | Unique `(organization, content_type, identified_key, system)`. Makes "set this record's Salesforce id" a well-defined upsert with a single conflict target, and makes replace/clear semantics unambiguous. |
| **One external resource per record type** | Unique `(organization, content_type, system, identifier)`. Within one organization, a given `(system, identifier)` maps to at most one event *and* at most one external attendee — an event and an attendee may share an id, two events may not. This constraint doubles as the reverse-lookup index. |
| **Allowlisted targets** | Writes accept only `calendar_integration.CalendarEvent` and `calendar_integration.ExternalAttendee`. An open generic-FK write surface reachable by an external API token lets a caller attach rows keyed to `Organization`, `SystemUser` or any other table; the allowlist closes that off. The *table* stays generic — adding a third target is a one-line registry change plus a `GenericRelation`. |
| **Cascade via `GenericRelation`** | `GenericRelation` on both target models, so Django's deletion collector removes identifier rows with their target — including when the target is itself cascaded from a parent (`Calendar` → `CalendarEvent`). A `post_delete` signal would miss those parent-driven cascades; a periodic sweep would leave windows where `.identified_object` resolves to `None`. |
| **Replace-the-set, tri-state input** | A write supplies the complete identifier list for that record and it replaces what is stored. **Omitted means untouched; an empty list means clear all.** The GraphQL input field therefore defaults to `strawberry.UNSET`, never `default_factory=list` — with a list default, every existing `scheduleEvent` caller that has never heard of this feature would start wiping identifiers. The REST serializer uses the same distinction via `partial`/absent-key handling. This is the single highest-risk detail in the plan and every write phase ships a test for it. |
| **`system` is a normalized URL** | `URLField`, normalized on write: scheme and host lowercased, trailing slash stripped from the path, path/query/fragment otherwise preserved. Because uniqueness is keyed on `system`, un-normalized input would let `https://Crm.Example.com/` and `https://crm.example.com` hold two competing identifiers for the same record. Normalization lives in one function that every write path calls — service, serializer and admin form. |
| **No feature flag** | This repo has no feature-flag module, and building one is larger than the feature. Every change here is additive: a new table, new `UNSET`-defaulted optional inputs, new optional filter arguments, a new mutation, one new key in the webhook payload. In place of a flag-off test, every phase that touches an existing write path ships a regression test proving a caller that never mentions identifiers sees byte-identical behavior. Consequence: there is no runtime kill switch — rollback is a revert plus deploy. |
| **Audited as part of the event write** | Identifier changes ride the existing `CalendarEventService._audit_event_write` ([calendar_event_service.py:262](../calendar_integration/services/calendar_event_service.py#L262)) with an `external_client_identifiers` diff key, rather than a parallel audit stream. Consistent with how the rest of the event's fields are audited. |
| **Lives in `calendar_integration`** | Next to the only two models it targets. Revisit if a third app ever needs identifiers. |
| **Webhook payload is a partner contract** | Adding `external_client_identifiers` to `CalendarEventWebhookPayload` is additive, but it is externally visible: `WEBHOOK_EVENT_DESCRIPTIONS` must be updated in the same phase, and partners want notice. |

## 3. Data Model Changes

### 3.1 New `ExternalClientIdentifier`

Added to @calendar_integration/models.py, exported alongside the other models in that module.

```python
class ExternalClientIdentifier(SingleOrganizationModelMixin, SafeRelationNullInitMixin, BaseModel):
    """A client-owned reference from one of our records to a resource in the client's system.

    ``system`` names the external system as a normalized URL; ``identifier`` is that
    system's opaque id for the resource. The target is any model in
    ``IDENTIFIABLE_MODELS`` -- today ``CalendarEvent`` and ``ExternalAttendee``.

    The generic relation cannot be an ``OrganizationSafeForeignKey``, so the target's
    organization is validated in the service layer on write, never by the schema. Every
    read path must go through ``objects`` (organization-scoped) rather than
    ``original_manager``.
    """

    objects: ClassVar[OrganizationScopedManager] = OrganizationScopedManager()

    # Not an OrganizationSafeForeignKey: ContentType is a Django-global table with no
    # organization column.
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    # Repo PKs are BigAutoField, so this is BigInteger, not Integer.
    identified_key = models.BigIntegerField()
    identified_object = GenericForeignKey("content_type", "identified_key")

    system = models.URLField(max_length=500)
    identifier = models.CharField(max_length=255)

    class Meta:
        constraints = (
            # One identifier per system per record.
            models.UniqueConstraint(
                fields=["organization", "content_type", "identified_key", "system"],
                name="extclientid_uniq_target_system",
            ),
            # One external resource maps to at most one record of a given type.
            # Doubles as the reverse-lookup index for the filter arguments.
            models.UniqueConstraint(
                fields=["organization", "content_type", "system", "identifier"],
                name="extclientid_uniq_system_ident",
            ),
        )
        indexes = (
            # Record -> identifiers, the direction every read on an event/attendee takes.
            models.Index(
                fields=["organization", "content_type", "identified_key"],
                name="extclientid_org_ct_key_idx",
            ),
        )

    def __str__(self):
        return f"{self.system}#{self.identifier}"
```

Index and constraint names are kept under 30 characters — Django rejects longer `Index` names.

### 3.2 `GenericRelation` on the two target models

In @calendar_integration/models.py, on both `CalendarEvent` ([models.py:1202](../calendar_integration/models.py#L1202)) and `ExternalAttendee` ([models.py:494](../calendar_integration/models.py#L494)):

```python
    external_client_identifiers = GenericRelation(
        "ExternalClientIdentifier",
        content_type_field="content_type",
        object_id_field="identified_key",
        related_query_name="calendar_event",  # "external_attendee" on ExternalAttendee
    )
```

This is what makes deletion cascade — Django's collector walks `GenericRelation`s, so identifier
rows go with their target even when the target is itself cascaded (deleting a `Calendar` cascades
its `CalendarEvent`s, which now cascade their identifiers). It also enables
`prefetch_related("external_client_identifiers")` on the read paths.

### 3.3 Target registry and normalization

New module @calendar_integration/external_client_identifiers.py:

```python
#: Models an ExternalClientIdentifier may point at, as "app_label.modelname" (lowercased,
#: matching ContentType.model). The table is generic; the write surface is not -- an open
#: generic-FK write path reachable by a public API token would let a caller key rows to
#: arbitrary tables.
IDENTIFIABLE_MODELS: frozenset[str] = frozenset({
    "calendar_integration.calendarevent",
    "calendar_integration.externalattendee",
})


def normalize_system(value: str) -> str:
    """Lowercase scheme and host, strip a trailing slash from the path.

    Uniqueness is keyed on ``system``, so two spellings of one host must not become two
    systems. Every write path calls this -- the service, the DRF serializer and the admin
    form -- because bulk_create bypasses ``Model.save()``.
    """
```

`ContentType` rows are resolved lazily through `ContentType.objects.get_for_model` and never
cached at import time (their ids differ per environment and are not stable across a fresh test
database).

### 3.4 Type plumbing

- @calendar_integration/services/dataclasses.py:
  - New `ExternalClientIdentifierData` dataclass (`system: str`, `identifier: str`).
  - `CalendarEventInputData` and `ExternalAttendeeInputData` ([dataclasses.py:59](../calendar_integration/services/dataclasses.py#L59)) gain
    `external_client_identifiers: list[ExternalClientIdentifierData] | None = None` — `None` is the
    "omitted, leave untouched" sentinel, distinct from `[]` meaning "clear".
  - `CalendarEventData` ([dataclasses.py:233](../calendar_integration/services/dataclasses.py#L233)) and
    `EventExternalAttendeeData` gain `external_client_identifiers: list[ExternalClientIdentifierData]`
    (defaulted, so it goes after `original_payload`).
- @webhooks/services/payloads.py: `CalendarEventWebhookPayload` gains
  `external_client_identifiers: list[dict[str, str]]`.

## 4. API Design

### 4.1 Public GraphQL — reading

`ExternalClientIdentifierGraphQLType` (`system`, `identifier`) is exposed as a list field on both
`CalendarEventGraphQLType` ([graphql.py:326](../calendar_integration/graphql.py#L326)) and
`ExternalAttendeeGraphQLType` ([graphql.py:229](../calendar_integration/graphql.py#L229)):

```graphql
type CalendarEventGraphQLType {
  # ... existing fields, including the provider-owned externalId, unchanged
  externalClientIdentifiers: [ExternalClientIdentifierGraphQLType!]!
}
```

### 4.2 Public GraphQL — writing

`scheduleEvent` ([mutations.py:3094](../public_api/mutations.py#L3094)) and the new
`updateCalendarEvent` both take:

```graphql
input ExternalClientIdentifierInput { system: String!, identifier: String! }

input ScheduleEventInput {
  # ... existing fields
  externalClientIdentifiers: [ExternalClientIdentifierInput!]   # UNSET = untouched, [] = clear
  externalAttendees: [ScheduleEventExternalAttendeeInput!]
}

input ScheduleEventExternalAttendeeInput {
  email: String!
  name: String! = ""
  externalClientIdentifiers: [ExternalClientIdentifierInput!]
}
```

Errors: `system` that fails URL validation, a blank/whitespace-only `identifier`, an `identifier`
over 255 characters, or a `(system, identifier)` already claimed by another record of the same type
in the organization → a domain error surfaced as a GraphQL error, with the whole mutation rolled
back. Partial application is never allowed.

### 4.3 Public GraphQL — lookup

`calendarEvents` ([queries.py:350](../public_api/queries.py#L350)) gains two optional arguments:

```graphql
calendarEvents(
  calendarId: Int, userId: Int, eventId: Int,
  startDatetime: DateTime, endDatetime: DateTime,
  externalClientIdentifierSystem: String,
  externalClientIdentifierIdentifier: String,
): [CalendarEventGraphQLType!]!
```

Both must be supplied together (supplying one alone is an error — a bare `identifier` across all
systems is not a meaningful query and would scan). The pair narrows whichever existing lookup mode
is in play and composes with the owner-scope filtering already applied to scoped tokens, so it
cannot widen what a scoped token can see. `system` is normalized before matching.

### 4.4 Public GraphQL — `updateCalendarEvent`

New mutation, mapped to `PublicAPIResources.CALENDAR_EVENT` in
`OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` ([permissions.py:29](../public_api/permissions.py#L29)),
alongside `scheduleEvent` and `rescheduleCalendarEvent`:

```graphql
input UpdateCalendarEventInput {
  organizationId: Int!
  eventId: Int!
  title: String
  description: String
  attendeeUserIds: [Int!]
  externalAttendees: [ScheduleEventExternalAttendeeInput!]
  externalClientIdentifiers: [ExternalClientIdentifierInput!]
}
```

Every field is `UNSET`-defaulted and omitted means untouched. Times, timezone and recurrence stay
owned by `rescheduleCalendarEvent` and are deliberately absent — this mutation does not overlap it.
Owner-scoped tokens may only update events on calendars their owner owns, matching
`rescheduleCalendarEvent`'s existing check.

### 4.5 Internal REST

`CalendarEventSerializer` ([serializers.py:816](../calendar_integration/serializers.py#L816)) and the
external-attendance serializers gain a nested writable `external_client_identifiers` list with the
same replace-the-set and omitted-vs-empty semantics. `CalendarEventFilterSet`
([filtersets.py:49](../calendar_integration/filtersets.py#L49)) gains
`external_client_identifier_system` and `external_client_identifier_identifier` parameters with the
same both-or-neither rule.

### 4.6 Webhooks

Every `calendar_event_*` webhook payload gains a key. Additive, but partner-visible:

```json
{
  "id": 42, "calendar_id": 7, "title": "Intro call",
  "external_client_identifiers": [
    {"system": "https://crm.example.com", "identifier": "deal-9182"}
  ]
}
```

An event with no identifiers emits `[]`, never a missing key and never `null`. Attendee webhook
payloads embed the event object, so they carry the event's identifiers; the attendee's *own*
identifiers are not added to `EventAttendeeWebhookPayload` in this plan (see **Open Questions**).

## 5. Phased Rollout

Phases are bundled by concern rather than one-per-use-case, per the granularity decision. No phase
carries a feature flag — see the "No feature flag" row in **Guiding Decisions** for the
justification, and note that each phase touching an existing write path carries an explicit
no-op-when-omitted regression test in its place. Phases 4, 5 and 6 each depend only on Phases 1–2
(Phase 6 also on Phase 3), so they can land in any order once the foundation is merged.

**Phase 7 was added on 2026-08-18, after Phases 1–6 shipped.** It is not part of the original
design; it exists because the Tier 4 review of Phase 6 traced six findings to one root cause in
an internal dataclass. It depends on all of Phases 1–6 and must land last.

### Phase 1 — Add the ExternalClientIdentifier table

**Goal**: the table, its constraints and its cascade behavior exist. Ship value: none on its own —
this is the foundation every later phase consumes, and it is split out because a schema change
deserves to be reviewable and revertable without any API surface attached to it.

**Feature flag**: none — a new table no code reads or writes yet is purely additive.

Changes:
1. @calendar_integration/models.py: add `ExternalClientIdentifier` per **Data Model Changes**; add
   the `GenericRelation` to `CalendarEvent` and `ExternalAttendee`. Import `ContentType`,
   `GenericForeignKey`, `GenericRelation` from `django.contrib.contenttypes`.
2. @calendar_integration/external_client_identifiers.py: new module with `IDENTIFIABLE_MODELS` and
   `normalize_system`.
3. @calendar_integration/migrations/: `makemigrations` — one `CreateModel` plus two
   `AddConstraint` and one `AddIndex`. All against a brand-new empty table, so no
   `AddIndexConcurrently` and no `atomic = False` (unlike
   [0042_availabletime_blockedtime_group_slot.py](../calendar_integration/migrations/0042_availabletime_blockedtime_group_slot.py),
   which needed both because it indexed populated tables). Verify the migration reverses cleanly.
4. @calendar_integration/factories.py: `ExternalClientIdentifierFactory`, organization-scoped like
   its siblings.
5. @calendar_integration/admin.py: register `ExternalClientIdentifier` with `list_display`
   (`system`, `identifier`, `content_type`, `identified_key`, `organization`) and `search_fields`
   (`system`, `identifier`). The admin form's `clean_system` calls `normalize_system` so
   hand-edited rows cannot violate the uniqueness assumption.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `calendar_integration/tests/models/test_external_client_identifier.py` — the two unique
  constraints each raise `IntegrityError` on violation; two records of *different* content types
  may share one `(system, identifier)`; two organizations may each hold the same
  `(system, identifier)`; `normalize_system` handles case, trailing slash, and preserves
  path/query/fragment.
- **Integration**: same file — deleting a `CalendarEvent` deletes its identifier rows; deleting an
  `ExternalAttendee` deletes its rows; deleting a `Calendar` cascades through its events to their
  identifier rows (the case a `post_delete` signal would have missed).

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)).
Model + migration + factory + admin is exact-precedent work, but this is the repo's first
`GenericForeignKey` and the cascade test is the whole point of the phase.

**Reusable skills**: `add-model` (model, manager, admin, factory conventions); `add-migration`
(migration authoring and the reverse path).

Acceptance: `makemigrations --check` is clean, the migration applies and reverses, and deleting a
`Calendar` removes the identifier rows of every event on it.

### Phase 2 — Identifier write service, normalization, validation, audit

**Goal**: one service owns identifier reads and replace-the-set writes, with normalization,
allowlist enforcement, cross-organization rejection and audit emission. Ship value: none directly —
no API reaches it yet — but every subsequent phase is a thin adapter over it, which is what keeps
Phases 3–6 small and consistent.

**Feature flag**: none — new service code with no existing callers.

Changes:
1. @calendar_integration/services/external_client_identifier_service.py: new
   `ExternalClientIdentifierService` with
   `replace_for_target(target, identifiers | None) -> tuple[list, list]` (returns old and new state
   for the audit diff; `None` is a no-op) and `get_for_targets(targets)` for batched reads.
   `replace_for_target` normalizes `system`, rejects blank/whitespace-only and over-length
   `identifier`s, rejects a `content_type` outside `IDENTIFIABLE_MODELS`, rejects a target whose
   `organization_id` differs from the bound organization, then diffs the existing set against the
   incoming one and applies the delete/create. Runs inside the caller's transaction so a
   constraint violation rolls the whole event write back.
2. @calendar_integration/exceptions.py: domain exceptions for the four rejection cases.
3. @calendar_integration/services/dataclasses.py: the type plumbing in **Data Model Changes**.
4. @calendar_integration/services/calendar_event_service.py: `create_event` ([:523](../calendar_integration/services/calendar_event_service.py#L523))
   and `update_event` ([:829](../calendar_integration/services/calendar_event_service.py#L829)) call the
   service for the event and for each external attendee, passing `None` when the caller omitted the
   field. Note that `update_event`'s existing attendee reconciliation *deletes and recreates*
   external attendees whose id is absent from the incoming list ([:1067-1084](../calendar_integration/services/calendar_event_service.py#L1067-L1084));
   identifiers for a recreated attendee must be applied to the new row, not silently dropped —
   this is the subtle case the phase's tests pin.
5. Audit: extend the `diff` passed to `_audit_event_write` ([:262](../calendar_integration/services/calendar_event_service.py#L262))
   with an `external_client_identifiers` key holding `{"old": [...], "new": [...]}`, emitted only
   when the set actually changed.
6. @di_core/containers.py: register the service alongside the other calendar services.

Spec use-case: shared scaffolding consumed by every API phase.

Tests:
- **Unit**: `calendar_integration/tests/services/test_external_client_identifier_service.py` —
  replace-the-set adds/removes/keeps correctly; `None` is a no-op; `[]` clears; `system` is
  normalized before comparison so a re-send with different casing is *not* treated as a change;
  a non-allowlisted `content_type` is rejected; a target from another organization is rejected;
  blank, whitespace-only and over-length identifiers are rejected.
- **Integration**: `calendar_integration/tests/services/test_calendar_event_service_identifiers.py` —
  `create_event` and `update_event` persist identifiers for the event and its external attendees;
  an `update_event` that omits the field leaves stored identifiers untouched; one that sends `[]`
  clears them; an attendee deleted-and-recreated during reconciliation keeps its identifiers; a
  duplicate `(system, identifier)` rolls the entire event write back, leaving no partial event;
  the audit diff carries the identifier change and is absent when nothing changed.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)).
Multi-file service work with real branching — tri-state input, cross-organization validation, and
the attendee-recreation interaction with an existing reconciliation loop.

Acceptance: an `update_event` call that never mentions identifiers produces a byte-identical event
write to the one it produced before this phase, and a call that sends a list replaces the stored
set exactly.

### Phase 3 — Public GraphQL read, create-time write, and lookup

**Goal**: public-API consumers can set identifiers when scheduling an event, read them back on
events and external attendees, and find events by `(system, identifier)`.

**Feature flag**: none — new optional `UNSET`-defaulted input fields, a new output field, and two
new optional query arguments. A caller that omits all of them sees the pre-feature API exactly.

Changes:
1. @calendar_integration/graphql.py: `ExternalClientIdentifierGraphQLType`; the list field on
   `CalendarEventGraphQLType` ([:326](../calendar_integration/graphql.py#L326)) and
   `ExternalAttendeeGraphQLType` ([:229](../calendar_integration/graphql.py#L229)).
2. @public_api/mutations.py: `ExternalClientIdentifierInput`; the `UNSET`-defaulted field on
   `ScheduleEventInput` ([:956](../public_api/mutations.py#L956)) and
   `ScheduleEventExternalAttendeeInput` ([:948](../public_api/mutations.py#L948)); `schedule_event`
   ([:3094](../public_api/mutations.py#L3094)) maps the input onto the dataclasses from Phase 2,
   passing `None` for `UNSET`.
3. @public_api/queries.py: the two filter arguments on `calendar_events`
   ([:350](../public_api/queries.py#L350)), applied after the existing owner-scope narrowing so a
   scoped token's visibility is unchanged; both-or-neither validation; `prefetch_related` on the
   generic relation so selecting identifiers on a list of events stays one extra query.
4. @public_api/tests/: no permission-mapping change needed — `scheduleEvent` and `calendarEvents`
   already map to `PublicAPIResources.CALENDAR_EVENT`.

Spec use-case: consumers link a new event and its external attendees to their own records, and
look events up by those links.

Tests:
- **Integration**: `public_api/tests/test_external_client_identifiers_graphql.py` — `scheduleEvent`
  with identifiers on the event and on an attendee persists and reads both back; a `scheduleEvent`
  that **omits** the field behaves identically to before, with no identifier rows written (the
  no-op regression test standing in for a flag-off test); a duplicate `(system, identifier)`
  returns an error and creates no event; filtering by `(system, identifier)` returns only the
  matching event; filtering with one argument alone errors; an owner-scoped token filtering by an
  identifier on another owner's event gets an empty result, not that event; a token from another
  organization filtering by a colliding `(system, identifier)` gets an empty result.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)).
Spans types, inputs, a mutation resolver and a query resolver, and the filter path has to compose
correctly with existing owner-scope narrowing.

Acceptance: a public-API token can schedule an event carrying identifiers, read them back, and
retrieve that event by `(system, identifier)`; a token that never sends the field observes no
change to `scheduleEvent`.

### Phase 4 — Carry event identifiers in webhook payloads

**Goal**: a consumer receiving a `calendar_event_*` webhook can route it against their own system
without an API round-trip.

**Feature flag**: none — one added key in an outbound payload. Additive for any consumer parsing
by key; noted as a partner-visible contract change in **Risk & Rollout Notes**.

Changes:
1. @webhooks/services/payloads.py: `CalendarEventWebhookPayload` gains
   `external_client_identifiers: list[dict[str, str]]`.
2. @calendar_integration/services/calendar_service_utils.py: both builders populate the new
   `CalendarEventData` field — `serialize_event_data` ([:251](../calendar_integration/services/calendar_service_utils.py#L251))
   from the ORM object (via the prefetched generic relation, not a per-event query), and
   `serialize_event_data_input` ([:265](../calendar_integration/services/calendar_service_utils.py#L265))
   from the input data, since side effects fire before the ORM object is reloaded.
3. @webhooks/services/webhook_calendar_side_effects.py: `_serialize_event`
   ([:27](../webhooks/services/webhook_calendar_side_effects.py#L27)) emits the key, always a list —
   `[]` when there are none, never `null` and never absent.
4. @webhooks/constants.py: update the `WEBHOOK_EVENT_DESCRIPTIONS` text for
   `CALENDAR_EVENT_CREATED`, `_UPDATED` and `_DELETED`, which enumerate the payload fields.

Spec use-case: consumers route inbound webhooks by their own identifiers.

Tests:
- **Integration**: `webhooks/tests/test_calendar_event_identifier_payloads.py` — created, updated
  and deleted event webhooks all carry the identifiers; an event with none emits `[]`; the delete
  payload snapshot captures identifiers as they were immediately before deletion (the rows are
  cascaded away by then, so the snapshot must be built first); attendee webhooks carry the event's
  identifiers through the embedded event object; serializing N events issues no per-event
  identifier query.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)).
Mechanical plumbing through two existing serializers, with one genuine ordering trap in the delete
snapshot.

Acceptance: a `calendar_event_created` webhook for an event with two identifiers delivers both in
`external_client_identifiers`, and one for an event with none delivers `[]`.

### Phase 5 — Internal REST read, write and filtering

**Goal**: the SPA reads and writes identifiers through the same REST endpoints it already uses for
events and attendees.

**Feature flag**: none — a nested optional serializer field and two optional filter parameters.
Omitting them preserves current behavior.

Changes:
1. @calendar_integration/serializers.py: `ExternalClientIdentifierSerializer`; nested writable list
   on `CalendarEventSerializer` ([:816](../calendar_integration/serializers.py#L816)) and on the
   external-attendee serializers ([:567](../calendar_integration/serializers.py#L567),
   [:584](../calendar_integration/serializers.py#L584)); `create` ([:1160](../calendar_integration/serializers.py#L1160))
   and `update` ([:1261](../calendar_integration/serializers.py#L1261)) map the field onto the Phase 2
   dataclasses, passing `None` when the key is absent from `validated_data` so a `PATCH` that omits
   it does not clear.
2. @calendar_integration/virtual_models.py: prefetch the generic relation on
   `CalendarEventVirtualModel` ([:105](../calendar_integration/virtual_models.py#L105)) and
   `ExternalAttendeeVirtualModel` ([:39](../calendar_integration/virtual_models.py#L39)) so list
   responses do not go N+1.
3. @calendar_integration/filtersets.py: the two parameters on `CalendarEventFilterSet`
   ([:49](../calendar_integration/filtersets.py#L49)), both-or-neither.
4. Regenerate @schema.yml.

Spec use-case: first-party clients manage the same links the public API exposes.

Tests:
- **Integration**: `calendar_integration/tests/test_event_identifier_rest.py` — `POST` with
  identifiers persists them; `PUT` replaces the set; `PATCH` omitting the key leaves them untouched;
  `PATCH` with `[]` clears them; the list endpoint filters correctly; a list of N events with
  identifiers issues a constant number of queries; a duplicate returns 400 with no partial write.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)).
Nested writable serializers interacting with the existing virtual-model layer, plus the
`PATCH`-omitted-versus-empty distinction, which is where a nested DRF field usually gets it wrong.

**Reusable skills**: `create-rest-endpoint` (serializer, filterset, `schema.yml` regeneration).

Acceptance: a `PATCH` that omits `external_client_identifiers` leaves stored identifiers unchanged,
and one that sends `[]` clears them.

### Phase 6 — Public `updateCalendarEvent` mutation

**Goal**: public-API consumers can update an event's metadata, attendees and identifiers — the
update and clear path the public API has never had.

**Feature flag**: none — an entirely new mutation. Nothing existing can regress from its addition;
the risk is internal to the mutation itself, addressed by the review-model override below.

Changes:
1. @public_api/mutations.py: `UpdateCalendarEventInput` and `update_calendar_event`, every field
   `UNSET`-defaulted with omitted meaning untouched. Delegates to
   `CalendarEventService.update_event` ([:829](../calendar_integration/services/calendar_event_service.py#L829)),
   passing through only the fields actually supplied so the existing reconciliation logic is not
   handed a synthetic "replace with nothing" for an omitted list.
2. Owner-scope enforcement mirroring `reschedule_calendar_event`
   ([:3201](../public_api/mutations.py#L3201)): a scoped token may only update events on calendars its
   owner owns, returning the same not-found-shaped error for events outside that set so existence
   does not leak.
3. @public_api/permissions.py: `"updateCalendarEvent": PublicAPIResources.CALENDAR_EVENT` in
   `FIELD_TO_RESOURCE_MAPPING` ([:29](../public_api/permissions.py#L29)).
4. @public_api/docs_content.py: document the mutation and, explicitly, that times, timezone and
   recurrence belong to `rescheduleCalendarEvent`.

Spec use-case: consumers update and clear the links they created, and maintain event metadata and
attendees.

Tests:
- **Integration**: `public_api/tests/test_update_calendar_event_mutation.py` — updating title alone
  leaves description, attendees and identifiers untouched; replacing identifiers works; `[]` clears
  them; replacing external attendees fires the attendee added/removed webhooks and preserves the
  surviving attendees' identifiers; an org-wide token may update any event, a scoped token only its
  owner's, and a scoped token targeting another owner's event gets the not-found-shaped error;
  a token lacking the `calendar_event` scope is rejected; a duplicate identifier rolls the whole
  update back, leaving title and attendees unchanged.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)).
A new mutation over an existing service method, but attendee replacement reaches provider sync,
notifications and the external-attendee delete path.

**Review models**: reviewer Tier 4 — this is the only phase that hands an external token a
general-purpose event mutation. The failure modes are an owner-scope bypass (a scoped token editing
an event it should not see) and an unintended attendee wipe from an omitted-versus-empty mix-up,
both of which are damaging and easy to miss in a diff that otherwise looks like ordinary resolver
wiring. Fixer left on the project default.

Acceptance: an owner-scoped public-API token can change an event's title without disturbing its
attendees or identifiers, cannot touch an event outside its owner's calendars, and clearing
identifiers with `[]` removes exactly those rows.

### Phase 7 — Make the event input tri-state, and two fixes it depends on

**Added 2026-08-18, after Phases 1–6 shipped.** This phase was not in the original plan. It exists
because the Tier 4 review of Phase 6 traced six separate findings to a single root cause, and
fixing that cause is a smaller change than defending against its symptoms one at a time.

**Goal**: `CalendarEventInputData` becomes tri-state on every field a caller can omit, so
`update_event` skips what was not supplied instead of being handed a reconstruction of it. Ship
value: removes a live lost-update race and a partner-visible webhook storm, and deletes the code
path that produced Phase 6's BLOCKER.

**Feature flag**: none — a semantic change to an internal dataclass with every caller updated in
the same commit.

**Why this is needed.** `CalendarEventInputData.title`, `description`, `attendances` and
`external_attendances` are *always-replace*. Only `external_client_identifiers` is natively
tri-state (`None` = untouched), added in Phase 2. Because of that asymmetry, Phase 6's
`update_calendar_event` resolver reads the event's current state from the database and re-sends it
for any field the caller left `UNSET`. The Tier 4 reviewer traced six findings to that
reconstruction:

1. A case-sensitive email match that hard-deleted an attendee and cascaded away its
   `ExternalClientIdentifier` rows, returning `200`. Fixed defensively in Phase 6 by normalizing
   with `.strip().lower()`; the root cause remains.
2. `name` silently blanked when an attendee was re-sent without one — `ScheduleEventExternalAttendeeInput.name`
   defaults to `""`, not `UNSET`. Fixed defensively in Phase 6.
3. Duplicate emails in one payload collapsing two attendees into one. Rejected explicitly in Phase 6.
4. **A lost-update race, still live.** The resolver's read happens outside `update_event`'s
   transaction with no lock, so two concurrent `updateCalendarEvent` calls touching *different*
   fields can silently revert each other. Request A sends `{title}` and reads attendees `[u1]`;
   request B sends `{attendeeUserIds: [u1, u2]}` and commits; A then commits `title` **and**
   `attendances=[u1]`, deleting u2 while B's caller was told it succeeded.
5. **Spurious `CALENDAR_EVENT_ATTENDEE_UPDATED` webhooks, still live.** Reconstructing every
   existing attendee *with its id* puts them all on `update_event`'s update-in-place branch, which
   unconditionally appends to `serialized_external_attendances_to_update`. A title-only update on
   an event with 50 external attendees emits 50 attendee-updated webhooks plus a `bulk_update`.
   The plan's **Guiding Decisions** treat webhook payloads as a partner contract.
6. The email-matching layer existing at all.

Items 4 and 5 are not fixed anywhere. Items 1–3 are patched at the symptom.

Changes:
1. @calendar_integration/services/dataclasses.py: `title`, `description`, `attendances` and
   `external_attendances` on `CalendarEventInputData` accept `None` meaning "leave untouched",
   matching what `external_client_identifiers` already does.
2. @calendar_integration/services/calendar_event_service.py: `update_event` skips the
   title/description assignment, the internal-attendance reconciliation and the
   external-attendance reconciliation when the corresponding field is `None`. `create_event`
   semantics are unchanged — on create, `None` behaves as today (empty), it does not raise.
3. Every construction site of `CalendarEventInputData` is made explicit so no existing caller
   changes behavior: the Phase 5 REST serializers, `schedule_event`, `reschedule_calendar_event`,
   `CalendarBundleService`, and the tests. **Enumerate them before touching `update_event`** —
   this dataclass is shared across REST, GraphQL and the bundle path, which is why the refactor
   was deferred out of Phase 6 rather than folded into it.
4. @public_api/mutations.py: delete `update_calendar_event`'s read-then-resend reconstruction and
   pass `strawberry.UNSET` → `None` straight through, exactly as it already does for
   `externalClientIdentifiers`.
5. @calendar_integration/services/calendar_service_utils.py: `serialize_event_external_attendee`
   (~line 174) unconditionally dereferences `external_attendance.external_attendee_fk`.
   `EventExternalAttendance.external_attendee` is `null=True`, so **any** `update_event` call —
   any caller, not just the public API — raises `AttributeError` on an event carrying such a row.
   Pre-existing, surfaced while testing Phase 6's null guard. A NULL attendee has no email or name
   to report, so decide deliberately whether to skip the row or emit a null-safe payload.
6. Re-pin the query-count assertions, **last**, after 1–5 have settled.

**An open decision this phase must make.** When a caller *does* supply `externalAttendees`,
`ScheduleEventExternalAttendeeInput` still carries no `id`, so `update_event` cannot match by id.
Tri-state removes the need to match anything in the *omitted* case, but not the supplied one.
Either keep email matching for supplied lists — retaining the `.strip().lower()` normalization,
since removing it reintroduces finding 1 — or accept plain replace-the-set (delete and recreate)
semantics and document loudly that a supplied list replaces wholesale, including identifiers.

**On the query counts.** The webhook DI fix ([#278](https://github.com/vintasoftware/vinta-schedule-api/pull/278))
makes calendar side effects actually dispatch, and each dispatch costs one `WebhookConfiguration`
lookup. Measured against this stack with that fix cherry-picked: **exactly one assertion moves**,
`test_update_event_on_commit_dispatch_reuses_prefetched_identifiers`, from **75 to 78** — three
dispatches (event update, attendee add, attendee remove). Phase 5's ICS count of 26 and the N+1
assertions are unaffected. The structural change in items 1–4 will move counts again by removing
the spurious attendee-update dispatches, so re-pin once at the end and state what each delta is
rather than bumping the number.

Spec use-case: none directly — this is a correctness and contract-hygiene phase.

Tests:
- **Integration**: a title-only `updateCalendarEvent` on an event with external attendees emits
  **zero** `CALENDAR_EVENT_ATTENDEE_UPDATED` webhooks. The existing Phase 6 webhook test cannot
  see this: it registers no `WebhookConfiguration` for that event type and asserts only
  `_ADDED` / `_REMOVED`.
- **Integration**: an `update_event` over an event carrying an `EventExternalAttendance` with a
  NULL `external_attendee` does not raise.
- **Regression**: every Phase 6 test passes unchanged. Any that must change needs an explicit
  argument that it is not a weakened assertion.
- Note that calendar webhooks do not dispatch at all until #278 lands, so any test asserting real
  dispatch must hand-wire the pipeline the way `webhooks/tests/test_calendar_event_identifier_payloads.py`
  does.

**Suggested AI model**: Tier 4 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)).
Changing the semantics of a dataclass shared by REST, GraphQL and the bundle fan-out, where the
failure mode is silent data loss in an existing caller rather than a test going red.

Acceptance: an `updateCalendarEvent` that supplies only `title` issues no attendee writes and no
attendee webhooks, leaves attendees and identifiers untouched, and cannot revert a concurrent
update to a field it did not mention. The full suite is green with no assertion weakened.

## 6. Risk & Rollout Notes

**No feature flag, and what stands in for one.** The repo has no flag primitive and this feature
does not justify building one. The mitigation is structural: every new input is `UNSET`-defaulted,
every new output field is additive, and each phase that touches an existing write path ships an
explicit test that a caller omitting the new field produces the same result as before. The cost is
real and worth stating plainly — there is no runtime kill switch. Backing out a phase means
reverting and deploying, and for Phase 1 that also means reversing a migration.

**Organization safety is code-enforced, not schema-enforced.** This is the sharpest risk in the
design. A `GenericForeignKey` cannot be an `OrganizationSafeForeignKey`, so nothing in the schema
stops an identifier row in organization A from pointing at an event in organization B. Three things
hold the line: `OrganizationScopedManager` on every read, an explicit organization check on the
target in `replace_for_target`, and the organization column participating in both unique
constraints so a cross-tenant collision is impossible. Reviewers should treat any new read path
that reaches for `original_manager` as a blocker.

**Migration safety.** Phase 1 creates one new table and indexes it while empty, so there is no
`ACCESS EXCLUSIVE` lock on a hot table, no rewrite and no need for `AddIndexConcurrently` —
unlike [0042_availabletime_blockedtime_group_slot.py](../calendar_integration/migrations/0042_availabletime_blockedtime_group_slot.py),
which indexed populated tables and needed both `AddIndexConcurrently` and `atomic = False`. The
`GenericRelation` additions are Python-level and produce no DDL against `calendar_event` or
`external_attendee`. Reverse path: the migration drops the table; verify `migrate <app> <prev>`
before merging.

**ContentType resolution.** Resolve content types through `ContentType.objects.get_for_model` at
call time. Do not cache ids at import time or hard-code them in a migration — they differ across
environments and across a freshly created test database.

**Query-plan risk on the filters.** The reverse lookup is served by the
`extclientid_uniq_system_ident` unique constraint's index, whose leading column is `organization`,
so a filtered query is an index lookup, not a scan. The both-or-neither rule on the filter
arguments exists to keep it that way — an `identifier`-only filter would not use the index prefix.

**Webhook payload is a partner contract.** Phase 4 adds a key to a payload partners already parse.
It is additive and should be safe for any key-based consumer, but partners deserve notice: run the
`handoff-to-client` skill after Phase 4 or Phase 6 to produce the consumer-facing change document.

**Admin writes bypass the service.** The admin is editable (a read-only admin was considered and
not chosen). The admin form calls `normalize_system`, so uniqueness stays sound, but admin edits do
not emit audit records and do not run the allowlist check. That is an accepted, deliberate gap —
`ExternalClientIdentifier` rows created from the admin are a support action, not a business write.
Revisit if it ever becomes routine.

**No backfill.** The table starts empty; no existing data maps into it. Nothing to make idempotent
or resumable.

**Rollback.** Phases 3–6 are pure additions to API surface: revert the phase's commit and the
surface disappears with no data loss, since identifier rows survive in the table. Phase 1 is the
only phase with a data-loss rollback — reversing its migration drops the table and everything in
it. Once any consumer has written identifiers, treat a Phase 1 revert as a data-destroying
operation and dump the table first.

**Deploy ordering.** Single repo, no cross-repo producers. Phases 1 and 2 must land before any of
3–6; 4, 5 and 6 are mutually independent (6 also needs 3).

## 7. Open Questions

1. **No per-record identifier cap.** A consumer can attach unbounded identifiers to one record.
   Recommended default: reject writes past 20 identifiers per record at the input layer. Deferred
   because no consumer is known to need more than a handful — decide before the feature is
   advertised externally. Owner: whoever signs off the public-API docs.
2. **Attendee identifiers in attendee webhook payloads.** Phase 4 adds identifiers to
   `CalendarEventWebhookPayload` only, per the "identifiers in webhook requests for events"
   request; attendee webhooks get the *event's* identifiers through the embedded event object but
   not the attendee's own. Recommended default: add
   `EventAttendeeWebhookPayload.external_client_identifiers` in a follow-up once a consumer asks,
   since it is another partner-visible contract change.
3. **Filters on `calendarGroupEvents`.** Phase 3 adds the lookup arguments to `calendarEvents`
   only. Recommended default: leave `calendarGroupEvents`
   ([queries.py:944](../public_api/queries.py#L944)) alone until a consumer needs it — it takes a
   required group id and a window, so identifier lookup fits it poorly.
4. **`system` normalization is one-way and not enforced by the schema.** Two consumers spelling one
   system with different paths (`https://crm.example.com/api` versus `https://crm.example.com`)
   hold distinct systems. Recommended default: accept it and document the exact normalization rules
   in the public API docs so consumers can be consistent, rather than trying to canonicalize
   further.

## 8. Touch List

**Phase 1 — table**
- @calendar_integration/models.py (edit: new model; `GenericRelation` on [CalendarEvent](../calendar_integration/models.py#L1202) and [ExternalAttendee](../calendar_integration/models.py#L494))
- @calendar_integration/external_client_identifiers.py (new)
- @calendar_integration/migrations/00XX_externalclientidentifier.py (new)
- @calendar_integration/factories.py (edit)
- @calendar_integration/admin.py (edit)
- @calendar_integration/tests/models/test_external_client_identifier.py (new)

**Phase 2 — service**
- @calendar_integration/services/external_client_identifier_service.py (new)
- @calendar_integration/services/dataclasses.py (edit)
- @calendar_integration/services/calendar_event_service.py (edit: [create_event](../calendar_integration/services/calendar_event_service.py#L523), [update_event](../calendar_integration/services/calendar_event_service.py#L829), [_audit_event_write](../calendar_integration/services/calendar_event_service.py#L262))
- @calendar_integration/exceptions.py (edit)
- @di_core/containers.py (edit: register the service)
- @calendar_integration/tests/services/test_external_client_identifier_service.py (new)
- @calendar_integration/tests/services/test_calendar_event_service_identifiers.py (new)

**Phase 3 — public GraphQL**
- @calendar_integration/graphql.py (edit)
- @public_api/mutations.py (edit)
- @public_api/queries.py (edit)
- @public_api/tests/test_external_client_identifiers_graphql.py (new)

**Phase 4 — webhooks**
- @webhooks/services/payloads.py (edit)
- @webhooks/services/webhook_calendar_side_effects.py (edit)
- @webhooks/constants.py (edit)
- @calendar_integration/services/calendar_service_utils.py (edit)
- @webhooks/tests/test_calendar_event_identifier_payloads.py (new)

**Phase 5 — internal REST**
- @calendar_integration/serializers.py (edit)
- @calendar_integration/virtual_models.py (edit)
- @calendar_integration/filtersets.py (edit)
- @schema.yml (regenerated)
- @calendar_integration/tests/test_event_identifier_rest.py (new)

**Phase 6 — updateCalendarEvent**
- @public_api/mutations.py (edit)
- @public_api/permissions.py (edit)
- @public_api/docs_content.py (edit)
- @public_api/tests/test_update_calendar_event_mutation.py (new)

**Phase 7 — tri-state event input (added 2026-08-18)**
- @calendar_integration/services/dataclasses.py (edit: `CalendarEventInputData` — `title`, `description`, `attendances`, `external_attendances` accept `None` = untouched)
- @calendar_integration/services/calendar_event_service.py (edit: `update_event` skips each block when its field is `None`; `create_event` unchanged)
- @public_api/mutations.py (edit: delete `update_calendar_event`'s read-then-resend reconstruction)
- @calendar_integration/services/calendar_service_utils.py (edit: null-safe `serialize_event_external_attendee`)
- @calendar_integration/serializers.py, @public_api/queries.py, @calendar_integration/services/calendar_bundle_service.py (edit: make every `CalendarEventInputData` construction site explicit — enumerate before changing `update_event`)
- @calendar_integration/tests/services/test_calendar_event_service_identifiers.py (edit: re-pin the on-commit query count)
- @public_api/tests/test_update_calendar_event_mutation.py (edit: no-attendee-webhook-on-title-only test)
