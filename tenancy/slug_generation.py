"""Derivation of an ``Organization.slug`` when the caller did not supply one.

``AbstractOrganization.slug`` (vinta-django-orgs) is NOT NULL and unique, so
"this organization has not picked a public identifier yet" stopped being a
storable state in Phase 1c of the vinta-django-orgs migration. Two callers need
to invent one, and they must agree:

* ``tenancy.migrations.0026_backfill_organization_slugs`` fills the rows that
  were ``NULL`` before the column became NOT NULL.
* ``Organization.save()`` fills a row whose caller left the slug blank.

The rules live here, once, rather than in either of them. **The validity rules
themselves are not restated** -- every candidate is checked with
``tenancy.slug_validation.validate_organization_slug``, the same callable the
REST serializer, the admin form and the GraphQL input use, so a slug this module
invents is one a human could have typed.

Algorithm
---------
1. ``slugify(name)``, truncated to ``SLUG_MAX_LENGTH`` and stripped of a
   truncation-induced trailing hyphen.
2. If that base is not a valid slug -- reserved word (``"Admin"`` ->
   ``"admin"``), too short (``"AB"`` -> ``"ab"``), purely numeric (``"2024"``),
   or carrying a character ``slugify`` keeps but our format rule does not
   (``"Foo_Bar"`` -> ``"foo_bar"``) -- skip straight to step 4.
3. Otherwise offer ``base``, then ``base-2``, ``base-3``, ... until one is free,
   re-validating each (the numeric suffix can push the value past the length
   bound) and re-truncating the base to make room for the suffix.
4. Fall back to ``org-<token>``: ``org-<pk>`` for an existing row, and
   ``org-<random hex>`` for a row that has no pk yet. Then ``org-<token>-2``,
   ``org-<token>-3``, ... on the (vanishingly unlikely) chance that is taken too.

Every step consults the caller-supplied ``slug_exists`` predicate rather than
querying itself, so the data migration can ask its historical model and
``Organization.save()`` can ask the live manager.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator

from django.core.exceptions import ValidationError
from django.utils.text import slugify

from tenancy.slug_validation import SLUG_MAX_LENGTH, validate_organization_slug


#: How many numeric disambiguators to try before giving up on a base and
#: falling back to the ``org-<token>`` form. Bounded so a pathological data set
#: cannot turn one save into an unbounded number of queries; the fallback is
#: always available and always unique.
MAX_DISAMBIGUATION_ATTEMPTS = 100

#: Bytes of randomness in the ``org-<token>`` fallback used when there is no pk
#: to name the row by. Eight hex characters; collisions are handled by the same
#: numeric disambiguation as any other candidate.
_FALLBACK_TOKEN_BYTES = 4


def _is_valid(candidate: str) -> bool:
    try:
        validate_organization_slug(candidate)
    except ValidationError:
        return False
    return True


def _truncate(base: str, reserve: int = 0) -> str:
    """``base`` shortened to fit ``SLUG_MAX_LENGTH`` minus ``reserve``, hyphen-safe."""
    return base[: SLUG_MAX_LENGTH - reserve].rstrip("-")


def _candidates(base: str) -> Iterator[str]:
    """``base`` and its numeric disambiguations, dropping the ones that are invalid.

    A generator so the common case -- the first candidate is free -- costs one
    validation and one ``slug_exists`` call rather than
    ``MAX_DISAMBIGUATION_ATTEMPTS`` of each.

    Yields **nothing at all** when the base itself is not a valid slug. That is
    the difference between "this name collides" and "this name cannot be a
    slug", and conflating them is a real bug rather than a refinement: an
    organization called ``Admin`` slugifies to the reserved route word ``admin``,
    and ``admin-2`` passes every rule -- so offering the disambiguations of an
    invalid base would quietly hand the reserved-word case a derived slug and
    the ``org-<token>`` fallback would never be reached. The same goes for a
    name that is too short (``"AB"`` -> ``"ab"``, but ``"ab-2"`` is long enough).
    """
    first = _truncate(base)
    if not _is_valid(first):
        return

    yield first

    for suffix in range(2, MAX_DISAMBIGUATION_ATTEMPTS + 1):
        marker = f"-{suffix}"
        candidate = f"{_truncate(base, reserve=len(marker))}{marker}"
        if _is_valid(candidate):
            yield candidate


def derive_organization_slug(
    name: str,
    *,
    slug_exists: Callable[[str], bool],
    fallback_token: str | None = None,
    disclose_name: bool = True,
) -> str:
    """Return a valid, unused slug for an organization called ``name``.

    Args:
        name: The organization's name. May be empty, non-ASCII, reserved, or
            anything else a human typed -- every failure mode lands on the
            ``org-<token>`` fallback rather than raising.
        slug_exists: ``(candidate) -> bool``, answering whether some *other*
            organization already holds ``candidate``. The caller owns the
            query, and owns excluding the row being saved from it.
        fallback_token: What to name the row by when nothing can be derived from
            ``name``. Pass the primary key (as a string) for an existing row;
            leave it out for a row that does not have one yet and a random token
            is used instead.
        disclose_name: Whether ``name``-derived candidates are offered at all.
            ``slug`` is public -- it appears in branded login URLs,
            ``brandingForTenant``, and the logo delivery route -- so deriving
            it from ``name`` discloses the organization's name to anyone who
            can guess or enumerate slugs. ``True`` (the default) is for
            callers that have a specific, sanctioned reason to accept that
            disclosure: the Phase 1c slug backfill (pre-launch, no production
            data to disclose) and the deliberate self-serve organization-create
            write, where a human explicitly chose the name for their own,
            about-to-be-public organization (``OrganizationService
            .create_organization``). Every other caller -- notably
            ``Organization.save()``'s own fallback for a row left with no
            explicit slug -- passes ``False``, going straight to the opaque
            ``org-<token>`` form instead. See the plan's Guiding Decisions for
            why the model-level default changed from name-derived to opaque.

    Returns:
        A slug that passes ``validate_organization_slug`` and that
        ``slug_exists`` reports as free.

    Raises:
        RuntimeError: If even the fallback form collides
            ``MAX_DISAMBIGUATION_ATTEMPTS`` times, which cannot happen for a
            distinct ``fallback_token`` and means the predicate is lying.
    """
    if disclose_name:
        for candidate in _candidates(slugify(name)):
            if not slug_exists(candidate):
                return candidate

    token = fallback_token or secrets.token_hex(_FALLBACK_TOKEN_BYTES)
    for candidate in _candidates(f"org-{token}"):
        if not slug_exists(candidate):
            return candidate

    raise RuntimeError(
        f"Could not derive a free organization slug for {name!r} "
        f"(fallback token {token!r}); every candidate reported as taken."
    )
