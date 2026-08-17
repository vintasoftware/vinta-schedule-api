"""Coverage for the slug half of ``OrganizationService.create_organization``:

``_create_organization_row``'s name-derivation, its collision-disambiguation
(via ``derive_organization_slug``), and its bounded ``IntegrityError`` retry
(``_SLUG_CREATE_ATTEMPTS`` attempts, discriminating a lost slug race -- which
is retried -- from any other integrity failure -- which is not, and must
propagate as-is rather than being reported as a slug collision).

Before this file, none of that had a test: ``grep OrganizationSlugCollisionError``
matched only ``services.py`` and ``exceptions.py``, and nothing pinned the
sanctioned name-derivation (``disclose_name=True``) either.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError

import pytest
from model_bakery import baker

from organizations.exceptions import OrganizationSlugCollisionError
from organizations.models import Organization
from organizations.services import OrganizationService


@pytest.fixture
def user():
    return baker.make(get_user_model(), email="creator@example.com")


class _FakeDiag:
    """Stands in for ``psycopg.errors.*.diag`` -- the attribute
    ``OrganizationService._create_organization_row`` reads to discriminate a
    retryable slug race from any other integrity failure."""

    def __init__(self, constraint_name: str):
        self.constraint_name = constraint_name


class _FakeDriverError(Exception):
    """Stands in for the underlying ``psycopg`` exception Django's
    ``IntegrityError`` wraps as ``__cause__``."""

    def __init__(self, constraint_name: str):
        super().__init__("simulated driver error")
        self.diag = _FakeDiag(constraint_name)


def _integrity_error(constraint_name: str) -> IntegrityError:
    """A ``django.db.IntegrityError`` whose ``__cause__`` carries ``diag.
    constraint_name``, exactly as a real one raised by a failed INSERT would."""
    driver_error = _FakeDriverError(constraint_name)
    error = IntegrityError("simulated")
    error.__cause__ = driver_error
    return error


@pytest.mark.django_db
class TestCreateOrganizationDerivesANameBasedSlug:
    def test_service_yields_a_name_derived_slug(self, user):
        """``create_organization`` is the one sanctioned name-derived runtime
        path (``disclose_name=True``) -- a plain slugification of the name."""
        service = OrganizationService()

        organization = service.create_organization(creator=user, name="Acme Inc")

        assert organization.slug == "acme-inc"

    def test_collision_disambiguates_with_a_numeric_suffix(self, user):
        """A prior organization already holding the derived slug pushes the
        new one to ``<base>-2`` rather than colliding."""
        baker.make(Organization, name="Existing Acme", parent=None, slug="acme-inc")
        service = OrganizationService()

        organization = service.create_organization(creator=user, name="Acme Inc")

        assert organization.slug == "acme-inc-2"


@pytest.mark.django_db
class TestCreateOrganizationRowSlugRaceRetry:
    def test_a_lost_slug_race_is_retried_and_the_third_attempt_succeeds(self, user):
        """Two ``IntegrityError``s naming the slug's unique constraint are
        retried on a fresh savepoint each time; the third attempt (the
        service's ``_SLUG_CREATE_ATTEMPTS`` budget) succeeds."""
        service = OrganizationService()
        real_create = Organization.objects.create
        calls = {"count": 0}

        def flaky_create(**kwargs):
            calls["count"] += 1
            if calls["count"] < 3:
                raise _integrity_error(service._SLUG_UNIQUE_CONSTRAINT_NAME)
            return real_create(**kwargs)

        with patch("organizations.services.Organization.objects.create", side_effect=flaky_create):
            organization = service.create_organization(creator=user, name="Acme Inc")

        assert calls["count"] == 3
        assert organization.slug == "acme-inc"

    def test_exhausting_the_retry_budget_raises_the_collision_error(self, user):
        """Three consecutive lost slug races (the whole ``_SLUG_CREATE_ATTEMPTS``
        budget) surface as ``OrganizationSlugCollisionError``, not a raw
        ``IntegrityError`` escaping to abort the enclosing transaction."""
        service = OrganizationService()
        calls = {"count": 0}

        def always_flaky_create(**kwargs):
            calls["count"] += 1
            raise _integrity_error(service._SLUG_UNIQUE_CONSTRAINT_NAME)

        with patch(
            "organizations.services.Organization.objects.create",
            side_effect=always_flaky_create,
        ):
            with pytest.raises(OrganizationSlugCollisionError):
                service.create_organization(creator=user, name="Acme Inc")

        assert calls["count"] == service._SLUG_CREATE_ATTEMPTS
        assert not Organization.objects.filter(name="Acme Inc").exists()

    def test_an_unrelated_integrity_error_is_reraised_rather_than_retried(self, user):
        """A ``uniq_org_name_per_parent`` violation is a different failure --
        every attempt would fail identically -- so it propagates immediately
        as ``IntegrityError`` instead of being retried into a misleading
        "could not allocate a unique slug" report."""
        service = OrganizationService()
        calls = {"count": 0}

        def duplicate_name_create(**kwargs):
            calls["count"] += 1
            raise _integrity_error("uniq_org_name_per_parent")

        with patch(
            "organizations.services.Organization.objects.create",
            side_effect=duplicate_name_create,
        ):
            with pytest.raises(IntegrityError):
                service.create_organization(creator=user, name="Acme Inc")

        assert calls["count"] == 1
