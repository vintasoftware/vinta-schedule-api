# Recurrence

> Source: [calendar_integration/models.py](../../calendar_integration/models.py) — `RecurrenceRule`, `RecurringMixin`, `EventRecurrenceException`, `BlockedTimeRecurrenceException`, `AvailableTimeRecurrenceException`, `*BulkModification` models.
> Helpers: [calendar_integration/recurrence_utils.py](../../calendar_integration/recurrence_utils.py).

Most things people schedule are recurring: clinics run weekly, on-call
shifts rotate, OR blocks repeat, conference rooms host the same meeting
every Monday. Storing one row per occurrence would be wasteful and
fragile. Instead, each `CalendarEvent`, `BlockedTime`, and
`AvailableTime` carries an optional `RecurrenceRule` — an [RFC 5545
RRULE](https://datatracker.ietf.org/doc/html/rfc5545#section-3.8.5.3) —
and the system **expands** occurrences on demand inside whatever date
range the caller asks about.

## The three layers

### 1. The `RecurrenceRule`

Stores the standard RRULE fields: `frequency`, `interval`, `count`,
`until`, `by_weekday`, `by_month_day`, `by_month`, `by_hour`, `by_minute`,
etc. `to_rrule_string()` and `from_rrule_string(...)` round-trip the
canonical string form so external systems (Google, Outlook) can read the
same rule.

Healthcare examples:

- **`FREQ=WEEKLY;BYDAY=TU,TH`** — Dr. Patel's outpatient clinic, every
  Tuesday and Thursday.
- **`FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0`** — morning
  rounds at 8 AM on weekdays.
- **`FREQ=MONTHLY;BYMONTHDAY=15`** — monthly QA meeting on the 15th.
- **`FREQ=WEEKLY;INTERVAL=2;BYDAY=MO`** — bi-weekly on-call rotation
  every other Monday.

### 2. The recurring object (`RecurringMixin`)

`CalendarEvent`, `BlockedTime`, and `AvailableTime` each inherit
`RecurringMixin`, which contributes:

- `start_time` / `end_time` (timezone-aware computed fields backed by
  `start_time_tz_unaware` / `end_time_tz_unaware` + `timezone`).
- `recurrence_rule` (1:1 to `RecurrenceRule`).
- `recurrence_id` (the original start of the occurrence this row
  represents — used by exception rows).
- `parent_recurring_object` (FK back to the master row when this row is
  a materialized exception or instance).
- `is_recurring_exception` (set on rows that override a single
  occurrence).
- `bulk_modification_parent` (FK to the original master when this row is
  a continuation of a split — see below).

The mixin's `get_occurrences_in_range(start, end)` and
`get_occurrences_in_range_with_bulk_modifications(start, end)` methods
return a list of in-memory occurrences (cancelled ones filtered out,
modified ones substituted in).

### 3. Exceptions

A single occurrence can be **cancelled** or **modified** without
breaking the parent's rule. Each recurring model has a paired exception
model (`EventRecurrenceException`, `BlockedTimeRecurrenceException`,
`AvailableTimeRecurrenceException`) with two fields that matter:

- `exception_date` — the original `recurrence_id` of the occurrence
  being excepted.
- `is_cancelled` — `True` to skip this occurrence; `False` to substitute
  a modified row.
- When `is_cancelled=False`, a separate "modified" row (e.g.
  `modified_event`) stores the overridden details (a new time, a new
  title, etc.).

Healthcare examples:

- **Holiday cancellation**: weekly Wednesday clinic exists year-round,
  but the Wednesday before Christmas is cancelled. One exception row,
  `is_cancelled=True`, `exception_date=2026-12-23T09:00`.
- **One-off reschedule**: the standing 8 AM rounds move to 9 AM on the
  morning after a hospital-wide outage. One exception row,
  `is_cancelled=False`, plus a modified `CalendarEvent` with the new
  time.
- **Substitute coverage**: Dr. Patel's Tuesday clinic is covered by
  Dr. Singh on one day. Modified exception event with a different
  attendance and resource.

## Bulk modifications and splits

Single-occurrence exceptions are fine for "this Wednesday only," but for
"every Wednesday from now on" they don't scale. The system models that
case as a **split**:

1. **Truncate** the original master's recurrence at the modification
   date by setting `UNTIL` (helper:
   `RecurrenceRuleSplitter.truncate_rule_until_date`).
2. **Create a continuation** — a new recurring object with a derived
   `RecurrenceRule` starting at the modification date
   (`RecurrenceRuleSplitter.create_continuation_rule`). The continuation
   carries `bulk_modification_parent` pointing back at the original.
3. **Track the split** in a `*BulkModification` row
   (`EventBulkModification`, `BlockedTimeBulkModification`,
   `AvailableTimeBulkModification`) so callers can reason about the
   chain.

The original lives unchanged before the modification date; the
continuation expresses the new rule afterwards. Helpers like
`get_occurrences_in_range_with_bulk_modifications` and
`only_calendars_available_in_ranges_with_bulk_modifications` walk both
when computing occurrences/availability so callers see the
post-modification reality.

### Healthcare examples — splits

- **Permanent clinic move**: from April 1, the Thursday afternoon clinic
  becomes a Friday morning clinic. The pre-April Thursday rule is
  truncated; an April-onward Friday continuation is created.
- **Cancel the whole rest of a series**: the Monday morning grand
  rounds are discontinued mid-quarter. Truncate the rule's `UNTIL` at
  the cutoff date and mark `is_bulk_cancelled=True` on the
  `*BulkModification` record. No continuation is created.
- **Schedule change at residency rotation**: every six weeks the
  resident rotation changes, so a recurring "didactics" event splits
  into a new master with new attendees while keeping older
  occurrences intact.

## Choosing the right tool

| Situation | Use |
|-----------|-----|
| Cancel one Wednesday's clinic | `EventRecurrenceException` with `is_cancelled=True`. |
| Move one Wednesday's clinic to Friday | Exception with `is_cancelled=False` + modified event. |
| Move every future Wednesday clinic to Friday | Bulk modification (split). |
| Add a one-off `BlockedTime` for a snow day | Single `BlockedTime` row, no recurrence. |
| Permanently change clinic hours for one provider | Split the recurring `AvailableTime`. |

## Performance notes

- Occurrence expansion is bounded by a `max_occurrences` argument
  (default `10000`). Callers that ask for very long windows of very
  fine-grained recurrences should pass an appropriate cap.
- Querysets like `annotate_recurring_occurrences_on_date_range` push
  expansion into SQL via Postgres functions so the API doesn't have to
  pull master rows into Python first.
- The `*_with_bulk_modifications` family is more expensive than the
  base methods because it walks continuation chains. Only use it when
  the caller needs the consolidated view.
