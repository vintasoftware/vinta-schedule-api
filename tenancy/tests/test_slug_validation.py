"""Table-driven unit tests for tenancy.slug_validation.

Covers all three rules — reserved words, format/length, and confusables —
plus a plain valid slug accepted.
"""

from django.core.exceptions import ValidationError

import pytest

from tenancy.slug_validation import (
    RESERVED_ORGANIZATION_SLUGS,
    SLUG_MAX_LENGTH,
    SLUG_MIN_LENGTH,
    validate_organization_slug,
)


class TestValidOrganizationSlugs:
    """A plain valid slug is accepted (raises nothing)."""

    @pytest.mark.parametrize(
        "value",
        [
            "acme",
            "acme-inc",
            "my-org-2",
            "a1b2c3",
            "abc",  # exactly SLUG_MIN_LENGTH
            "x" * SLUG_MAX_LENGTH,  # exactly SLUG_MAX_LENGTH
        ],
    )
    def test_accepts_valid_slug(self, value: str):
        validate_organization_slug(value)  # must not raise


class TestReservedWordSlugs:
    """Each reserved word is rejected, one case per entry in the reserved list."""

    @pytest.mark.parametrize("value", sorted(RESERVED_ORGANIZATION_SLUGS))
    def test_rejects_reserved_word(self, value: str):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug(value)
        assert "reserved" in str(excinfo.value).lower()

    def test_rejects_reserved_word_case_insensitively(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug("ADMIN")
        assert "reserved" in str(excinfo.value).lower()


class TestFormatAndLengthViolations:
    """One case per format/length sub-rule, each rejected with a rule-specific message."""

    def test_rejects_uppercase_characters(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug("MyOrg")
        assert "lowercase" in str(excinfo.value).lower()

    def test_rejects_underscore(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug("my_org")
        assert "lowercase" in str(excinfo.value).lower()

    def test_rejects_leading_hyphen(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug("-myorg")
        assert "hyphen" in str(excinfo.value).lower()

    def test_rejects_trailing_hyphen(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug("myorg-")
        assert "hyphen" in str(excinfo.value).lower()

    def test_rejects_consecutive_hyphens(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug("my--org")
        assert "hyphen" in str(excinfo.value).lower()

    def test_rejects_space(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug("my org")
        assert "lowercase" in str(excinfo.value).lower()

    def test_rejects_too_short(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug("a" * (SLUG_MIN_LENGTH - 1))
        assert "between" in str(excinfo.value).lower()

    def test_rejects_too_long(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug("a" * (SLUG_MAX_LENGTH + 1))
        assert "between" in str(excinfo.value).lower()

    def test_rejects_purely_numeric(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug("12345")
        assert "numeric" in str(excinfo.value).lower()

    def test_rejects_purely_numeric_with_hyphens(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug("123-456")
        assert "numeric" in str(excinfo.value).lower()

    def test_rejects_empty_string(self):
        with pytest.raises(ValidationError):
            validate_organization_slug("")


class TestConfusableSlugs:
    """Mixed-script / confusable-character slugs are rejected as a distinct rule.

    Lookalike strings are built from ``chr(codepoint)`` rather than typed as
    literal characters, so the ambiguous characters under test don't trip
    ruff's own homoglyph lint (RUF001) on this file.
    """

    CYRILLIC_A = chr(0x0430)  # CYRILLIC SMALL LETTER A — visually "a"
    CYRILLIC_ER = chr(0x0440)  # CYRILLIC SMALL LETTER ER — visually "r"
    FULLWIDTH_ONE = chr(0xFF11)  # FULLWIDTH DIGIT ONE — visually "1"

    def test_rejects_cyrillic_lookalike_of_a_plausible_slug(self):
        """Cyrillic 'a' substituted for the first Latin "a" in "acme"."""
        lookalike = self.CYRILLIC_A + "cme"
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug(lookalike)
        assert "confusable" in str(excinfo.value).lower() or "ascii" in str(excinfo.value).lower()

    def test_rejects_cyrillic_lookalike_mid_word(self):
        """Cyrillic 'er' substituted for the "r" in "myorg"."""
        lookalike = "myo" + self.CYRILLIC_ER + "g"
        with pytest.raises(ValidationError):
            validate_organization_slug(lookalike)

    def test_rejects_fullwidth_digits(self):
        """Fullwidth digit one looks like ASCII "1" but is a distinct codepoint."""
        lookalike = "org" + self.FULLWIDTH_ONE
        with pytest.raises(ValidationError):
            validate_organization_slug(lookalike)

    def test_confusable_message_names_the_offending_character(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_organization_slug(self.CYRILLIC_A + "cme")
        message = str(excinfo.value)
        assert self.CYRILLIC_A in message or "U+0430" in message
