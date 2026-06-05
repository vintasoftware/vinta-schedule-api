# Tracking — REST API Frontend Gaps (amendment: phases 17–19)

- **Plan**: ai-plans/2026-06-05-REST_API_FRONTEND_GAPS_IMPLEMENTATION_PLAN.md (Amendment section)
- **Plan id**: rest-api-frontend-gaps
- **Run options**: auto-flow; inline PR comments ON
- **Branch pattern**: stacked on phase-16 (last of the original run)
- **mypy baseline**: 108 full-project errors (pre-existing); keep at 108.
- **POLICY REMINDER**: NO `Co-Authored-By` trailers in commits (project forbids; one slipped into Phase 17's first commit and was stripped — watch for it).

## Completed Phases (this amendment)
### Phase 17 — Resend organization invitation ✅
- Model haiku; fixer haiku. Branch phase-17 (base phase-16). PR: https://github.com/vintasoftware/vinta-schedule-api/pull/59
- `POST /invitations/{id}/resend/` re-invokes invite_user_to_organization (resets token+expiry, resends email); accepted→400; org-scoped. Layer 3 → removed broad except (was masking 500s) + added real reset test. Stripped an AI co-author trailer. Outer gate green (1442), mypy 108.

## Current Phase
Phase 18 (REVISED per requester) — Rooms-sync config via org Partial Update + working trigger.
- Config persists via `PATCH /organizations/{id}/` (nested google_service_account on OrganizationSerializer; private_key/private_key_id WRITE-ONLY, never returned). NOT a separate endpoint.
- Fix the trigger: request_rooms_sync authenticates with the org's GoogleCalendarServiceAccount (NOT initialize_without_provider) then request_organization_calendar_resources_import; 400 (not 500) when unconfigured; create_organization must not crash when unconfigured.
- Tier 3. Org update already admin-gated (Phase 7).

## Remaining Phases
Phase 19 (REVISED per requester) — Service-layer hardening:
- Wrap .delay in transaction.on_commit inside CalendarService.request_calendar_sync, request_organization_calendar_resources_import, request_calendars_import (fix pre-existing race; adjust tests via captureOnCommitCallbacks).
- is_active filtering on bundle-create/resource-allocation serializer querysets + public GraphQL calendars query (Phase 9 follow-up); keep already-selected disabled (Phase 10 union pattern), bar new disabled.

## Reusable infra
- IsOrganizationAdmin; get_active_organization_membership; OrganizationSerializer + OrganizationViewSet.update (admin-gated, already overridden in Phase 7 with select_for_update + transition).
- OrganizationService.request_rooms_sync (to fix in 18); GoogleCalendarServiceAccount model (calendar_integration/models.py ~1541; org FK, calendar FK nullable, email/audience/public_key, private_key_id+private_key EncryptedCharField).
- CalendarService.authenticate(account, organization); is_authenticated_calendar_service requires account+calendar_adapter.

## Deferred
None.
