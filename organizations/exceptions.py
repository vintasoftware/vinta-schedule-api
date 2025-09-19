from rest_framework.exceptions import ValidationError


class DuplicateInvitationError(ValidationError):
    default_detail = "An active invitation for this email already exists."
    default_code = "duplicate_invitation"


class InvalidInvitationTokenError(ValidationError):
    default_detail = "Invalid or expired token"
    default_code = "invalid_invitation_token"


class InvitationNotFoundError(ValidationError):
    default_detail = "Invitation does not exist"
    default_code = "invitation_not_found"
