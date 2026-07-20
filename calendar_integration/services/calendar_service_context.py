"""Shared authentication context passed to all CalendarService sub-services.

The facade (``CalendarService``) builds one instance of this after ``authenticate()``
or ``initialize_without_provider()`` and hands it to every sub-service it constructs.
Sub-services read the context instead of reaching back into the facade, so they are
decoupled and independently testable.

Note: the facade *also* keeps its own instance attributes (``organization``,
``account``, etc.) because the type-guards in ``type_guards.py`` inspect those
attributes on the facade directly. The context is a separate, immutable snapshot.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from allauth.socialaccount.models import SocialAccount

    from audit.services import AuditService
    from calendar_integration.models import GoogleCalendarServiceAccount
    from calendar_integration.services.calendar_permission_service import CalendarPermissionService
    from calendar_integration.services.calendar_side_effects_service import (
        CalendarSideEffectsService,
    )
    from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
    from organizations.models import Organization
    from payments.services.entitlement_service import EntitlementService
    from public_api.models import SystemUser
    from users.models import User


@dataclasses.dataclass(frozen=True)
class CalendarServiceContext:
    """Immutable snapshot of the auth state built by ``CalendarService`` after authentication.

    All fields are optional because the context is built in two states:
    - **authenticated** (via ``authenticate()``): all fields set.
    - **initialized without provider** (via ``initialize_without_provider()``): only
      ``organization`` and ``user_or_token`` are set; ``account``, ``calendar_adapter``,
      ``calendar_permission_service``, and ``calendar_side_effects_service`` may be ``None``.
    """

    organization: Organization | None
    user_or_token: User | str | SystemUser | None
    account: SocialAccount | GoogleCalendarServiceAccount | None
    calendar_adapter: CalendarAdapter | None
    calendar_permission_service: CalendarPermissionService | None
    calendar_side_effects_service: CalendarSideEffectsService | None
    # Audit trail recorder, threaded from the facade so sub-services can emit audit
    # records for the business writes they perform. Defaults to None so contexts built
    # directly in tests (without DI) still construct.
    audit_service: AuditService | None = None
    # Phase 6b: pre-paid limit enforcement, threaded from the facade so sub-services
    # (bundle, availability, sync) can guard their own creation paths without each
    # taking a separate DI-injected constructor parameter. Defaults to None so
    # contexts built directly in tests (without DI) still construct; a guarded method
    # skips its check when this is None, mirroring ``audit_service``'s no-op-when-absent
    # convention -- the facade (the only production entry point) always injects a real
    # instance, so this only matters for tests that build a sub-service directly.
    entitlement_service: EntitlementService | None = None
    # Mirrors ``CalendarService._bypass_entitlement_limits``, set by
    # ``authenticate(bypass_limits=True)``. Threaded into the snapshot so a sub-service
    # guard honours the facade's bypass mode too: before this existed, a facade in
    # bypass mode still had its provider-entitlement gate skipped (that check reads the
    # facade attribute directly) while ``CalendarEventService``'s post-paid guard --
    # which reads ``entitlement_service`` off the context -- kept enforcing, so a
    # service explicitly placed in bypass mode was still blockable.
    bypass_entitlement_limits: bool = False
