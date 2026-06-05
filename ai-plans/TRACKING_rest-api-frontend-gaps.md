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

### Phase 18 — Rooms-sync config via org PATCH + working trigger ✅
- **Status**: done, reviewed (3 layers; Layer 3 credentials-surface, 0 blockers; fixer → TOCTOU guard on post-commit trigger + validate-creds-before-write + test asserts), pushed, PR opened.
- **Model**: sonnet; fixer sonnet. **Branch**: phase-18 (base phase-17). **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/60
- **Key**: nested write-only google_service_account on OrganizationSerializer (secrets never returned; read = email/audience/configured); upsert in update() (atomic, org-level SA via calendar_fk__isnull=True, rotation=delete+create); request_rooms_sync authenticates with the SA (fixes the live 500); 400 when unconfigured; create_organization guarded; on_commit trigger TOCTOU-guarded. Outer gate green (1456), mypy 108. No trailer.

## Current Phase
Phase 19 (FINAL, REVISED per requester) — Service-layer hardening.
1. on_commit at the service layer: wrap the `.delay(...)` inside CalendarService.request_calendar_sync, request_organization_calendar_resources_import, request_calendars_import. GOAL = exactly-ONE deferral, service-owned. View-layer on_commit wraps added earlier (Phase 4 request_import; Phase 7/18 sync_rooms+transition+create_organization) become redundant → remove them so callers invoke the service directly (avoid double-layer). Update tests (captureOnCommitCallbacks / patched on_commit).
2. is_active filtering: bundle-create + resource-allocation serializer querysets + public GraphQL calendars query exclude disabled calendars; keep already-selected disabled (Phase 10 union pattern), bar new disabled.

## Remaining Phases
(none after 19)

## Reusable infra
- IsOrganizationAdmin; get_active_organization_membership; OrganizationSerializer + OrganizationViewSet.update (admin-gated, already overridden in Phase 7 with select_for_update + transition).
- OrganizationService.request_rooms_sync (to fix in 18); GoogleCalendarServiceAccount model (calendar_integration/models.py ~1541; org FK, calendar FK nullable, email/audience/public_key, private_key_id+private_key EncryptedCharField).
- CalendarService.authenticate(account, organization); is_authenticated_calendar_service requires account+calendar_adapter.

## Deferred
None.
