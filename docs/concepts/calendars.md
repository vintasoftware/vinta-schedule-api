# Calendars

> Source: [calendar_integration/models.py](../../calendar_integration/models.py) — `Calendar`, `CalendarOwnership`, `ChildrenCalendarRelationship`.

A **Calendar** is the smallest bookable unit in the system. Every event,
blocked time, and available window hangs off exactly one `Calendar`. It is
*also* the integration point with external providers (Google, Microsoft
Outlook, Apple, ICS feeds) — when a calendar is backed by a provider, the
system keeps it in sync.

A `Calendar` always belongs to one `Organization` (multi-tenant isolation —
see [glossary.md](../glossary.md#organization)), and may have one or more
**owners** (users) through `CalendarOwnership`.

## Calendar types

The `calendar_type` field (see [`CalendarType`](../../calendar_integration/constants.py))
describes what the calendar represents in the real world. Picking the right
type matters: it changes how availability is computed and how bookings are
distributed.

### `PERSONAL`

A calendar belonging to a person. In healthcare:

- **Dr. Patel's clinic calendar** — outpatient slots she controls.
- **Nurse Ana's shift calendar** — when she's on the floor.
- **Surgeon Dr. Okafor's OR calendar** — when he's available to operate.

Personal calendars are typically owned by exactly one user via
`CalendarOwnership`, and that owner usually has their external provider
(Google/Microsoft) authenticated so events sync both ways.

### `RESOURCE`

A calendar belonging to a *thing*. The thing can be booked but it doesn't
"attend" the event the way a person does.

Healthcare examples:

- **Exam Room 3** — a consult room.
- **MRI Suite A** — a piece of equipment + the space it occupies.
- **OR-2** — operating theatre two.
- **Telemetry monitor #14** — a portable device.
- **The hospital's only colonoscopy cart** — true bottleneck resources.

Resource calendars optionally have a `capacity` (e.g. an infusion bay with
six chairs). They typically have no human owners; bookings touch them via
`ResourceAllocation` rather than `EventAttendance`.

### `VIRTUAL`

A calendar that represents an online meeting/event endpoint rather than a
physical place.

Healthcare examples:

- **Telehealth session** — the calendar that "owns" the Zoom/Teams/Doxy.me
  link a patient connects to.
- **Virtual tumour-board** — the recurring oncology case review held over
  video.
- **Remote second-opinion consults**.

### `BUNDLE`

A calendar that aggregates other calendars and presents them as a single
bookable façade. Bookings made on the bundle propagate to every child
calendar (as full events on internal/same-provider calendars, or as
`BlockedTime` rows on cross-provider ones).

See [calendar-bundles.md](calendar-bundles.md) for the full mechanism, and
[calendar-groups.md](calendar-groups.md) for the newer, more flexible
approach.

## Providers

`provider` (see `CalendarProvider`) determines where the canonical event
data lives and how it syncs:

- `INTERNAL` — owned entirely by Vinta Schedule. No external sync.
- `GOOGLE`, `MICROSOFT`, `APPLE`, `ICS` — backed by an external provider.
  Two-way sync runs through the relevant adapter under
  [calendar_integration/services/calendar_adapters/](../../calendar_integration/services/calendar_adapters/).

A clinic typically mixes providers: physicians use Google/Microsoft (their
hospital email), but resource calendars and virtual telehealth calendars are
often `INTERNAL`.

## `manage_available_windows`

By default a calendar's availability is whatever the *external* provider
reports — busy when there are events on it, free otherwise. Set
`manage_available_windows = True` to flip the model:

- The calendar is **only** available during explicitly-declared
  `AvailableTime` windows (see [availability.md](availability.md)).
- Outside those windows the calendar is treated as fully booked, even if the
  upstream provider thinks it's open.

Use it for any "I work these hours, period" calendar:

- A physician who only sees patients Tue/Thu 9 AM – noon.
- A resource ("Procedure Cart B") that is only on the schedule when a
  trained tech is rostered.
- A virtual telehealth calendar that is only bookable during published
  clinic hours.

## `accepts_public_scheduling`

When `True`, this calendar can be booked by external (unauthenticated)
people through a public scheduling link. The flag is independent of the
calendar type — a clinic might keep `accepts_public_scheduling=False` on
the surgeon's OR calendar but `=True` on a triage nurse's screening
calendar.

## Ownership

`CalendarOwnership` links users to calendars. Owners can manage the
calendar (create events, declare availability, etc.). One ownership row per
(calendar, user); `is_default=True` marks a user's "drop new events here by
default" calendar.

Examples:

- A solo PCP owns one `PERSONAL` calendar; `is_default=True`.
- A locum tenens physician working across two hospitals owns two `PERSONAL`
  calendars in different organizations.
- A resource calendar (MRI Suite A) usually has *zero* owners — its
  scheduling rules come from the org's policies, not a person.

## Bundles vs. Groups (quick orientation)

There are **two** mechanisms for "one booking, many calendars": **bundles**
([calendar-bundles.md](calendar-bundles.md)) and **groups**
([calendar-groups.md](calendar-groups.md)). Bundles came first and use a
dedicated `BUNDLE` calendar with a fixed list of child calendars. Groups
are the newer model: they don't need a façade calendar and they support
**pools** of candidate calendars per role ("any physician + any room"),
which is what most clinic-style booking flows actually need. New work
should generally prefer groups; bundles remain for cases that already use
them or where the external-provider-sync semantics of a single primary
calendar are desirable.
