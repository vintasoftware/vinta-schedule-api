import logging
from typing import Annotated, ClassVar

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

    Captures first_name, last_name, an optional organization_name, and two
    required policy-acceptance acknowledgements. At signup time, the intended
    org name is persisted on Profile.pending_organization_name so it can be
    consumed during email-confirmation provisioning (Phase 3).

    When a non-expired, unaccepted OrganizationInvitation exists for the
    signup email, the org name is left blank — the user will auto-join the
    inviting org instead of creating a new one.

    Consent acceptance is split into two separate fields (Phase 9) rather
    than one combined checkbox: Twilio / TCPA require SMS consent to be its
    own explicit, distinct opt-in, not bundled with Terms/Privacy acceptance.
    """

    first_name = forms.CharField(max_length=255, required=True, label="First Name")
    last_name = forms.CharField(max_length=255, required=True, label="Last Name")
    organization_name = forms.CharField(
        max_length=255,
        required=False,
        label="Organization Name",
    )
    accepted_terms = forms.BooleanField(
        required=True,
        label="I agree to the Privacy Policy and Terms of Use.",
        error_messages={
            "required": "You must accept the Privacy Policy and Terms of Use to create an account.",
        },
    )
    accepted_sms_consent = forms.BooleanField(
        required=True,
        label=(
            "I agree to receive SMS text messages (e.g. verification codes) at the "
            "phone number I provide. Msg & data rates may apply."
        ),
        error_messages={
            "required": (
                "You must agree to receive SMS text messages at the phone number you "
                "provide to create an account."
            ),
        },
    )

    # Maps each consent checkbox field to the PolicyDocumentType values it
    # covers. Keeping this as an explicit mapping (rather than iterating
    # PolicyDocumentType.values directly) means SMS consent could later
    # become optional without touching how terms/privacy are recorded.
    _CONSENT_FIELD_DOCUMENT_TYPES: ClassVar[dict[str, list[str]]] = {
        "accepted_terms": [
            PolicyDocumentType.PRIVACY_POLICY,
            PolicyDocumentType.TERMS_OF_USE,
        ],
        "accepted_sms_consent": [PolicyDocumentType.SMS_CONSENT],
    }

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

        Each document type is driven by the checkbox that covers it (Phase 9
        — separate SMS consent checkbox): ``PRIVACY_POLICY`` /
        ``TERMS_OF_USE`` are recorded because ``accepted_terms`` is required
        and checked, and ``SMS_CONSENT`` is recorded because
        ``accepted_sms_consent`` is required and checked, independently. Both
        fields are required today so all three document types are always
        recorded on a successful signup, but the SMS row is driven
        specifically by ``accepted_sms_consent`` — if that field ever became
        optional, only the SMS_CONSENT recording would stop, leaving terms
        recording untouched.

        Each row is recorded with ``phone_number=user.phone_number`` (Phase 8
        — phone-keyed consent). The email/password signup form collects no
        phone number, so this is ``""`` at this point in that path; the
        phone-keyed SMS gate never matches a blank value, and a later phone
        submission (e.g. login-by-phone / change-phone) records its own
        consent row for that specific number.

        A document type with no published version yet raises
        ``NoPolicyDocumentError``; that is logged and swallowed per document
        type so signup still succeeds — the SMS gate independently fails
        closed if no ``SMS_CONSENT`` record exists.
        """
        client_ip = client_ip_from_request(request)
        user_agent = user_agent_from_request(request)

        for field_name, document_types in self._CONSENT_FIELD_DOCUMENT_TYPES.items():
            if not self.cleaned_data.get(field_name):
                continue
            for document_type in document_types:
                try:
                    consent_service.record_consent(
                        user,
                        document_type,
                        source=ConsentSource.SIGNUP_FORM,
                        ip=client_ip,
                        user_agent=user_agent,
                        phone_number=user.phone_number,
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
