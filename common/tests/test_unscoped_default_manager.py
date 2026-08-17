"""``unscoped_default_manager``: what it turns off, and that it turns it back on.

This is the only construct in the project that unscopes ``objects`` *globally* --
every scoped model, for the duration of a block -- rather than at one call site.
It exists for the two places where Django itself reaches for
``Model._default_manager`` and no argument can redirect it (``ForeignKey.formfield``
in the admin, ``Model.validate_unique``), and both of its callers are one line
long for that reason.

Its two halves are tested separately because they fail differently. Failing to
unscope is a 500 in the admin; failing to *re-scope* is a cross-tenant read
anywhere later in the same worker, which is much quieter -- so the restore is
pinned on the exception path as well as the normal one.

``Calendar`` stands in for "any scoped model": the flag is read in
``OrganizationScopedManager.get_queryset``, which every one of the 34 shares.
"""

from typing import Any

import pytest
from vinta_orgs.exceptions import OrganizationNotFoundError

from calendar_integration.models import Calendar
from common.managers import unscoped_default_manager
from common.organization_context import get_current_organization, organization_context
from organizations.models import Organization


@pytest.fixture
def organization_a(db: Any) -> Organization:
    return Organization.objects.create(name="Unscoped Org A")


@pytest.fixture
def organization_b(db: Any) -> Organization:
    return Organization.objects.create(name="Unscoped Org B")


@pytest.fixture
def calendar_a(organization_a: Organization) -> Calendar:
    return Calendar.objects.create(name="A's calendar", organization=organization_a)


@pytest.fixture
def calendar_b(organization_b: Organization) -> Calendar:
    return Calendar.objects.create(name="B's calendar", organization=organization_b)


@pytest.mark.django_db
class TestInsideTheBlockObjectsIsUnscoped:
    def test_a_bound_read_sees_the_other_organizations_rows(
        self,
        organization_a: Organization,
        calendar_a: Calendar,
        calendar_b: Calendar,
    ) -> None:
        with organization_context(organization_a):
            # Control: the same read, outside the block, is confined to A.
            assert set(Calendar.objects.values_list("pk", flat=True)) == {calendar_a.pk}

            with unscoped_default_manager():
                assert set(Calendar.objects.values_list("pk", flat=True)) == {
                    calendar_a.pk,
                    calendar_b.pk,
                }

    def test_an_unbound_read_answers_instead_of_raising(
        self, calendar_a: Calendar, calendar_b: Calendar
    ) -> None:
        """The case the admin actually hits: nothing is bound at all."""
        assert get_current_organization() is None
        with pytest.raises(OrganizationNotFoundError):
            Calendar.objects.count()

        with unscoped_default_manager():
            assert set(Calendar.objects.values_list("pk", flat=True)) == {
                calendar_a.pk,
                calendar_b.pk,
            }

    def test_the_models_own_queryset_class_survives(
        self, organization_a: Organization, calendar_a: Calendar
    ) -> None:
        """It unscopes; it does not fall back to a generic queryset.

        ``get_original_queryset`` keeps the model's own queryset class, so a
        manager method a ``ModelAdmin`` chains inside the block still exists.
        """
        with unscoped_default_manager():
            assert isinstance(Calendar.objects.all(), type(Calendar.original_manager.all()))


@pytest.mark.django_db
class TestTheBlockAlwaysRestores:
    def test_after_a_normal_exit(self, calendar_a: Calendar) -> None:
        with unscoped_default_manager():
            assert Calendar.objects.count() >= 1

        with pytest.raises(OrganizationNotFoundError):
            Calendar.objects.count()

    def test_after_an_exception_inside_the_block(self, calendar_a: Calendar) -> None:
        """The path that matters: a raising ``ModelForm`` must not leave it off."""
        with pytest.raises(RuntimeError, match="boom"), unscoped_default_manager():
            raise RuntimeError("boom")

        with pytest.raises(OrganizationNotFoundError):
            Calendar.objects.count()

    def test_a_bound_read_is_scoped_again_afterwards(
        self,
        organization_a: Organization,
        calendar_a: Calendar,
        calendar_b: Calendar,
    ) -> None:
        with organization_context(organization_a):
            with unscoped_default_manager():
                assert Calendar.objects.count() == 2  # noqa: PLR2004

            assert set(Calendar.objects.values_list("pk", flat=True)) == {calendar_a.pk}

    def test_nesting_restores_one_level_at_a_time(self, calendar_a: Calendar) -> None:
        """``ForeignKey.formfield`` runs inside an inline that is itself inside a
        change view, so the two callers can nest. The inner block's exit must not
        re-scope the outer one.
        """
        with unscoped_default_manager():
            with unscoped_default_manager():
                assert Calendar.objects.count() >= 1

            assert Calendar.objects.count() >= 1

        with pytest.raises(OrganizationNotFoundError):
            Calendar.objects.count()
