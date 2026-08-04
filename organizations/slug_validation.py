"""Shared validation rules for ``Organization.slug``.

A single module so the REST serializer (this phase) and the public GraphQL
input (a later phase) enforce identical rules — see the plan's **Slug
validation** guiding decision. Exposes one callable,
:func:`validate_organization_slug`, that raises
``django.core.exceptions.ValidationError`` naming the specific rule violated.
Uniqueness is deliberately NOT checked here — it needs a live queryset (and,
on update, the instance being excluded), which differs per caller (REST
serializer vs. admin form vs. GraphQL mutation), so each surface checks it
against its own query.

Three rules. Execution order (confusables, then reserved words, then
format/length) is chosen so each violation surfaces its most specific
message — e.g. ``"ADMIN"`` is rejected as the reserved word ``admin``
(case-insensitive match) rather than as a generic "must be lowercase" format
error:

1. **Confusables** — reject any non-ASCII character outright. This is the
   phishing defense: a self-serve slug lands in a URL path, and a visual
   twin of another organization's slug (a Cyrillic/Greek homoglyph of a
   Latin letter) paired with a copied logo is a workable phishing kit. No
   confusables library (e.g. ``confusable_homoglyphs``) is a project
   dependency today (checked against ``uv.lock``), so this uses stdlib
   ``unicodedata`` rather than adding one: reject anything outside the
   printable ASCII range, with a `unicodedata.name` lookup used only to
   produce a legible error message. Because the format rule below confines
   a *valid* slug to ``[a-z0-9-]``, every legitimate slug is pure ASCII by
   construction — this check exists so a non-ASCII submission gets a
   specific "confusable character" error instead of falling through to the
   generic format-violation message, which is what lets a caller (or a
   test) tell the two rule violations apart.
2. **Reserved words** — our own route names and names implying us. Kept as
   module-level data (frozensets) rather than scattered conditionals so the
   list can grow without a rewrite (see Open Question 3 in the plan: the
   list is a product asset, reviewed as rejection complaints arrive).
   Matched case-insensitively so a differently-cased reserved word is still
   caught as reserved rather than merely as a format violation.
3. **Format and length** — lowercase alphanumeric characters with internal
   hyphens only; bounded length; no leading or trailing hyphen; not purely
   numeric (a numeric slug reads as an organization id, which is exactly
   the enumerable identifier the slug exists to replace — see the plan's
   **Public identifier** guiding decision).
"""

from __future__ import annotations

import re
import unicodedata

from django.core.exceptions import ValidationError


SLUG_MIN_LENGTH = 3
SLUG_MAX_LENGTH = 63

# Our own route names / paths a branded login URL must never shadow.
_RESERVED_ROUTE_SLUGS: frozenset[str] = frozenset(
    {
        "login",
        "logout",
        "signup",
        "admin",
        "api",
        "dashboard",
        "app",
        "auth",
        "static",
        "media",
        "assets",
        "accounts",
        "account",
        "settings",
        "billing",
        "invite",
        "invitations",
        "invitation",
        "organizations",
        "organization",
        "public",
        "graphql",
        "webhooks",
        "webhook",
        "help",
        "support",
        "docs",
        "status",
        "www",
    }
)

# Names implying the vinta / vinta-schedule brand itself, and close variants
# (hyphenated / concatenated) an admin might plausibly try.
_RESERVED_VENDOR_SLUGS: frozenset[str] = frozenset(
    {
        "vinta",
        "vinta-schedule",
        "vintaschedule",
        "vinta-scheduler",
        "vintascheduler",
        "vinta-software",
        "vintasoftware",
        "vinta-api",
        "vintaapi",
        "vinta-schedule-api",
        "vintascheduleapi",
    }
)

RESERVED_ORGANIZATION_SLUGS: frozenset[str] = _RESERVED_ROUTE_SLUGS | _RESERVED_VENDOR_SLUGS

# Lowercase alphanumeric groups joined by single internal hyphens; no leading,
# trailing, or consecutive hyphen.
_SLUG_FORMAT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _validate_confusables(value: str) -> None:
    """Reject any non-ASCII character outright (see module docstring, rule 1)."""
    for char in value:
        if ord(char) > 0x7F:
            try:
                char_name = unicodedata.name(char)
            except ValueError:
                char_name = f"U+{ord(char):04X}"
            raise ValidationError(
                f"Slug contains a non-ASCII character ({char!r}, {char_name}) that could "
                "be a lookalike of an ASCII letter or digit. Use plain lowercase letters, "
                "numbers, and hyphens only.",
                code="organization_slug_confusable",
            )


def _validate_format_and_length(value: str) -> None:
    """Rule 3: lowercase alphanumeric + internal hyphens, bounded length, not numeric."""
    if len(value) < SLUG_MIN_LENGTH or len(value) > SLUG_MAX_LENGTH:
        raise ValidationError(
            f"Slug must be between {SLUG_MIN_LENGTH} and {SLUG_MAX_LENGTH} characters long.",
            code="organization_slug_length",
        )

    if not _SLUG_FORMAT_RE.fullmatch(value):
        raise ValidationError(
            "Slug must contain only lowercase letters, numbers, and internal hyphens "
            "(no leading or trailing hyphen, no consecutive hyphens, no other characters).",
            code="organization_slug_format",
        )

    if value.replace("-", "").isdigit():
        raise ValidationError(
            "Slug must not be purely numeric — a numeric slug reads as an organization id.",
            code="organization_slug_numeric",
        )


def _validate_not_reserved(value: str) -> None:
    """Rule 2: reject our own route names and names implying us (case-insensitive)."""
    if value.lower() in RESERVED_ORGANIZATION_SLUGS:
        raise ValidationError(
            f"'{value}' is a reserved word and cannot be used as an organization slug.",
            code="organization_slug_reserved",
        )


def validate_organization_slug(value: str) -> None:
    """Validate ``value`` as a candidate ``Organization.slug``.

    Raises ``django.core.exceptions.ValidationError`` on the first rule
    violated (confusables, then reserved words, then format/length), with a
    message naming that specific rule. Does not check uniqueness — see
    module docstring. Callers are expected to skip this entirely for an
    empty/``None`` value (slug is optional); an empty string is rejected
    directly if this is called on it.
    """
    if not value:
        raise ValidationError("Slug must not be empty.", code="organization_slug_empty")

    _validate_confusables(value)
    _validate_not_reserved(value)
    _validate_format_and_length(value)
