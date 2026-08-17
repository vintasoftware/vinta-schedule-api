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
``common.managers.OrganizationScopedManager``): related managers are not scoped
-- reverse foreign keys, many-to-many, and prefetches through either -- and a
write that names its own organization is not either. "Names" means in the
arguments that reach the *lookup*: ``get_or_create``/``update_or_create``'s
``defaults`` does not count, and the last class here is why.
"""

from __future__ import annotations

import pytest
from vinta_orgs.exceptions import OrganizationNotFoundError

from calendar_integration.models import Calendar, CalendarOwnership, CalendarSync
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationMembership
from users.models import User


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

    def test_bulk_create_works_unbound_because_each_object_carries_its_own(
        self, organization_a, organization_b
    ):
        """``bulk_create`` never calls ``save()``, so the context's stamping is not
        available to it -- the objects have to carry their organizations, and they
        do. The manager therefore lets the statement through unbound rather than
        refusing a write that is already unambiguous.
        """
        created = Calendar.objects.bulk_create(
            [
                Calendar(name="A's", organization=organization_a),
                Calendar(name="B's", organization=organization_b),
            ]
        )

        assert {calendar.organization_id for calendar in created} == {
            organization_a.id,
            organization_b.id,
        }
        assert set(Calendar.objects.filter_by_organization(organization_a.id)) == {created[0]}

    def test_bulk_update_works_unbound_and_writes_each_rows_own_organization(
        self, organization_a, organization_b, calendar_a, calendar_b
    ):
        """Same reasoning as ``bulk_create``: the statement is addressed to primary
        keys the caller already holds. Both organizations' rows are updated in one
        call, which no ambient scope could express.
        """
        calendar_a.name = "renamed A"
        calendar_b.name = "renamed B"

        Calendar.objects.bulk_update([calendar_a, calendar_b], ["name"])

        calendar_a.refresh_from_db()
        calendar_b.refresh_from_db()
        assert (calendar_a.name, calendar_a.organization_id) == ("renamed A", organization_a.id)
        assert (calendar_b.name, calendar_b.organization_id) == ("renamed B", organization_b.id)

    def test_a_many_to_many_traversal_reads_unbound_and_stays_in_its_organization(
        self, organization_a, organization_b, calendar_a, calendar_b
    ):
        """``Calendar.memberships`` goes through ``CalendarOwnership`` on
        ``through_fields=("calendar", "membership")`` -- the organization-safe
        relation -- so the first hop of the join carries ``organization`` and the
        related manager needs no ambient one. Pinned because a many-to-many
        related manager lands in the same unscoped carve-out as a reverse foreign
        key while resting on a *different* argument (see
        ``common.managers.OrganizationScopedManager``).
        """
        user_a = User.objects.create_user(email="a@example.com")
        user_b = User.objects.create_user(email="b@example.com")
        membership_a = OrganizationMembership.objects.create(
            user=user_a, organization=organization_a
        )
        membership_b = OrganizationMembership.objects.create(
            user=user_b, organization=organization_b
        )
        CalendarOwnership.objects.create(
            organization=organization_a, calendar=calendar_a, membership_user_id=user_a.id
        )
        CalendarOwnership.objects.create(
            organization=organization_b, calendar=calendar_b, membership_user_id=user_b.id
        )

        assert list(calendar_a.memberships.all()) == [membership_a]
        assert list(calendar_b.memberships.all()) == [membership_b]

    def test_a_prefetch_through_a_related_manager_reads_unbound(
        self, organization_a, organization_b, calendar_a, calendar_b
    ):
        """``prefetch_related`` runs the related manager's queryset as a second,
        separate query rather than as a join, so it hits ``get_queryset`` on its
        own -- exactly the path the ``self.instance`` carve-out has to cover for
        the reverse accessor to work outside a bound context.
        """
        sync_a = CalendarSync.objects.create(
            calendar=calendar_a,
            organization=organization_a,
            start_datetime="2025-06-22T00:00:00Z",
            end_datetime="2025-06-22T23:59:00Z",
            should_update_events=True,
        )
        CalendarSync.objects.create(
            calendar=calendar_b,
            organization=organization_b,
            start_datetime="2025-06-22T00:00:00Z",
            end_datetime="2025-06-22T23:59:00Z",
            should_update_events=True,
        )

        prefetched = Calendar.objects.filter_by_organization(organization_a.id).prefetch_related(
            "syncs"
        )

        assert [list(calendar.syncs.all()) for calendar in prefetched] == [[sync_a]]


class TestGetOrCreateDoesNotWidenItsLookup:
    """``defaults`` names the organization; ``kwargs`` does not.

    ``defaults`` is only applied to the row that gets created or updated -- it
    takes no part in the ``get()`` that runs first. So it cannot make the lookup
    safe, and the manager must not treat it as if it did: an unscoped
    ``get(external_id=...)`` reaches every tenant's rows, and
    ``update_or_create`` would then ``save()`` whichever one it found.
    """

    def test_get_or_create_does_not_return_another_organizations_row(
        self, organization_a, organization_b
    ):
        Calendar.objects.create(
            name="B's shared-id calendar", external_id="shared", organization=organization_b
        )

        with organization_context(organization_a):
            calendar, created = Calendar.objects.get_or_create(
                external_id="shared",
                defaults={"organization": organization_a, "name": "A's shared-id calendar"},
            )

        assert created is True
        assert calendar.organization_id == organization_a.id
        assert Calendar.objects.filter_by_organization(organization_b.id).count() == 1

    def test_get_or_create_refuses_rather_than_reaching_across_tenants_unbound(
        self, organization_a, organization_b
    ):
        """With nothing bound the lookup is genuinely unscoped, so it raises. The
        alternative -- honouring ``defaults`` -- is what handed back another
        organization's row.
        """
        Calendar.objects.create(
            name="B's shared-id calendar", external_id="shared", organization=organization_b
        )

        with pytest.raises(OrganizationNotFoundError):
            Calendar.objects.get_or_create(
                external_id="shared",
                defaults={"organization": organization_a, "name": "A's shared-id calendar"},
            )

    def test_update_or_create_does_not_write_another_organizations_row(
        self, organization_a, organization_b
    ):
        other = Calendar.objects.create(
            name="B's shared-id calendar", external_id="shared", organization=organization_b
        )

        with organization_context(organization_a):
            calendar, created = Calendar.objects.update_or_create(
                external_id="shared",
                defaults={"organization": organization_a, "name": "A's shared-id calendar"},
            )

        assert created is True
        assert calendar.organization_id == organization_a.id

        other.refresh_from_db()
        assert other.name == "B's shared-id calendar"
        assert other.organization_id == organization_b.id

    def test_naming_the_organization_in_the_lookup_still_skips_the_scope(
        self, organization_a, organization_b
    ):
        """The carve-out itself is unchanged: an organization in ``kwargs`` *does*
        narrow the ``get()``, so the call still works unbound. Every
        ``get_or_create`` / ``update_or_create`` in this codebase is spelled this
        way.
        """
        Calendar.objects.create(
            name="B's shared-id calendar", external_id="shared", organization=organization_b
        )

        calendar, created = Calendar.objects.get_or_create(
            external_id="shared",
            organization=organization_a,
            defaults={"name": "A's shared-id calendar"},
        )

        assert created is True
        assert calendar.organization_id == organization_a.id
