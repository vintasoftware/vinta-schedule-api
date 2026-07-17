from pathlib import Path

from django.conf import settings

import pytest
from rest_framework.exceptions import NotFound

from public_api.docs_content import (
    _ALLOWLIST,
    _extract_title,
    get_concept_doc,
    list_concept_docs,
)


CONCEPTS_DIR = Path(settings.BASE_DIR) / "docs" / "concepts"

EXPECTED_SLUGS = (
    "availability",
    "calendar-bundles",
    "calendar-groups",
    "calendars",
    "events",
    "recurrence",
)


class TestListConceptDocs:
    def test_returns_all_six_real_files_with_real_titles(self):
        summaries = list_concept_docs()

        assert [summary["slug"] for summary in summaries] == sorted(EXPECTED_SLUGS)
        for summary in summaries:
            on_disk_markdown = (CONCEPTS_DIR / f"{summary['slug']}.md").read_text()
            assert summary["title"] == _extract_title(
                on_disk_markdown, fallback_slug=summary["slug"]
            )

    def test_returns_sorted_alphabetically_by_slug(self):
        summaries = list_concept_docs()

        slugs = [summary["slug"] for summary in summaries]
        assert slugs == sorted(slugs)


class TestExtractTitle:
    def test_normal_heading_on_first_line(self):
        markdown = "# My Title\n\nSome body text.\n"

        assert _extract_title(markdown, fallback_slug="fallback") == "My Title"

    def test_heading_not_on_first_line(self):
        markdown = "> Some blockquote intro\n\n# My Title\n\nBody.\n"

        assert _extract_title(markdown, fallback_slug="fallback") == "My Title"

    def test_no_heading_falls_back_to_title_cased_slug(self):
        markdown = "Just some text with no headings at all.\n"

        assert _extract_title(markdown, fallback_slug="my-doc-slug") == "My Doc Slug"


class TestGetConceptDoc:
    def test_returns_markdown_byte_identical_to_file_on_disk(self):
        doc = get_concept_doc("calendar-groups")

        on_disk = (CONCEPTS_DIR / "calendar-groups.md").read_text()
        assert doc["markdown"] == on_disk
        assert doc["slug"] == "calendar-groups"

    def test_unknown_slug_raises_not_found(self):
        with pytest.raises(NotFound):
            get_concept_doc("does-not-exist")

    @pytest.mark.parametrize(
        "slug",
        [
            "../settings",
            "..%2Fsettings",
            "%2Fetc%2Fpasswd",
            "/etc/passwd",
            "../../pyproject",
        ],
    )
    def test_traversal_payloads_raise_not_found(self, slug):
        with pytest.raises(NotFound):
            get_concept_doc(slug)

    def test_allowlist_contains_exactly_the_expected_slugs(self):
        assert set(_ALLOWLIST.keys()) == set(EXPECTED_SLUGS)
