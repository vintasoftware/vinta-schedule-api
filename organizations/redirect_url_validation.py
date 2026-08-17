"""Shared validation rules for ``OrganizationBranding.redirect_url``.

One module so the REST serializer and the public GraphQL input enforce identical
rules — the same "same validation" contract ``organizations/slug_validation.py``
established for ``Organization.slug``. Exposes one callable,
:func:`validate_redirect_url`, that raises ``django.core.exceptions.ValidationError``
naming the specific rule violated.

``redirect_url`` stores a single, concrete post-authentication destination — never a
caller-supplied value, never a pattern. That is what makes the rules below exhaustive
rather than a partial allowlist:

1. **No control characters** — a literal CR, LF, or tab anywhere in the value is
   rejected (e.g. header/response-splitting payloads such as
   ``https://example.com\r\nSet-Cookie: ...``).
2. **HTTPS only** — no ``http://`` and no other scheme. The destination is handed back
   to a browser right after authentication; anything but ``https`` is refused outright.
3. **No wildcard character** — a literal ``*`` anywhere in the value is rejected. The
   old ``return_url_allowlist`` this field replaces stored entries that were sometimes
   used as glob-like patterns (e.g. ``https://*.example.com``); a single stored
   destination must never regain that shape.
4. **No path-prefix pattern** — a non-root path ending in ``/`` is rejected. A trailing
   slash after a path segment is the other half of allowlist-prefix semantics (e.g.
   ``https://example.com/callback/`` historically meant "anything under here"); a
   concrete destination has no reason to end that way. The bare root (``https://
   example.com`` or ``https://example.com/``) is unaffected — there is no path segment
   to be a prefix of.
5. **Well-formed, with a host** — the value must otherwise pass Django's
   ``URLValidator`` (restricted to the ``https`` scheme), which rejects hostless
   values (``https://``), scheme-confusion (``https:evil.com``, no ``//host``), and
   other malformed URLs that the checks above don't cover.

An empty value is valid: ``redirect_url`` is optional, and ``""`` means "no configured
destination" — the caller falls back to the dashboard.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator


_REDIRECT_URL_SCHEME = "https"
_WILDCARD_CHARACTER = "*"
_CONTROL_CHARACTERS = ("\r", "\n", "\t")
_well_formed_https_url = URLValidator(schemes=[_REDIRECT_URL_SCHEME])


def validate_redirect_url(value: str) -> None:
    """Validate ``value`` as a candidate ``OrganizationBranding.redirect_url``.

    Raises ``django.core.exceptions.ValidationError`` on the first rule violated
    (control characters, then scheme, then wildcard, then path-prefix, then general
    well-formedness/host), with a message naming that specific rule. A falsy value
    (``""``/``None``) is valid and returns without raising — callers that require a
    non-empty value must check that separately.
    """
    if not value:
        return

    if any(character in value for character in _CONTROL_CHARACTERS):
        raise ValidationError(
            "redirect_url must not contain control characters (CR, LF, or tab).",
            code="redirect_url_control_characters",
        )

    parsed = urlsplit(value)
    if parsed.scheme != _REDIRECT_URL_SCHEME:
        raise ValidationError(
            f"redirect_url must use the {_REDIRECT_URL_SCHEME} scheme.",
            code="redirect_url_scheme",
        )

    if _WILDCARD_CHARACTER in value:
        raise ValidationError(
            "redirect_url must not contain a wildcard character ('*').",
            code="redirect_url_wildcard",
        )

    if parsed.path.endswith("/") and parsed.path != "/":
        raise ValidationError(
            "redirect_url must not use a path-prefix pattern "
            "(no trailing slash after a path segment).",
            code="redirect_url_path_prefix",
        )

    try:
        _well_formed_https_url(value)
    except ValidationError as exc:
        raise ValidationError(
            "redirect_url must be a well-formed https URL with a host.",
            code="redirect_url_malformed",
        ) from exc
