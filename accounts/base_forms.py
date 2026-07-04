import logging
from typing import Annotated

from django import forms
from django.utils import timezone

from dependency_injector.wiring import Provide, inject

from common.utils.request_utils import client_ip_from_request, user_agent_from_request
from legal.exceptions import NoPolicyDocumentError
from legal.models import ConsentSource, PolicyDocumentType
from legal.services import ConsentService
from organizations.models import OrganizationInvitation
from users.models import Profile


logger = logging.getLogger(__name__)


class BaseVintaScheduleSignupForm(forms.Form):
    """
    Base form for user signup.

    Captures first_name, last_name, an optional organization_name, and a
    required policy-acceptance acknowledgement. At signup time, the intended
    org name is persisted on Profile.pending_organization_name so it can be
    consumed during email-confirmation provisioning (Phase 3).

    When a non-expired, unaccepted OrganizationInvitation exists for the
    signup email, the org name is left blank — the user will auto-join the
    inviting org instead of creating a new one.
    """

    first_name = forms.CharField(max_length=255, required=True, label="First Name")
    last_name = forms.CharField(max_length=255, required=True, label="Last Name")
    organization_name = forms.CharField(
        max_length=255,
        required=False,
        label="Organization Name",
    )
    accepted_policies = forms.BooleanField(
        required=True,
        label="I agree to the Privacy Policy, Terms of Use, and SMS messaging consent.",
        error_messages={
            "required": (
                "You must accept the Privacy Policy, Terms of Use, and SMS messaging "
                "consent to create an account."
            ),
        },
    )

    def _has_pending_invitation(self, email: str) -> bool:
        """Return True if a non-expired, unaccepted invitation exists for *email*."""
        return OrganizationInvitation.objects.filter(
            email__iexact=email,
            expires_at__gt=timezone.now(),
            accepted_at__isnull=True,
            membership__isnull=True,
        ).exists()

    def _record_signup_consents(self, request, user, consent_service: ConsentService) -> None:
        """Record acceptance of every published policy document type.

        Consent is captured for all three ``PolicyDocumentType`` values at
        signup (privacy policy, terms of use, SMS consent) — only
        ``SMS_CONSENT`` gates SMS sending (Phase 5), but recording the other
        two is captured for completeness per the plan's Open Questions.

        A document type with no published version yet raises
        ``NoPolicyDocumentError``; that is logged and swallowed per document
        type so signup still succeeds — the SMS gate independently fails
        closed if no ``SMS_CONSENT`` record exists.
        """
        client_ip = client_ip_from_request(request)
        user_agent = user_agent_from_request(request)

        for document_type in PolicyDocumentType.values:
            try:
                consent_service.record_consent(
                    user,
                    document_type,
                    source=ConsentSource.SIGNUP_FORM,
                    ip=client_ip,
                    user_agent=user_agent,
                )
            except NoPolicyDocumentError:
                logger.warning(
                    "Skipping signup consent capture for document_type=%s, user=%s: "
                    "no published PolicyDocument exists yet.",
                    document_type,
                    user.pk,
                )

    @inject
    def signup(
        self,
        request,
        user,
        consent_service: Annotated[ConsentService, Provide["consent_service"]] = None,  # type: ignore[assignment]
    ):
        """
        Persist first_name, last_name, and (conditionally) organization_name on
        the user's Profile, and record acceptance of the published policy
        documents (privacy policy, terms of use, SMS consent).

        organization_name is stored only when no pending invitation matches the
        signup email.  Invited users will auto-join an existing org at
        email-confirmation time; they must not accidentally trigger org creation.

        ``consent_service`` is DI-injected (mirrors ``AccountAdapter`` /
        ``CalendarViewSet``'s per-method ``@inject``). It carries a
        ``Provide`` default rather than being required so allauth's
        positional call ``self.signup(request, user)`` (see
        ``allauth.account.forms``) keeps working unchanged.
        """
        user.save()

        self._record_signup_consents(request, user, consent_service)

        first_name = self.cleaned_data.get("first_name", "")
        last_name = self.cleaned_data.get("last_name", "")
        organization_name = self.cleaned_data.get("organization_name", "")

        # If a pending invitation exists for this email, leave the org name blank
        # so Phase 3's provisioning hook falls through to the invite-auto-join path.
        if self._has_pending_invitation(user.email):
            organization_name = ""

        try:
            profile = user.profile
            profile.first_name = first_name
            profile.last_name = last_name
            profile.pending_organization_name = organization_name
            profile.save()
        except Profile.DoesNotExist:
            Profile.objects.create(
                user=user,
                first_name=first_name,
                last_name=last_name,
                pending_organization_name=organization_name,
            )
        return user
