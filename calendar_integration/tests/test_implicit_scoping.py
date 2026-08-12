"""What ``.objects`` means on a ``calendar_integration`` model after Phase 2a.

Three claims, one per class:

1. A query run inside ``organization_context(...)`` returns that organization's
   rows and only those, without the caller filtering for it.
2. The same query with nothing bound **raises** ``OrganizationNotFoundError``
   rather than returning an empty result -- ``STRICT_ORGANIZATION_FILTER``.
3. The documented ways out -- ``original_manager`` / ``objects.unscoped()`` /
   ``objects.filter_by_organization(...)`` -- still work, and the first two still
   cross organizations.

Plus the two carve-outs this project makes on top of the package (see
``common.managers.OrganizationScopedManager``): reverse related managers are not
scoped, and a write that names its own organization is not either.
"""

from __future__ import annotations

import pytest
from vinta_orgs.exceptions import OrganizationNotFoundError

from calendar_integration.models import Calendar, CalendarSync
from common.organization_context import organization_context
from organizations.models import Organization


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization_a() -> Organization:
    return Organization.objects.create(name="Org A")


@pytest.fixture
def organization_b() -> Organization:
    return Organization.objects.create(name="Org B")


@pytest.fixture
def calendar_a(organization_a: Organization) -> Calendar:
    return Calendar.objects.create(name="A's calendar", organization=organization_a)


@pytest.fixture
def calendar_b(organization_b: Organization) -> Calendar:
    return Calendar.objects.create(name="B's calendar", organization=organization_b)


class TestABoundQueryScopesItself:
    def test_returns_only_the_bound_organizations_rows(
        self, organization_a, calendar_a, calendar_b
    ):
        with organization_context(organization_a):
            assert list(Calendar.objects.all()) == [calendar_a]

    def test_a_lookup_for_another_organizations_row_reads_as_missing(
        self, organization_a, calendar_b
    ):
        with organization_context(organization_a):
            with pytest.raises(Calendar.DoesNotExist):
                Calendar.objects.get(pk=calendar_b.pk)

    def test_the_scope_follows_the_binding_rather_than_the_call_site(
        self, organization_a, organization_b, calendar_a, calendar_b
    ):
        with organization_context(organization_a):
            assert Calendar.objects.count() == 1
        with organization_context(organization_b):
            assert Calendar.objects.count() == 1

    def test_the_binding_does_not_survive_the_block(self, organization_a, calendar_a):
        with organization_context(organization_a):
            assert Calendar.objects.count() == 1

        with pytest.raises(OrganizationNotFoundError):
            Calendar.objects.count()


class TestAnUnboundQueryRaises:
    def test_reading_raises_rather_than_returning_nothing(self, calendar_a):
        with pytest.raises(OrganizationNotFoundError):
            list(Calendar.objects.all())

    def test_the_refusal_happens_when_the_queryset_is_built_not_when_it_is_consumed(
        self, calendar_a
    ):
        """Eager on purpose: the traceback points at the call site that forgot to
        scope, not at whatever later line happened to iterate the queryset.
        """
        with pytest.raises(OrganizationNotFoundError):
            Calendar.objects.filter(name="anything")

    def test_the_message_names_the_model_and_the_ways_out(self, calendar_a):
        with pytest.raises(OrganizationNotFoundError) as exc_info:
            Calendar.objects.count()

        message = str(exc_info.value)
        assert "Calendar" in message
        assert "organization_context" in message
        assert "filter_by_organization" in message
        assert "original_manager" in message

    def test_none_is_still_allowed_unbound(self, calendar_a):
        """``return Model.objects.none()`` is how a view says "you may read
        nothing", and drf-spectacular calls it during schema generation. It can
        leak nothing, so it must not require a binding.
        """
        assert list(Calendar.objects.none()) == []


class TestTheDocumentedWaysOut:
    def test_original_manager_crosses_organizations(self, calendar_a, calendar_b):
        assert set(Calendar.original_manager.all()) == {calendar_a, calendar_b}

    def test_unscoped_crosses_organizations_and_keeps_the_models_queryset(
        self, calendar_a, calendar_b
    ):
        queryset = Calendar.objects.unscoped()

        assert set(queryset) == {calendar_a, calendar_b}
        # ``unscoped()`` goes through the manager, so the model's own queryset
        # methods survive -- ``original_manager`` is the package's generic one.
        assert hasattr(queryset, "live_of_type")

    def test_filter_by_organization_works_unbound_and_scopes(
        self, organization_a, calendar_a, calendar_b
    ):
        assert list(Calendar.objects.filter_by_organization(organization_a.id)) == [calendar_a]

    def test_filter_by_organization_means_what_it_says_under_a_different_binding(
        self, organization_a, organization_b, calendar_a, calendar_b
    ):
        """It starts from the *unscoped* queryset, so it reaches the organization
        it names even while another one is bound. Reaching another organization's
        rows is the only reason to call it.
        """
        with organization_context(organization_b):
            assert list(Calendar.objects.filter_by_organization(organization_a.id)) == [calendar_a]


class TestTheProjectsTwoCarveOuts:
    def test_a_reverse_related_manager_reads_unbound(self, organization_a, calendar_a):
        """A reverse accessor is already restricted to one parent row, and for a
        safe relation that filter carries the parent's organization. Demanding an
        ambient one on top would break every traversal outside a bound context.
        """
        sync = CalendarSync.objects.create(
            calendar=calendar_a,
            organization=organization_a,
            start_datetime="2025-06-22T00:00:00Z",
            end_datetime="2025-06-22T23:59:00Z",
            should_update_events=True,
        )

        assert list(calendar_a.syncs.all()) == [sync]

    def test_a_reverse_related_manager_still_cannot_reach_another_organization(
        self, organization_a, organization_b, calendar_a, calendar_b
    ):
        CalendarSync.objects.create(
            calendar=calendar_b,
            organization=organization_b,
            start_datetime="2025-06-22T00:00:00Z",
            end_datetime="2025-06-22T23:59:00Z",
            should_update_events=True,
        )

        assert list(calendar_a.syncs.all()) == []

    def test_a_write_that_names_its_organization_works_unbound(self, organization_a):
        calendar = Calendar.objects.create(name="named", organization=organization_a)

        assert calendar.organization_id == organization_a.id

    def test_a_write_that_names_no_organization_adopts_the_bound_one(self, organization_a):
        """``SingleOrganizationModelMixin.save()`` resolves the organization from
        the context when the instance was built without one. Pinned deliberately:
        it is the one place the ambient context *writes* rather than reads.
        """
        with organization_context(organization_a):
            calendar = Calendar.objects.create(name="adopted")

        assert calendar.organization_id == organization_a.id

    def test_a_write_that_names_no_organization_raises_unbound(self):
        with pytest.raises(OrganizationNotFoundError):
            Calendar.objects.create(name="orphan")
