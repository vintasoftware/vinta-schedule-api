"""ConsentService — records and queries UserConsent (audit-grade proof).

Usage (DI-injected, mirrors ``organizations.services.OrganizationService``):

    consent = self.consent_service.record_consent(
        user,
        PolicyDocumentType.SMS_CONSENT,
        source=ConsentSource.SIGNUP_FORM,
        ip=request.META.get("REMOTE_ADDR"),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )
"""

import logging
from typing import Annotated

from dependency_injector.wiring import Provide, inject

from audit.constants import AuditAction
from audit.services import AuditService
from legal.exceptions import NoPolicyDocumentError
from legal.models import PolicyDocument, UserConsent
from organizations.models import get_active_organization_membership
from users.models import User


logger = logging.getLogger(__name__)


class ConsentService:
    """Service for recording and querying per-user policy consent.

    ``UserConsent`` is a global, non-tenant-scoped model (consent is per-user,
    not per-organization — see ``legal.models.UserConsent``). ``AuditService``,
    however, records business writes against a tenant-scoped ``Audit`` table
    that requires an ``organization_id``. To bridge this, consent grants are
    audited against the user's *active* organization membership when one
    exists (resolved via ``get_active_organization_membership``); when the
    user has no organization yet (e.g. mid-signup, before tenant provisioning)
    the audit emission is skipped — the ``UserConsent`` row itself already
    carries the audit-grade proof fields (``accepted_at``, exact
    ``policy_document`` version, ``ip_address``, ``user_agent``, ``source``),
    so consent proof does not depend on the org-scoped audit trail.
    """

    @inject
    def __init__(
        self,
        audit_service: Annotated[AuditService, Provide["audit_service"]],
    ) -> None:
        self.audit_service = audit_service

    def record_consent(
        self,
        user: User,
        document_type: str,
        *,
        source: str,
        ip: str | None = None,
        user_agent: str = "",
    ) -> UserConsent:
        """Record that `user` accepted the latest published version of `document_type`.

        Resolves the latest ``PolicyDocument`` of `document_type` (raises
        ``NoPolicyDocumentError`` when none has ever been published) and
        creates a new, version-pinned ``UserConsent`` row. Emits an
        ``AuditService`` CREATE record when the user has an active
        organization membership.

        :param user: The user granting consent.
        :param document_type: One of ``PolicyDocumentType``'s values.
        :param source: One of ``ConsentSource``'s values.
        :param ip: The client IP address that submitted the consent, if known.
        :param user_agent: The client user-agent string, if known.
        :raises NoPolicyDocumentError: When no published ``PolicyDocument``
            exists for `document_type`.
        :return: The created, persisted ``UserConsent`` instance.
        """
        document = PolicyDocument.objects.latest_for(document_type)
        if document is None:
            raise NoPolicyDocumentError(
                f"No published policy document exists for document_type={document_type!r}."
            )

        consent = UserConsent.objects.create(
            user=user,
            policy_document=document,
            source=source,
            ip_address=ip,
            user_agent=user_agent,
        )

        self._audit_consent_created(consent)

        return consent

    def has_sms_consent(self, user: User) -> bool:
        """Return True if `user` has ever accepted any version of SMS_CONSENT.

        Delegates to ``UserConsentManager.has_sms_consent`` (consent-once-ever:
        any prior SMS-consent row satisfies the gate, regardless of version).
        """
        return UserConsent.objects.has_sms_consent(user)

    def _audit_consent_created(self, consent: UserConsent) -> None:
        """Emit an AuditService CREATE record for a newly-created UserConsent.

        No-op (with a log line) when the consenting user has no active
        organization membership — see the class docstring for why that is
        safe: the ``UserConsent`` row is itself the audit-grade proof.
        """
        membership = get_active_organization_membership(consent.user)
        if membership is None:
            logger.info(
                "Skipping AuditService record for UserConsent %s: user %s has no "
                "active organization membership.",
                consent.id,
                consent.user_id,
            )
            return

        actor = self.audit_service.actor_from_membership(membership)
        self.audit_service.record(
            organization_id=membership.organization_id,
            action=AuditAction.CREATE,
            actor=actor,
            subject=self.audit_service.subject_from_instance(consent),
        )
