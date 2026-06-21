# Tracking — Calendar Event ICS Export

- **Plan**: `ai-plans/2026-06-20-CALENDAR_EVENT_ICS_EXPORT_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-06-21
- **Last updated**: 2026-06-21
- **Feature flag**: none (purely additive read-only surface)

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: false (PR description only)
- `use_worktree`: true
- `worktree_path`: `.claude/worktrees/plan-calendar-event-ics-export`
- `worktree_branch`: `plan/calendar-event-ics-export`
- `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-calendar-event-ics-export.yaml`
- `commit_strategy_resolved`: modular-commits
- `plan_branch`: `plan/calendar-event-ics-export`
- `pr`: [#158](https://github.com/vintasoftware/vinta-schedule-api/pull/158) (one PR for the whole plan)

## Completed Phases

### Phase 1 — Add `icalendar` dependency + core ICS builder ✅
- **Status**: complete, reviewed (3-layer), outer gate green (2980 passed).
- **Model**: claude-haiku-4-5 (plan tier: Tier 2). Reviewer: reviewer agent. Fixer: fixer agent.
- **Commits**:
  - `e02f567` chore(calendar): add icalendar dependency for ICS export
  - `7d9f65c` feat(calendar): add ICS builder service for calendar events
  - `68eab6e` chore(calendar): pin icalendar below 8.0
  - `68b84e7` refactor(calendar): tighten ICS builder dtstamp + strengthen tests
- **Summary**: Added `CalendarEventICSService.build_ics(event) -> bytes` in
  `calendar_integration/services/ics_service.py` — a pure, stateless builder emitting a single-VEVENT
  iCalendar with PRODID/VERSION, UID (`external_id` else `event-{id}@vinta-schedule`), SUMMARY,
  DTSTART/DTEND (tz-aware GeneratedFields), DTSTAMP (from `event.modified`), DESCRIPTION, STATUS:CONFIRMED,
  SEQUENCE (`int(event.modified.timestamp())`). `CalendarEvent` has NO `location` field → LOCATION omitted.
  `icalendar>=7.1.3,<8` (BSD-2-Clause, license-checked OK). 15 unit tests, all round-trip via
  `icalendar.Calendar.from_ical`. ORGANIZER/ATTENDEE/RRULE/EXDATE/CANCELLED deferred to Phase 2.
- **Review findings**: 0 BLOCKER, 6 SHOULD-FIX (version pin upper bound, dead `datetime.utcnow()` fallback
  removed, SEQUENCE exact-equality assertion, special-char round-trip assertion, tz-aware assertion,
  missing-required-field validation tests) — all fixed. 2 NITs (DI deferral noted per plan; tautological
  assert removed).

### Phase 2 — Add participants + recurrence to the builder ✅
- **Status**: complete, reviewed (3-layer), outer gate green (2992 passed).
- **Model**: claude-sonnet-4-6 (plan tier: Tier 3). Reviewer + fixer agents.
- **Commits**:
  - `25cb6c6` feat(calendar): add organizer + attendees to ICS builder
  - `86ce23b` fix(calendar): correct EXDATE timezone and organizer selection in ICS builder
- **Summary**: Extended `build_ics` with ORGANIZER (calendar default owner via
  `CalendarOwnership.is_default`, deterministic fallback by `membership_user_id`, read from the
  prefetch cache — no N+1), ATTENDEE lines (internal via `event.attendances → membership.user.email`;
  external via `event.external_attendances → external_attendee.email`; PARTSTAT from `RSVPStatus`,
  ROLE=REQ-PARTICIPANT), RRULE (`recurrence_rule.to_rrule_string()` → `icalendar.vRecur.from_ical`),
  EXDATE (cancelled `EventRecurrenceException.exception_date`), STATUS:CANCELLED (defensive, documented
  currently-unreachable guard). DTSTART/DTEND/EXDATE all localized to `event.timezone` so they share a
  TZID (RFC 5545 §3.8.5.1 — fixes a latent bug where Google Calendar drops mismatched-TZID EXDATEs).
  Required prefetch set documented in the service module docstring.
- **Review findings**: 1 BLOCKER (EXDATE/DTSTART TZID mismatch — fixed by localizing DTSTART/DTEND +
  EXDATE to the event tz), 2 SHOULD-FIX (nondeterministic + N+1-causing `ownerships.first()` → default-owner
  from prefetch cache; unreachable STATUS:CANCELLED test labeled as a future-proofing guard), 2 NITs
  (return type, docstring) — all addressed.
- **Notes**: pre-existing `mypy .` baseline has 258 errors unrelated to this change (gate is
  `check --deploy` + `pytest`, not mypy). A pre-existing `CalendarEventFactory.create_recurring_event`
  double-`timezone` kwarg bug was worked around in-test (not fixed — out of scope).

## Current Phase
Phase 3 — REST ICS download action (next).

## Remaining Phases
- Phase 3 — REST ICS download action (Tier 2).
- Phase 4 — Public GraphQL `eventIcs` query (Tier 3).

## Deferred Phases
None (no cross-repo phases; no flag-removal phase — no flag declared).
