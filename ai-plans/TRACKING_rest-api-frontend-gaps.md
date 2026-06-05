# Tracking — REST API Frontend Gaps (amendment: phases 17–19)

- **Plan**: ai-plans/2026-06-05-REST_API_FRONTEND_GAPS_IMPLEMENTATION_PLAN.md (Amendment section)
- **Plan id**: rest-api-frontend-gaps
- **Resumed**: 2026-06-05 (follow-up gaps after phases 0–16 shipped, PRs #42–#58)
- **Run options**: auto-flow; inline PR comments ON
- **Branch pattern**: stacked on phase-16 (last of the original run)
- **mypy baseline**: 108 full-project errors (pre-existing); every phase must keep it at 108.

## Context for these phases
Two gaps: (1) resend invitation; (2) rooms-syncing unusable — `request_rooms_sync` + `create_organization` call initialize_without_provider (account=None) then request_organization_calendar_resources_import which requires a provider-authed service → RAISES; and no API to configure the org's GoogleCalendarServiceAccount.
Decisions: fix trigger + add config endpoint; fix create_organization too; append phases.

## Completed Phases (this amendment)
(none yet)

## Current Phase
Phase 17 — Resend organization invitation (admin). resend @action on OrganizationInvitationViewSet → re-invoke invite_user_to_organization (already resets token+expiry+resends email); refuse if accepted. Tier 2.

## Remaining Phases
18 (configure org GoogleCalendarServiceAccount — admin, write-only secrets, Tier 3), 19 (authenticate rooms-sync with the service account; fix request_rooms_sync + create_organization; 400 not 500 when unconfigured — Tier 3).

## Reusable infra (from phases 0–16)
- `IsOrganizationAdmin` (organizations/permissions.py) — admin gate, collection + object (has_object_permission has hasattr(organization_id) fallback).
- `get_active_organization_membership(user)` (organizations/models.py).
- public_api REST surface pattern (SystemUserTokenViewSet) for new admin endpoints.
- `OrganizationService.request_rooms_sync` (to fix in 19); `OrganizationService.invite_user_to_organization` (reset+resend, reuse in 17).
- GoogleCalendarServiceAccount model: calendar_integration/models.py ~1541 (email, audience, public_key, private_key_id [Encrypted], private_key [Encrypted], org FK, calendar FK nullable).

## Deferred
None.
