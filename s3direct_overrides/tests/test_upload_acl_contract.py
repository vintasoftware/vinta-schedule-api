"""Guards the contract between the browser upload path and the S3 bucket it writes to.

django-s3direct always puts an ``x-amz-acl`` header on the browser's PUT — its
``get_upload_params`` view falls back to ``public-read`` when a destination omits
``acl``, so there is no "send no ACL" option. Two things therefore have to line up
with the Terraform in ``infrastructure/modules/s3-cloudfront/``:

1. The media bucket sets ``object_ownership = "BucketOwnerEnforced"``, so S3 rejects
   every canned ACL except ``bucket-owner-full-control``.
2. Any PutObject carrying an ACL header is authorized against ``s3:PutObjectAcl`` on
   top of ``s3:PutObject``. Without it S3 answers 403 AccessDenied before it ever
   gets far enough to complain about the ACL itself.

Floci (the local S3 emulator) enforces neither rule, so only real AWS catches a
mismatch here.
"""

import re
from pathlib import Path

from django.conf import settings


# The only canned ACL a BucketOwnerEnforced bucket accepts on a PUT.
# https://docs.aws.amazon.com/AmazonS3/latest/userguide/about-object-ownership.html
ACLS_ALLOWED_WHEN_OWNERSHIP_IS_ENFORCED = {"bucket-owner-full-control"}

TERRAFORM_MODULE = (
    Path(settings.BASE_DIR) / "infrastructure" / "modules" / "s3-cloudfront" / "main.tf"
)


def _app_user_policy_actions(terraform: str) -> set[str]:
    """Pull the actions out of the ``ObjectAccess`` statement of the app IAM user policy."""
    statement = re.search(
        r'sid\s*=\s*"ObjectAccess".*?actions\s*=\s*\[(.*?)\]',
        terraform,
        re.DOTALL,
    )
    assert statement, "ObjectAccess statement not found in the s3-cloudfront module"
    return set(re.findall(r'"([^"]+)"', statement.group(1)))


def test_s3direct_destinations_use_an_acl_the_media_bucket_accepts():
    for name, destination in settings.S3DIRECT_DESTINATIONS.items():
        # `or "public-read"` mirrors s3direct.views.get_upload_params: an absent or
        # empty `acl` is not "no ACL", it is `public-read`.
        acl = destination.get("acl") or "public-read"
        assert acl in ACLS_ALLOWED_WHEN_OWNERSHIP_IS_ENFORCED, (
            f"S3DIRECT_DESTINATIONS['{name}'] uploads with acl={acl!r}, which the media "
            f"bucket rejects because Object Ownership is BucketOwnerEnforced."
        )


def test_app_iam_user_may_set_the_acl_s3direct_sends():
    actions = _app_user_policy_actions(TERRAFORM_MODULE.read_text())

    assert "s3:PutObject" in actions
    assert "s3:PutObjectAcl" in actions, (
        "django-s3direct always sends an x-amz-acl header, so the app IAM user needs "
        "s3:PutObjectAcl as well as s3:PutObject or every browser upload gets 403."
    )
