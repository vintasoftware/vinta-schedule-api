# Availability — `AvailableTime` and `BlockedTime`

> Source: [calendar_integration/models.py](../../calendar_integration/models.py) — `AvailableTime`, `BlockedTime`, `Calendar.manage_available_windows`.
> Queryset: `CalendarQuerySet.only_calendars_available_in_ranges` in [calendar_integration/querysets.py](../../calendar_integration/querysets.py).

A calendar's "is this slot bookable?" answer is computed from three
sources:

1. **`CalendarEvent` rows** that overlap the slot — already-booked time.
2. **`BlockedTime` rows** — explicit unavailability (vacations, surgeries
   from another system, paged emergencies).
3. For calendars with `manage_available_windows=True`,
   **`AvailableTime` rows** — the *only* periods that count as open.

For calendars with `manage_available_windows=False`, anything that isn't
an event or a `BlockedTime` is implicitly available (during reasonable
hours; the upstream provider is the source of truth).

## `AvailableTime` — opening hours

Positive declarations of when a calendar is open. Only meaningful for
calendars with `manage_available_windows=True`.

Healthcare examples:

- **Dr. Patel's outpatient clinic**: `AvailableTime` for every Tuesday
  9 AM – 12 PM and Thursday 1 PM – 5 PM, recurring weekly.
- **MRI Suite A**: `AvailableTime` for weekdays 7 AM – 9 PM (the rad-tech
  shift); nights and weekends are not bookable at all.
- **Telehealth Saturday clinic**: a single recurring `AvailableTime`
  declaring 9 AM – 1 PM on `BYDAY=SA`.
- **Locum coverage**: ad-hoc `AvailableTime` rows for the dates a locum is
  on-site, no recurrence.

`AvailableTime` is itself a recurring object (it inherits `RecurringMixin`
— see [recurrence.md](recurrence.md)). Most opening-hours patterns are
expressed as a single recurring row with a weekly `RecurrenceRule`.

## `BlockedTime` — explicit unavailability

Negative declarations: even if the calendar would otherwise look open,
this period is not bookable.

Healthcare examples:

- **Vacations**: Dr. Lee away June 12 – June 23.
- **Conference / CME leave**: a four-day block while a surgeon attends a
  conference.
- **Provider-side blocks**: the cleaning crew has the OR for 30 minutes
  between cases — block it so booking flows skip it.
- **External-system busy time**: an event imported from the surgeon's
  Outlook account that we don't manage but must respect — synced as a
  `BlockedTime` so it counts as busy without polluting the local event
  list (see `external_id`, `bundle_calendar`, and the sync flow in
  [calendar_integration/services/calendar_service.py](../../calendar_integration/services/calendar_service.py)).
- **Bundle/group propagation**: when a booking is made on the primary
  calendar of a bundle or group, every other selected calendar gets a
  `BlockedTime` so it shows as busy without duplicating event data.

`BlockedTime` is also recurring (e.g. "the standing OR cleaning slot
8:00–8:30 every weekday" can be one recurring `BlockedTime`).

## Computing availability — `only_calendars_available_in_ranges`

The canonical "is this calendar free for these windows?" check. Given a
list of `(start, end)` ranges, it returns the calendars that have:

- No overlapping `CalendarEvent` (including expanded recurring
  occurrences).
- No overlapping `BlockedTime` (also recurrence-expanded).
- For `manage_available_windows=True` calendars, *some* `AvailableTime`
  fully covering the range.

Group-level availability ([calendar-groups.md](calendar-groups.md))
delegates to this method per range, so any improvement here flows into
group bookability automatically.

### Worked example — booking an MRI

A patient needs a 45-minute MRI on a Wednesday afternoon. The booking
flow asks: "Is `MRI Suite A` free for any 45-minute window between 1 PM
and 5 PM Wednesday?"

- `AvailableTime`: weekdays 7 AM – 9 PM (managed window). ✔️
- `BlockedTime`: Tuesday-night maintenance, no Wednesday entry. ✔️
- `CalendarEvent`s: existing MRI scans at 1 PM (60 min), 3 PM (30 min),
  4 PM (45 min). ❌ for those windows.

`only_calendars_available_in_ranges` walks the candidate windows
(2:00, 2:15, …, 4:15) and returns the suite as available for 2:00, 2:15,
3:30, and 3:45.

## Managed vs. unmanaged calendars — when to flip the bit

| Use `manage_available_windows=True` | Use `manage_available_windows=False` |
|---|---|
| Clinic provider with a *publishable* schedule. | Hospital staff with a normal Outlook calendar; we want "free if no event." |
| Resource calendars that are bookable only during specific hours (MRI suite, infusion bay). | Resource calendars used 24/7 (a portable monitor that's always available unless explicitly booked). |
| Virtual calendars for telehealth that should be bookable only during clinic hours. | Calendars synced from external systems that already publish their own busy/free. |

## Recurrence interactions

`AvailableTime` and `BlockedTime` both inherit `RecurringMixin`, so all of
the patterns in [recurrence.md](recurrence.md) apply: recurring weekly
clinics, single-occurrence cancellations ("clinic is closed Dec 24
this year only"), and bulk modifications ("starting next quarter we
move the Thursday clinic to Friday") work the same way they do for
events.

## Bulk modifications and `*_with_bulk_modifications`

When a recurring `AvailableTime` is split (e.g. "from April 1 onward,
Tuesday clinic shifts from 9–12 to 8–11"), the original row is truncated
with `UNTIL=2026-03-31` and a continuation row is created. Helpers like
`only_calendars_available_in_ranges_with_bulk_modifications` traverse
both the original and any continuations so callers see the consolidated
schedule. See [recurrence.md](recurrence.md#bulk-modifications-and-splits)
for the full mechanism.
