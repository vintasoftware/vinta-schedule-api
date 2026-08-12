"""Derive an ``Organization.slug`` when the caller did not supply one.

``slug`` is NOT NULL, unique, and rejected when blank by the
``organization_slug_not_blank`` check constraint, so *something* has to produce
a value for every write path that does not ask the human for one. Two shapes
exist, and which one a caller gets is a disclosure decision, not a formatting
one:

``org-<token>`` -- **opaque**, and the default (``disclose_name=False``).
    The slug is public: it appears in branded login URLs. Deriving it from the
    organization's name therefore publishes that name. ``Organization.save()``
    uses this form, so an organization saved without an explicit slug from any
    code path never leaks its name by accident.

``<slugified name>`` -- **name-derived**, opt-in (``disclose_name=True``).
    Sanctioned for exactly two callers, both of which are places a human chose
    the name for their own, about-to-be-public organization:
    ``OrganizationService.create_organization`` (the self-serve "create my own
    organization" flow) and the Phase 1 backfill of pre-existing rows. See the
    plan's two Guiding Decisions rows on slugs.

Every candidate is checked against :func:`organizations.slug_validation.
validate_organization_slug` before it is offered, so a derived slug is subject
to the same reserved-word, confusable and format rules a hand-picked one is --
``slugify("Admin")`` is ``"admin"``, which is reserved, and a name written
entirely in a non-Latin script slugifies to the empty string.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from django.core.exceptions import ValidationError
from django.utils.text import slugify

from organizations.slug_validation import SLUG_MAX_LENGTH, validate_organization_slug


#: Prefix of the opaque form. Not itself a reserved word (``org`` alone is too
#: short to be a valid slug anyway); the token is what makes it unique.
OPAQUE_SLUG_PREFIX = "org"

#: Bytes of randomness behind ``org-<token>``. Twelve lowercase hex characters
#: -- collision-free in practice, and short enough to stay well inside
#: ``SLUG_MAX_LENGTH`` so the disambiguation loop below is never reached.
_OPAQUE_SLUG_TOKEN_BYTES = 6

#: How many candidates each loop tries before giving up. Bounded so a bug in
#: ``slug_exists`` cannot spin forever; large enough that reaching the limit
#: means something is genuinely wrong rather than merely crowded.
_MAX_ATTEMPTS = 1000


class SlugDerivationError(RuntimeError):
    """Raised when no free, valid slug could be derived within the attempt budget."""


def _is_valid(candidate: str) -> bool:
    try:
        validate_organization_slug(candidate)
    except ValidationError:
        return False
    return True


def name_derived_slug_base(name: str) -> str | None:
    """The slugified form of ``name``, or ``None`` when it is not usable.

    ``None`` means the name produced nothing a slug may be -- empty (a name in
    a script ``slugify`` strips entirely), reserved (``"Admin"``), or otherwise
    rejected by the shared rules. The caller is expected to fall back rather
    than to mangle the value into compliance: a mangled slug no longer resembles
    the name it was supposed to disclose, so it buys nothing over the opaque
    form.
    """
    base = slugify(name)[:SLUG_MAX_LENGTH].strip("-")
    if not base or not _is_valid(base):
        return None
    return base


def disambiguate_slug(base: str, *, slug_exists: Callable[[str], bool]) -> str | None:
    """``base``, or ``base-2`` / ``base-3`` / ... -- the first one free.

    Returns ``None`` when no free candidate is valid within the attempt budget,
    which the caller is expected to treat as "fall back to the opaque form"
    rather than as an error. The numeric suffix is applied to a *truncated*
    base so the result never exceeds ``SLUG_MAX_LENGTH``.
    """
    if _is_valid(base) and not slug_exists(base):
        return base

    for counter in range(2, _MAX_ATTEMPTS + 2):
        suffix = f"-{counter}"
        candidate = f"{base[: SLUG_MAX_LENGTH - len(suffix)].strip('-')}{suffix}"
        if _is_valid(candidate) and not slug_exists(candidate):
            return candidate

    return None


def opaque_organization_slug(*, slug_exists: Callable[[str], bool]) -> str:
    """A fresh, free ``org-<token>`` slug that discloses nothing about the row."""
    for _attempt in range(_MAX_ATTEMPTS):
        candidate = f"{OPAQUE_SLUG_PREFIX}-{secrets.token_hex(_OPAQUE_SLUG_TOKEN_BYTES)}"
        if _is_valid(candidate) and not slug_exists(candidate):
            return candidate

    raise SlugDerivationError(
        f"Could not derive a free opaque organization slug in {_MAX_ATTEMPTS} attempts."
    )


def derive_organization_slug(
    name: str,
    *,
    slug_exists: Callable[[str], bool],
    disclose_name: bool = False,
) -> str:
    """Return a free, valid slug for an organization called ``name``.

    ``disclose_name=False`` (the default, and what ``Organization.save()``
    passes) never looks at ``name`` at all and returns the opaque form. Pass
    ``True`` only from a call site where publishing the organization's name is
    the intended outcome; even then the opaque form is used as the fallback
    when the name yields nothing valid.
    """
    if disclose_name:
        base = name_derived_slug_base(name)
        if base is not None:
            derived = disambiguate_slug(base, slug_exists=slug_exists)
            if derived is not None:
                return derived

    return opaque_organization_slug(slug_exists=slug_exists)
