import re
from pathlib import Path
from typing import TypedDict

from django.conf import settings

from rest_framework.exceptions import NotFound


class ConceptDocSummary(TypedDict):
    """Manifest entry for a concept doc: enough to render a listing/nav."""

    slug: str
    title: str


class ConceptDoc(ConceptDocSummary):
    """A single concept doc's full content, mirroring the frontend's ``ConceptDoc`` type."""

    markdown: str


_CONCEPTS_DIR = Path(settings.BASE_DIR) / "docs" / "concepts"

# Allow-list of slug -> resolved absolute path, built once at import time by globbing
# `docs/concepts/*.md`. This is the whole security property of this module: a request's
# slug is only ever looked up as a *key* in this dict, never joined into a filesystem
# path, so path traversal is structurally impossible rather than filtered.
_ALLOWLIST: dict[str, Path] = {
    path.stem: path.resolve() for path in sorted(_CONCEPTS_DIR.glob("*.md"))
}

_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_FENCED_CODE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def _extract_title(markdown: str, *, fallback_slug: str) -> str:
    """Return the text of the first ``# `` heading in ``markdown``.

    Falls back to a title-cased version of ``fallback_slug`` when the file has no
    heading at all — a doc without a heading should still be listable, not raise.

    Strips fenced code blocks (``` ... ```) before searching to avoid matching
    headings inside code examples (e.g. shell comments).
    """
    # Remove fenced code blocks to avoid matching headings inside them
    markdown_without_fences = _FENCED_CODE_RE.sub("", markdown)
    match = _HEADING_RE.search(markdown_without_fences)
    if match is None:
        return fallback_slug.replace("-", " ").title()
    return match.group(1)


def list_concept_docs() -> list[ConceptDocSummary]:
    """Return one summary per allow-listed concept doc, sorted alphabetically by slug."""
    summaries: list[ConceptDocSummary] = []
    for slug in sorted(_ALLOWLIST):
        markdown = _ALLOWLIST[slug].read_text()
        summaries.append({"slug": slug, "title": _extract_title(markdown, fallback_slug=slug)})
    return summaries


def get_concept_doc(slug: str) -> ConceptDoc:
    """Return the full content of the concept doc identified by ``slug``.

    ``slug`` is looked up as a dict key against the allow-list built at import time —
    it is never joined into a filesystem path. Raises :class:`NotFound` for any slug
    that is not a key of the allow-list (unknown slug, path-traversal payload, etc).
    """
    path = _ALLOWLIST.get(slug)
    if path is None:
        raise NotFound(detail=f"Unknown concept doc slug '{slug}'.")

    markdown = path.read_text()
    return {
        "slug": slug,
        "title": _extract_title(markdown, fallback_slug=slug),
        "markdown": markdown,
    }
