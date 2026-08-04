"""Shared logic for the branding-logo upload/delivery surface (Phase 2b of the
Organization Auth-Area Branding plan).

Split into its own module so the S3-key normalization, the delivery-route URL
builder, and the signed-upload-payload builder each have exactly one
implementation, reused across the REST serializer (``organizations/serializers.py``),
the GraphQL surface (``public_api/mutations.py``, ``public_api/queries.py``), and
the invitation-email context (``organizations/notification_contexts.py``).
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast
from urllib.parse import unquote, urlparse

from django.conf import settings
from django.urls import reverse

from s3direct.utils import get_aws_credentials, get_key

from organizations.exceptions import BrandingLogoUploadRejectedError


if TYPE_CHECKING:
    from organizations.models import Organization


# Reserved slug (also listed in `organizations.slug_validation._RESERVED_ROUTE_SLUGS`,
# which is what guarantees no real organization can ever claim it) used to key the
# delivery route when there is no real organization -- or the organization has not
# set a slug of its own -- to key it by. Requesting this "slug" hits the delivery
# route's own unknown-slug branch, which serves the bundled default logo -- the same
# path a genuinely unknown slug takes, so this is not a second code path to keep in
# sync.
DEFAULT_LOGO_SLUG_SENTINEL = "default"

# Cache-Control: short max-age. The route's URL is stable across re-uploads (same
# organization slug, same path), so a long max-age would pin a replaced logo in
# caches and in already-delivered emails -- see the plan's "Logo delivery caching"
# guiding decision.
LOGO_CACHE_MAX_AGE_SECONDS = 300

DEFAULT_LOGO_ASSET_PATH = Path(__file__).resolve().parent / "assets" / "default_logo.png"
DEFAULT_LOGO_CONTENT_TYPE = "image/png"
# Fixed identity for the default logo's ETag -- distinct from any real S3 key, and
# stable across releases so a cached default logo doesn't needlessly revalidate.
DEFAULT_LOGO_ETAG_IDENTITY = "vinta-schedule-default-logo"


def normalize_uploaded_logo_key(value: str) -> str:
    """Normalize a client-submitted logo value to a bare S3 key.

    Mirrors ``s3direct_overrides.serializer_fields.S3DirectField.to_internal_value``:
    a caller may submit a full signed/public URL (with a query string, a
    scheme+host, and/or a bucket-name path prefix) or a bare key. Either way,
    only the bare key is ever persisted -- that is what the delivery route
    resolves against S3, never a URL, and never a signed one.
    """
    if not value:
        return ""

    unquoted = unquote(value)
    if "?" in unquoted:
        value = unquoted.split("?")[0]

    if "://" not in value:
        return value

    parsed = urlparse(value)
    path = parsed.path.lstrip("/")
    bucket = getattr(settings, "AWS_MEDIA_BUCKET_NAME", "")
    if bucket and path.startswith(f"{bucket}/"):
        path = path[len(bucket) + 1 :]
    return path


def build_logo_delivery_url(organization: Organization | None, request=None) -> str:
    """Absolute URL to the logo delivery route for ``organization``.

    Always returns a working URL -- never a signed S3 URL, never a bare key,
    never empty -- so every caller of this function can hand the result
    straight to an ``<img>`` tag. When ``organization`` is ``None`` or has not
    set a slug, the URL points at the reserved "default" sentinel slug (see
    ``DEFAULT_LOGO_SLUG_SENTINEL``), which the delivery route resolves through
    its own unknown-slug branch to the bundled default logo.

    ``organization`` must be the **branding root** -- the organization that
    actually owns the ``OrganizationBranding`` row (``branding.organization``
    after ``resolve_branding_for_display``), not necessarily the organization
    whose branding is being presented. A child organization's invitation, for
    instance, presents its reseller ancestor's branding; keying the URL by the
    reseller's slug (not the child's, which is usually unset) is what lets the
    delivery route resolve to the reseller's real logo instead of silently
    falling back to the default.
    """
    org_slug = organization.slug if organization is not None and organization.slug else None
    path = reverse(
        "organization-branding-logo",
        kwargs={"org_slug": org_slug or DEFAULT_LOGO_SLUG_SENTINEL},
    )
    if request is not None:
        return request.build_absolute_uri(path)
    return f"{settings.DEFAULT_PROTOCOL}://{settings.API_DOMAIN}{path}"


def compute_logo_etag(identity: str) -> str:
    """A quoted ETag derived from ``identity`` (the stored S3 key for a real logo,
    or ``DEFAULT_LOGO_ETAG_IDENTITY`` for the bundled default).

    RFC 7232 ¶2.3 permits any opaque quoted string; hashing keeps the header a
    fixed, header-safe length regardless of what the key itself contains.
    """
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f'"{digest}"'


def guess_logo_content_type(key: str) -> str:
    """Best-effort Content-Type for a stored logo key, from its file extension.

    The upload allowlist restricts logos to PNG/JPEG/WebP (see
    ``vinta_schedule_api.settings.base.S3DIRECT_DESTINATIONS["branding_logos"]``),
    and ``generate_s3direct_file_name`` preserves the original extension, so this
    is reliable for every object this route will ever be asked to stream.
    """
    content_type, _ = mimetypes.guess_type(key)
    return content_type or "application/octet-stream"


class SignedBrandingLogoUpload(TypedDict):
    object_key: str
    access_key_id: str | None
    session_token: str | None
    region: str | None
    bucket: str | None
    endpoint: str | None
    acl: str


def sign_branding_logo_upload(
    file_name: str, file_type: str, file_size: int
) -> SignedBrandingLogoUpload:
    """Build the same signed-upload payload the shipped s3direct signing view
    (``POST /s3direct/get_upload_params/``) returns for the ``branding_logos``
    destination, for GraphQL/partner-API callers that cannot reach that
    session-scoped Django endpoint directly.

    Re-checks the destination's own content constraints (content-type allowlist,
    size range) -- these apply no matter who is asking, so they are re-verified
    here rather than trusted to the caller. Authorization (is this caller
    allowed to upload a logo at all) is the GraphQL mutation's job, not this
    function's -- see ``public_api.mutations.Mutation.create_branding_logo_upload``.

    Raises:
        BrandingLogoUploadRejectedError: content type not in the allowlist, or
            size outside the configured range, naming the specific rule broken.
    """
    # `S3DIRECT_DESTINATIONS` mixes value types (callables, strings, lists) across
    # its destinations, which collapses mypy's inferred value type to `object`
    # unless explicitly cast back to the shape this function actually relies on.
    destinations = cast("dict[str, dict[str, Any]]", getattr(settings, "S3DIRECT_DESTINATIONS", {}))
    dest = destinations.get("branding_logos")
    if not dest:
        raise BrandingLogoUploadRejectedError("Branding logo upload destination is not configured.")

    allowed = dest.get("allowed")
    if allowed and allowed != "*" and file_type not in allowed:
        raise BrandingLogoUploadRejectedError(
            f"Invalid file type ({file_type}). Allowed types: {', '.join(allowed)}."
        )

    cl_range = dest.get("content_length_range")
    if cl_range and not (cl_range[0] <= file_size <= cl_range[1]):
        raise BrandingLogoUploadRejectedError(
            f"Invalid file size (must be between {cl_range[0]} and {cl_range[1]} bytes)."
        )

    key = dest.get("key")
    object_key = get_key(key, file_name, dest)
    aws_credentials = get_aws_credentials()

    return {
        "object_key": object_key,
        "access_key_id": aws_credentials.access_key,
        "session_token": aws_credentials.token,
        "region": dest.get("region") or getattr(settings, "AWS_S3_REGION_NAME", None),
        "bucket": dest.get("bucket") or getattr(settings, "AWS_STORAGE_BUCKET_NAME", None),
        "endpoint": dest.get("endpoint") or getattr(settings, "AWS_S3_ENDPOINT_URL", None),
        "acl": dest.get("acl") or "public-read",
    }
