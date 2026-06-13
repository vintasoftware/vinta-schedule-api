from rest_framework.exceptions import ValidationError


class DuplicateInvitationError(ValidationError):
    default_detail = "An active invitation for this email already exists."
    default_code = "duplicate_invitation"


class NoServiceAccountConfiguredError(ValidationError):
    default_detail = (
        "Configure a Google service account for this organization before syncing rooms."
    )
    default_code = "no_service_account_configured"


class InvalidInvitationTokenError(ValidationError):
    default_detail = "Invalid or expired token"
    default_code = "invalid_invitation_token"


class InvitationNotFoundError(ValidationError):
    default_detail = "Invitation does not exist"
    default_code = "invitation_not_found"


class UserAlreadyHasMembershipError(ValidationError):
    default_detail = "User is already a member of this organization."
    default_code = "user_already_has_membership"
