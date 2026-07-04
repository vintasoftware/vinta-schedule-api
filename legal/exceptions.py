from rest_framework.exceptions import ValidationError


class NoPolicyDocumentError(ValidationError):
    default_detail = "No published policy document exists for this document type."
    default_code = "no_policy_document"
