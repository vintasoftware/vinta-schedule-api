"""Route/reserved-slug sync guard.

Answers the review question raised on PR #206 (arthurzeras): if a new URL route is
added in the future, how do we *enforce* that it also lands in the reserved-slug list,
rather than relying on a reviewer's memory?

This test derives the list of top-level route segments from the **live** root
URLconf (``vinta_schedule_api.urls``) at test time and asserts every one of them is
present in :data:`tenancy.slug_validation.RESERVED_ORGANIZATION_SLUGS`. A new
top-level mount (``path("something/", ...)``) that isn't added to the reserved list
will fail this test, naming the offending segment.

Scope — "top-level" means the routes mounted directly in the project's root
``urlpatterns`` (``auth/``, ``super/``, ``schema/``, ...), not every nested path inside
an included urlconf or DRF router (e.g. the individual REST resources registered on
the root ``DefaultRouter``, like ``organizations/`` or ``calendar-events/``, which are
mounted under an *empty*-prefix ``include(...)`` and therefore skipped — see the skip
rule below). Those are a separate, much larger surface tracked by the app-level
``routes.py`` modules, not by this guard.
"""

import re

from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

from tenancy.slug_validation import RESERVED_ORGANIZATION_SLUGS


# A slug can only ever be `[a-z0-9-]+` (see slug_validation._SLUG_FORMAT_RE), so only a
# literal, lowercase-alphanumeric-or-hyphen first path segment can ever collide with
# one. Skip:
#   - the empty-root segment ("" — e.g. the DRF router's `include(...)`, mounted at the
#     project root with no literal prefix of its own);
#   - any segment containing a URL parameter / regex capture (e.g. "<slug>", "(?P<..>");
#   - anything that isn't purely lowercase letters, digits, and hyphens once extracted.
_SLUG_SHAPED_SEGMENT_RE = re.compile(r"^[a-z0-9-]+$")


def _top_level_route_segments() -> set[str]:
    """Enumerate the first path segment of every top-level entry in the root URLconf.

    Reads `django.urls.get_resolver().url_patterns` — the literal list of
    `URLPattern`/`URLResolver` objects Django built from
    `vinta_schedule_api.urls.urlpatterns` — without recursing into what each one
    includes. This intentionally stays a level 1 view: a `path("auth/",
    include("accounts.urls"))` contributes only the segment "auth", not whatever
    `accounts.urls` mounts beneath it, because the reserved-slug guard only needs to
    protect the very first path component a branded slug could ever occupy.
    """
    segments: set[str] = set()
    for pattern in get_resolver().url_patterns:
        assert isinstance(pattern, URLPattern | URLResolver)
        route = str(pattern.pattern)  # e.g. "auth/", "schema/swagger-ui/", "", "super/"
        first_segment = route.split("/", 1)[0]
        if not first_segment:
            # Empty-root mount (e.g. the router `include(...)`) — see module docstring.
            continue
        if not _SLUG_SHAPED_SEGMENT_RE.fullmatch(first_segment):
            # Not a plain lowercase-alnum-hyphen literal (e.g. a dynamic segment) — a
            # slug can never take this shape, so it can never collide with one.
            continue
        segments.add(first_segment)
    return segments


class TestReservedSlugRoutesSync:
    """Every live top-level URL route segment must be a reserved organization slug."""

    def test_every_top_level_route_segment_is_reserved(self):
        route_segments = _top_level_route_segments()

        # Sanity check: the enumeration itself must find something real, otherwise a
        # change to how urls.py is structured could silently make this test vacuous.
        assert route_segments, "expected at least one top-level route segment"

        unreserved = sorted(route_segments - RESERVED_ORGANIZATION_SLUGS)
        assert not unreserved, (
            "the following top-level URL route segment(s) are not in "
            "tenancy.slug_validation's reserved-slug set: "
            f"{unreserved}. Add each to _RESERVED_ROUTE_SLUGS in "
            "organizations/slug_validation.py."
        )
