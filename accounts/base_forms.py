from django import forms
from django.utils import timezone

from organizations.models import OrganizationInvitation
from users.models import Profile


class BaseVintaScheduleSignupForm(forms.Form):
    """
    Base form for user signup.

    Captures first_name, last_name, and an optional organization_name. At
    signup time, the intended org name is persisted on Profile.pending_organization_name
    so it can be consumed during email-confirmation provisioning (Phase 3).

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

    def _has_pending_invitation(self, email: str) -> bool:
        """Return True if a non-expired, unaccepted invitation exists for *email*."""
        return OrganizationInvitation.objects.filter(
            email__iexact=email,
            expires_at__gt=timezone.now(),
            accepted_at__isnull=True,
            membership__isnull=True,
        ).exists()

    def signup(self, request, user):
        """
        Persist first_name, last_name, and (conditionally) organization_name on
        the user's Profile.

        organization_name is stored only when no pending invitation matches the
        signup email.  Invited users will auto-join an existing org at
        email-confirmation time; they must not accidentally trigger org creation.
        """
        user.save()

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
