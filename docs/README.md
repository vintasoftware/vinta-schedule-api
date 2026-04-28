# Vinta Schedule — Domain Documentation

This folder explains the scheduling concepts that live in
[calendar_integration/](../calendar_integration/). The examples are deliberately
drawn from the **health tech** world — outpatient clinics, surgical theatres,
on-call shifts, telehealth — because that's where most of the trickier
constraints (multi-resource bookings, recurring shifts, exceptions, splits)
actually show up.

If a term in any of these documents is unfamiliar, check
[glossary.md](glossary.md). Whenever a new domain term is introduced anywhere in
the docs, add it to the glossary in the same change.

## How to read these docs

Start with **Calendars**, then **Availability**, then **Events**. Those three
together describe a "single-resource" booking world. The remaining documents
layer multi-resource scheduling on top:

| Order | Document | What it covers |
|-------|----------|----------------|
| 1 | [concepts/calendars.md](concepts/calendars.md) | The four calendar types (personal, resource, virtual, bundle), ownership, providers. |
| 2 | [concepts/availability.md](concepts/availability.md) | `AvailableTime`, `BlockedTime`, managed vs. unmanaged availability. |
| 3 | [concepts/events.md](concepts/events.md) | `CalendarEvent`, attendances, external attendees, resource allocations. |
| 4 | [concepts/recurrence.md](concepts/recurrence.md) | Recurrence rules, exceptions, bulk modifications, splits. |
| 5 | [concepts/calendar-bundles.md](concepts/calendar-bundles.md) | Bundle calendars: a single bookable façade over many calendars. |
| 6 | [concepts/calendar-groups.md](concepts/calendar-groups.md) | `CalendarGroup`, slots, memberships, group bookings. |
| ⭐ | [glossary.md](glossary.md) | Quick definitions for every domain term. |

## A 30-second tour

A patient books a 30-minute consult. To make that booking:

1. The clinic's **calendars** describe who/what can be booked — physicians have
   *personal* calendars, exam rooms have *resource* calendars, telehealth
   sessions have *virtual* calendars.
2. **Availability** says when each of those is free — `AvailableTime` is a
   positive declaration ("Dr. Lee is open"); `BlockedTime` is a negative one
   ("the MRI suite is in use"); both can repeat.
3. The booking itself is a **CalendarEvent** with attendances and
   resource allocations.
4. Most clinic appointments need **multiple** calendars at once — a doctor *and*
   a room. That's what **CalendarGroups** model: a template that says "to make
   this booking, pick one calendar from each of these slots."
5. **CalendarBundles** are the older one-shot mechanism for the same problem
   (single façade calendar that owns several child calendars). Groups
   generalize the idea by adding pools and per-slot picking.
6. **Recurrence** lets shifts ("Dr. Patel rounds Mon/Wed/Fri 7–11 AM") and
   weekly clinics live as one row with thousands of occurrences, including
   single-day exceptions ("cancel the Wed before Christmas") and bulk
   modifications ("from March onward, move it to 8 AM").
