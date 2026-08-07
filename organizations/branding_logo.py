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

import boto3
from botocore.config import Config as BotoConfig
from s3direct.utils import get_aws_credentials, get_key

from organizations.exceptions import BrandingLogoUploadRejectedError
from s3direct_overrides.utils import get_signed_url


if TYPE_CHECKING:
    from django.db.models.fields.files import FieldFile

    from organizations.models import Organization, OrganizationBranding


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

# The single shared media bucket (`common.media_storage_backend.MediaStorage`,
# `location=""`) also holds `profile_pictures`, `providers_documents`, and
# `healthcare_entities_documents` (PHI) at their own top-level prefixes. Every
# legitimate branding-logo upload lands under THIS prefix -- see
# `S3DIRECT_DESTINATIONS["branding_logos"]["key_args"]` in
# `vinta_schedule_api/settings/base.py`, which `s3direct.utils.get_key` hands to
# `generate_s3direct_file_name` as `dest`, producing keys shaped
# `f"{dest}/{unique_file_name}"` -- i.e. `uploads/branding_logos/<file>`.
#
# Both writers of the ``logo`` field (``organizations.serializers.
# BrandingLogoField.to_internal_value`` and
# ``public_api.mutations.Mutation.update_branding``) reject any normalized key
# that does not start with this prefix -- see ``validate_branding_logo_key``.
# The delivery route (``organizations.views.OrganizationLogoDeliveryView.
# _resolve_logo_key``) independently treats any stored key outside this prefix
# as a miss (never streams it) -- defense in depth against a key that
# references another destination's object (e.g. `providers_documents/...`)
# somehow ending up on a branding row, which would otherwise let the
# unauthenticated delivery route disclose a cross-tenant private object.
BRANDING_LOGO_KEY_PREFIX = "uploads/branding_logos/"


def normalize_uploaded_logo_key(value: str) -> str:
    """Normalize a client-submitted logo value to a bare S3 key.

    Mirrors ``s3direct_overrides.serializer_fields.S3DirectField.to_internal_value``:
    a caller may submit a full signed/public URL (with a query string, a
    scheme+host, and/or a bucket-name path prefix) or a bare key. Either way,
    only the bare key is ever persisted -- that is what the delivery route
    resolves against S3, never a URL, and never a signed one.

    Raises:
        BrandingLogoUploadRejectedError: the normalized key is non-empty and
            does not fall under ``BRANDING_LOGO_KEY_PREFIX`` -- i.e. it points
            at another destination's prefix in the shared media bucket (or an
            absolute/bare path outside any destination). Both writers of the
            ``logo`` field call this function, so the rejection is enforced
            uniformly regardless of surface (REST or GraphQL). An empty value
            is always allowed -- that is how a caller clears the logo.
    """
    if not value:
        return ""

    unquoted = unquote(value)
    if "?" in unquoted:
        value = unquoted.split("?")[0]

    if "://" in value:
        parsed = urlparse(value)
        path = parsed.path.lstrip("/")
        bucket = getattr(settings, "AWS_MEDIA_BUCKET_NAME", "")
        if bucket and path.startswith(f"{bucket}/"):
            path = path[len(bucket) + 1 :]
        value = path

    if value and not value.startswith(BRANDING_LOGO_KEY_PREFIX):
        raise BrandingLogoUploadRejectedError(
            f"Invalid logo key: must be an object uploaded via the branding_logos "
            f"upload destination (expected prefix {BRANDING_LOGO_KEY_PREFIX!r})."
        )
    return value


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


def signed_logo_url(logo: FieldFile | str | None) -> str | None:
    """Time-limited signed S3 URL for a branding row's stored logo, or ``None``
    when no logo is set.

    ``logo`` is the model's ``FieldFile`` (or a bare key string). The signature
    is minted by ``s3direct_overrides.utils.get_signed_url``, the same helper
    every other S3-backed field's serializer uses, so a re-uploaded logo is a
    different URL and no cache -- browser, CDN, or SPA -- can pin the replaced
    image. That is why the API surfaces sign the key instead of handing out the
    delivery route's stable per-slug URL.

    Returns ``None`` (never a signed URL) for a key outside
    ``BRANDING_LOGO_KEY_PREFIX``. Both writers of the ``logo`` field already
    reject such a key (``normalize_uploaded_logo_key``), so this only matters
    for a row written around them -- e.g. a direct DB insert. It mirrors the
    delivery route's own prefix guard
    (``organizations.views.OrganizationLogoDeliveryView._resolve_logo_key``):
    a key pointing at another destination's prefix in the shared media bucket
    (``providers_documents/``, ``healthcare_entities_documents/``) must never
    become a readable URL, signed or otherwise.
    """
    key = logo if isinstance(logo, str) else (getattr(logo, "name", "") or "")
    if not key or not key.startswith(BRANDING_LOGO_KEY_PREFIX):
        return None
    return get_signed_url(key)


def build_logo_display_url(branding: OrganizationBranding | None, request=None) -> str:
    """Absolute, always-renderable logo URL for an API read surface.

    A signed S3 URL when ``branding`` has a usable logo (see
    ``signed_logo_url``), otherwise the delivery route's default-logo URL --
    so callers keep the "never empty, always renders" contract they had when
    every read went through the route.

    The fallback is deliberately keyed by the reserved ``default`` sentinel
    slug rather than the organization's own slug: an organization with no logo
    resolves through the route to the same bundled default anyway, and keying
    it by the sentinel keeps a branded-but-logoless organization's response
    byte-for-byte identical to an unknown one -- the no-enumeration-oracle
    contract ``public_api.queries.branding_for_tenant`` is built on.

    Not used by the invitation email (``organizations.notification_contexts``):
    a signed URL expires, and an email opened days later must still render its
    logo, so that caller keeps ``build_logo_delivery_url``.
    """
    if branding is not None:
        signed = signed_logo_url(branding.logo)
        if signed:
            return signed
    return build_logo_delivery_url(None, request=request)


def compute_logo_etag(identity: str) -> str:
    """A quoted ETag derived from ``identity`` (the stored S3 key for a real logo,
    or ``DEFAULT_LOGO_ETAG_IDENTITY`` for the bundled default).

    RFC 7232 ¶2.3 permits any opaque quoted string; hashing keeps the header a
    fixed, header-safe length regardless of what the key itself contains.
    """
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f'"{digest}"'


# The only Content-Types `guess_logo_content_type` will ever return for a real
# object -- everything else (including `image/svg+xml`, `text/html`, or any other
# browser-sniffable/renderable type) falls back to `application/octet-stream`.
# The upload allowlist (`BRANDING_LOGO_CONTENT_TYPES` in
# `vinta_schedule_api.settings.base`) is only advisory at upload time (see
# `sign_branding_logo_upload`'s docstring for why it cannot be enforced by S3
# itself under this project's signing mechanism) -- the stored key's extension is
# attacker-influenced (a caller who bypasses the advisory check, or an object that
# predates this constraint, controls it), so the delivery route must never trust
# it into a browser-renderable Content-Type. This is what keeps a `.svg`/`.html`
# key from becoming stored XSS on `_stream_key`.
_ALLOWED_LOGO_CONTENT_TYPES = frozenset(("image/png", "image/jpeg", "image/webp"))


def guess_logo_content_type(key: str) -> str:
    """Best-effort, allowlisted Content-Type for a stored logo key, from its file
    extension.

    Only ever returns one of ``_ALLOWED_LOGO_CONTENT_TYPES`` -- any other guess
    (notably ``image/svg+xml`` or ``text/html``) falls back to
    ``application/octet-stream``, since the key's extension is not a trustworthy
    signal (see the module-level comment above ``_ALLOWED_LOGO_CONTENT_TYPES``).
    """
    content_type, _ = mimetypes.guess_type(key)
    if content_type in _ALLOWED_LOGO_CONTENT_TYPES:
        return content_type
    return "application/octet-stream"


class SignedBrandingLogoUpload(TypedDict):
    object_key: str
    upload_url: str
    expires_in: int


# Long enough for a slow connection to finish a logo upload, short enough that a
# leaked URL is not a lasting write grant on the bucket. Mirrors
# `users.views.PROFILE_PICTURE_UPLOAD_URL_EXPIRY_SECONDS`.
BRANDING_LOGO_UPLOAD_URL_EXPIRY_SECONDS = 900


def sign_branding_logo_upload(
    file_name: str, file_type: str, file_size: int
) -> SignedBrandingLogoUpload:
    """Mint a presigned S3 PUT URL for the ``branding_logos`` destination, for
    REST/GraphQL/partner-API callers that cannot reach the session-scoped
    ``POST /s3direct/get_upload_params/`` Django endpoint directly.

    Re-checks the destination's own content constraints (content-type allowlist,
    size range) -- these apply no matter who is asking, so they are re-verified
    here rather than trusted to the caller. Authorization (is this caller
    allowed to upload a logo at all) is the REST view's/GraphQL mutation's job,
    not this function's -- see ``organizations.views.
    OrganizationBrandingLogoUploadParamsView`` and ``public_api.mutations.
    Mutation.create_branding_logo_upload``.

    Unlike the shipped ``s3direct`` signing view, no AWS credentials ever reach
    the caller -- the returned ``upload_url`` is a complete SigV4 presigned PUT
    URL; the caller just PUTs the file body to it with a matching Content-Type
    and no other headers. No ACL is signed either: the media bucket sets Object
    Ownership to BucketOwnerEnforced, which rejects any upload carrying an
    ``x-amz-acl`` header -- objects stay private through the bucket's
    public-access block and are only ever served through the delivery route.

    Content-Type is a signed header, so a caller cannot swap it after the URL is
    issued (a mismatched ``Content-Type`` on the PUT invalidates the signature).
    File size is **not** enforced by S3 itself -- a presigned PUT URL (unlike a
    presigned POST policy) carries no ``content-length-range`` condition, so the
    size check below is advisory only. The actual mitigation for an
    oversized/mistyped object that slips past it is downstream, not at upload
    time: ``BRANDING_LOGO_KEY_PREFIX`` constrains every written ``logo`` key to
    this destination's own prefix (see ``normalize_uploaded_logo_key``), and the
    delivery route (``organizations.views.OrganizationLogoDeliveryView``) treats
    every streamed object's bytes and extension as untrusted regardless of what
    was declared at upload time (allowlisted Content-Type via
    ``guess_logo_content_type`` + ``X-Content-Type-Options: nosniff`` on every
    response) -- so it still cannot be used for stored XSS or to serve another
    tenant's object.

    Raises:
        BrandingLogoUploadRejectedError: content type not in the allowlist, size
            outside the configured range, or the destination's S3 configuration
            (bucket/region/endpoint) is incomplete.
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

    bucket = dest.get("bucket") or getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
    region = dest.get("region") or getattr(settings, "AWS_S3_REGION_NAME", None)
    endpoint = dest.get("endpoint") or getattr(settings, "AWS_S3_ENDPOINT_URL", None)
    if not bucket or not region or not endpoint:
        raise BrandingLogoUploadRejectedError("S3 configuration is incomplete.")

    object_key = get_key(dest.get("key"), file_name, dest)
    aws_credentials = get_aws_credentials()

    s3_client = boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id=aws_credentials.access_key,
        aws_secret_access_key=aws_credentials.secret_key,
        aws_session_token=aws_credentials.token,
        config=BotoConfig(
            signature_version="s3v4",
            # Floci (local) only answers path-style; on AWS "auto" picks
            # virtual-hosted. Mirrors what django-storages reads for the same call.
            s3={"addressing_style": getattr(settings, "AWS_S3_ADDRESSING_STYLE", "auto")},
        ),
    )

    upload_url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": object_key, "ContentType": file_type},
        ExpiresIn=BRANDING_LOGO_UPLOAD_URL_EXPIRY_SECONDS,
        HttpMethod="PUT",
    )

    return {
        "object_key": object_key,
        "upload_url": upload_url,
        "expires_in": BRANDING_LOGO_UPLOAD_URL_EXPIRY_SECONDS,
    }


def branding_diff_state(branding: OrganizationBranding | None) -> dict[str, str]:
    """Field-level snapshot of an ``OrganizationBranding`` row for audit diffs
    (Organization Auth-Area Branding plan, Phase 4).

    Shared by both write surfaces -- ``organizations.views.OrganizationBrandingView``
    (REST) and ``public_api.mutations.Mutation.update_branding`` (GraphQL) -- so
    they feed ``audit.diff.compute_diff`` identically-shaped before/after states
    rather than each re-deriving the field list.

    ``logo`` is captured as its stored key (``FieldFile.name``, normalized to
    ``""`` when unset) rather than the ``FieldFile`` object itself, so equality
    comparison in ``compute_diff`` behaves like every other plain string field.

    Returns an all-empty state (never ``None``) when ``branding`` is ``None`` --
    the "before" side of a create, where there is no prior row to diff against.
    Callers creating a fresh row should skip diffing entirely (an audit CREATE
    record carries no diff) rather than feed this into ``compute_diff`` against
    an all-empty "before", which would misrepresent a creation as an update of
    every field from empty.
    """
    if branding is None:
        return {
            "app_name": "",
            "logo": "",
            "primary_color": "",
            "secondary_color": "",
            "support_email": "",
            "redirect_url": "",
        }
    return {
        "app_name": branding.app_name,
        "logo": branding.logo.name or "",
        "primary_color": branding.primary_color,
        "secondary_color": branding.secondary_color,
        "support_email": branding.support_email,
        "redirect_url": branding.redirect_url,
    }
