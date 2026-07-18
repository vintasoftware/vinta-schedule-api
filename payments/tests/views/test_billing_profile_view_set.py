from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from payments.models import BillingAddress, BillingProfile


@pytest.fixture
def organization():
    return baker.make(Organization)


@pytest.fixture
def membership(user, organization):
    """An active ADMIN membership: billing profile writes require admin (SHOULD-FIX 4)."""
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=organization,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def non_admin_membership(user, organization):
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=organization,
        role=OrganizationRole.MEMBER,
        is_active=True,
    )


@pytest.mark.django_db
class TestBillingProfileViewSet:
    def test_retrieve_billing_profile_success(self, auth_client, membership, organization):
        """Test successfully retrieving a billing profile."""
        # Create billing address and profile
        billing_address = baker.make(
            BillingAddress,
            street_name="Main Street",
            street_number="123",
            city="New York",
            state="NY",
            country="US",
            zip_code="10001",
            neighborhood="Manhattan",
            address_line_2="Apt 4B",
        )
        _billing_profile = baker.make(
            BillingProfile,
            organization=organization,
            document_type="SSN",
            document_number="123456789",
            billing_address=billing_address,
        )

        url = reverse("api:BillingProfile-retrieve")
        response = auth_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        assert response.data["id"] == organization.pk
        assert response.data["document_type"] == "SSN"
        assert response.data["document_number"] == "123456789"
        assert response.data["billing_address"]["street_name"] == "Main Street"
        assert response.data["billing_address"]["city"] == "New York"

    def test_retrieve_billing_profile_not_found(self, auth_client, membership):
        """Test retrieving billing profile when it doesn't exist."""
        url = reverse("api:BillingProfile-retrieve")
        response = auth_client.get(url)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_retrieve_billing_profile_no_membership(self, auth_client):
        """A user with no active organization membership cannot resolve a billing
        profile at all — the active org resolves to None and the lookup 404s."""
        url = reverse("api:BillingProfile-retrieve")
        response = auth_client.get(url)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_retrieve_billing_profile_unauthenticated(self, anonymous_client):
        """Test retrieving billing profile without authentication."""
        url = reverse("api:BillingProfile-retrieve")
        response = anonymous_client.get(url)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_retrieve_billing_profile_cross_organization_isolation(
        self, auth_client, membership, organization
    ):
        """A member of org A only ever sees org A's billing profile, never org B's,
        even when both organizations have a profile of their own.

        This proves tenant isolation on data, not merely absence of data: unlike a
        setup where org B has a profile but org A does not (which would 404 even
        against un-scoped, user-keyed code), both orgs have distinct profiles here.
        """
        org_a_address = baker.make(BillingAddress)
        baker.make(
            BillingProfile,
            organization=organization,
            document_type="SSN",
            document_number="AAA",
            billing_address=org_a_address,
        )
        other_organization = baker.make(Organization)
        org_b_address = baker.make(BillingAddress)
        baker.make(
            BillingProfile,
            organization=other_organization,
            document_type="SSN",
            document_number="BBB",
            billing_address=org_b_address,
        )

        url = reverse("api:BillingProfile-retrieve")
        response = auth_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        assert response.data["document_number"] == "AAA"

    def test_retrieve_billing_profile_switches_with_active_org_header(
        self, auth_client, user, organization
    ):
        """A user who is an active member of both org A and org B gets the profile
        of whichever org the `X-Organization-Id` header selects, for each org in
        turn."""
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        other_organization = baker.make(Organization)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=other_organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        org_a_address = baker.make(BillingAddress)
        baker.make(
            BillingProfile,
            organization=organization,
            document_type="SSN",
            document_number="AAA",
            billing_address=org_a_address,
        )
        org_b_address = baker.make(BillingAddress)
        baker.make(
            BillingProfile,
            organization=other_organization,
            document_type="SSN",
            document_number="BBB",
            billing_address=org_b_address,
        )

        url = reverse("api:BillingProfile-retrieve")

        response_a = auth_client.get(url, HTTP_X_ORGANIZATION_ID=str(organization.pk))
        assert response_a.status_code == status.HTTP_200_OK
        assert response_a.data["document_number"] == "AAA"

        response_b = auth_client.get(url, HTTP_X_ORGANIZATION_ID=str(other_organization.pk))
        assert response_b.status_code == status.HTTP_200_OK
        assert response_b.data["document_number"] == "BBB"

    def test_retrieve_billing_profile_header_names_non_member_org_forbidden(
        self, auth_client, membership, organization
    ):
        """A header naming an organization the caller does not belong to is
        rejected with 403, not silently ignored or resolved to the caller's org."""
        non_member_organization = baker.make(Organization)

        url = reverse("api:BillingProfile-retrieve")
        response = auth_client.get(url, HTTP_X_ORGANIZATION_ID=str(non_member_organization.pk))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_create_billing_profile_success(self, auth_client, membership, organization):
        """Test successfully creating a billing profile."""
        url = reverse("api:BillingProfile-create")
        data = {
            "contact_first_name": "Ada",
            "contact_last_name": "Lovelace",
            "contact_email": "billing@example.com",
            "contact_phone": "+1-555-0100",
            "document_type": "SSN",
            "document_number": "123456789",
            "billing_address": {
                "street_name": "Main Street",
                "street_number": "123",
                "city": "New York",
                "state": "NY",
                "country": "US",
                "zip_code": "10001",
                "neighborhood": "Manhattan",
                "address_line_2": "Apt 4B",
            },
        }

        response = auth_client.post(url, data, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["id"] == organization.pk
        assert response.data["document_type"] == "SSN"
        assert response.data["document_number"] == "123456789"
        assert response.data["billing_address"]["street_name"] == "Main Street"

        # Verify the billing profile was created in the database
        billing_profile = BillingProfile.objects.get(organization=organization)
        assert billing_profile.document_type == "SSN"
        assert billing_profile.document_number == "123456789"
        assert billing_profile.contact_email == "billing@example.com"
        assert billing_profile.billing_address.street_name == "Main Street"

    def test_create_billing_profile_duplicate_returns_conflict(
        self, auth_client, membership, organization
    ):
        """A second POST for an organization that already has a billing profile
        returns 409 instead of hitting the PK unique violation as a 500."""
        baker.make(BillingProfile, organization=organization)

        url = reverse("api:BillingProfile-create")
        data = {
            "contact_first_name": "Ada",
            "contact_email": "billing@example.com",
            "document_type": "SSN",
            "document_number": "123456789",
            "billing_address": {
                "street_name": "Main Street",
                "street_number": "123",
                "city": "New York",
                "state": "NY",
                "country": "US",
                "zip_code": "10001",
            },
        }

        response = auth_client.post(url, data, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert BillingProfile.objects.filter(organization=organization).count() == 1

    def test_create_billing_profile_no_membership_forbidden(self, auth_client):
        """A user with no active organization membership cannot create a billing
        profile — `request.organization` is None, so the write must be rejected
        rather than hit an IntegrityError."""
        url = reverse("api:BillingProfile-create")
        data = {
            "contact_first_name": "Ada",
            "contact_email": "billing@example.com",
            "document_type": "SSN",
            "document_number": "123456789",
            "billing_address": {
                "street_name": "Main Street",
                "street_number": "123",
                "city": "New York",
                "state": "NY",
                "country": "US",
                "zip_code": "10001",
            },
        }

        response = auth_client.post(url, data, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_create_billing_profile_non_admin_forbidden(self, auth_client, non_admin_membership):
        """A non-admin member may not create the organization's billing profile."""
        url = reverse("api:BillingProfile-create")
        data = {
            "contact_first_name": "Ada",
            "contact_email": "billing@example.com",
            "document_type": "SSN",
            "document_number": "123456789",
            "billing_address": {
                "street_name": "Main Street",
                "street_number": "123",
                "city": "New York",
                "state": "NY",
                "country": "US",
                "zip_code": "10001",
            },
        }

        response = auth_client.post(url, data, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_create_billing_profile_invalid_data(self, auth_client, membership):
        """Test creating billing profile with invalid data."""
        url = reverse("api:BillingProfile-create")
        data = {
            "document_type": "",  # Empty required field
            "billing_address": {
                "street_name": "",  # Empty required field
                "city": "New York",
            },
        }

        response = auth_client.post(url, data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_create_billing_profile_unauthenticated(self, anonymous_client):
        """Test creating billing profile without authentication."""
        url = reverse("api:BillingProfile-create")
        data = {
            "document_type": "SSN",
            "document_number": "123456789",
            "billing_address": {
                "street_name": "Main Street",
                "street_number": "123",
                "city": "New York",
                "state": "NY",
                "country": "US",
                "zip_code": "10001",
            },
        }

        response = anonymous_client.post(url, data, format="json")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_update_billing_profile_success(self, auth_client, membership, organization):
        """Test successfully updating a billing profile with PUT."""
        # Create existing billing address and profile
        billing_address = baker.make(
            BillingAddress,
            street_name="Old Street",
            street_number="456",
            city="Old City",
            state="CA",
            country="US",
            zip_code="90210",
        )
        billing_profile = baker.make(
            BillingProfile,
            organization=organization,
            document_type="DL",
            document_number="OLD123",
            billing_address=billing_address,
        )

        url = reverse("api:BillingProfile-update")
        data = {
            "document_type": "SSN",
            "document_number": "NEW456789",
            "billing_address": {
                "street_name": "New Street",
                "street_number": "789",
                "city": "New City",
                "state": "NY",
                "country": "US",
                "zip_code": "10001",
                "neighborhood": "Updated Hood",
                "address_line_2": "Suite 100",
            },
        }

        response = auth_client.put(url, data, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["document_type"] == "SSN"
        assert response.data["document_number"] == "NEW456789"
        assert response.data["billing_address"]["street_name"] == "New Street"
        assert response.data["billing_address"]["city"] == "New City"

        # Verify the billing profile was updated in the database
        billing_profile.refresh_from_db()
        billing_address.refresh_from_db()
        assert billing_profile.document_type == "SSN"
        assert billing_profile.document_number == "NEW456789"
        assert billing_address.street_name == "New Street"
        assert billing_address.city == "New City"

    def test_update_billing_profile_not_found(self, auth_client, membership):
        """Test updating billing profile when it doesn't exist."""
        url = reverse("api:BillingProfile-update")
        data = {
            "document_type": "SSN",
            "document_number": "123456789",
            "billing_address": {
                "street_name": "Main Street",
                "street_number": "123",
                "city": "New York",
                "state": "NY",
                "country": "US",
                "zip_code": "10001",
            },
        }

        response = auth_client.put(url, data, format="json")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_billing_profile_no_membership_forbidden(self, auth_client):
        """A user with no active organization membership cannot update a billing
        profile. `IsOrganizationAdmin` (gating writes, SHOULD-FIX 4) denies a
        membership-less caller before the lookup is even attempted."""
        url = reverse("api:BillingProfile-update")
        data = {"document_type": "SSN", "document_number": "123456789"}

        response = auth_client.put(url, data, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_update_billing_profile_non_admin_forbidden(
        self, auth_client, non_admin_membership, organization
    ):
        """A non-admin member may not update the organization's billing profile,
        even when one already exists."""
        baker.make(BillingProfile, organization=organization)

        url = reverse("api:BillingProfile-update")
        data = {"document_type": "SSN", "document_number": "123456789"}

        response = auth_client.put(url, data, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_partial_update_billing_profile_success(self, auth_client, membership, organization):
        """Test successfully partially updating a billing profile with PATCH."""
        # Create existing billing address and profile
        billing_address = baker.make(
            BillingAddress,
            street_name="Old Street",
            street_number="456",
            city="Old City",
            state="CA",
            country="US",
            zip_code="90210",
        )
        billing_profile = baker.make(
            BillingProfile,
            organization=organization,
            document_type="DL",
            document_number="OLD123",
            billing_address=billing_address,
        )

        url = reverse("api:BillingProfile-partial_update")
        data = {"document_number": "UPDATED123", "billing_address": {"city": "Updated City"}}

        response = auth_client.patch(url, data, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["document_type"] == "DL"  # Should remain unchanged
        assert response.data["document_number"] == "UPDATED123"  # Should be updated
        assert (
            response.data["billing_address"]["street_name"] == "Old Street"
        )  # Should remain unchanged
        assert response.data["billing_address"]["city"] == "Updated City"  # Should be updated

        # Verify the billing profile was partially updated in the database
        billing_profile.refresh_from_db()
        billing_address.refresh_from_db()
        assert billing_profile.document_type == "DL"
        assert billing_profile.document_number == "UPDATED123"
        assert billing_address.street_name == "Old Street"
        assert billing_address.city == "Updated City"

    def test_partial_update_billing_profile_address_only(
        self, auth_client, membership, organization
    ):
        """Test partially updating only the billing address."""
        # Create existing billing address and profile
        billing_address = baker.make(
            BillingAddress,
            street_name="Old Street",
            street_number="456",
            city="Old City",
            state="CA",
            country="US",
            zip_code="90210",
        )
        _billing_profile = baker.make(
            BillingProfile,
            organization=organization,
            document_type="DL",
            document_number="OLD123",
            billing_address=billing_address,
        )

        url = reverse("api:BillingProfile-partial_update")
        data = {"billing_address": {"zip_code": "10001", "neighborhood": "New Neighborhood"}}

        response = auth_client.patch(url, data, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["document_type"] == "DL"  # Should remain unchanged
        assert response.data["document_number"] == "OLD123"  # Should remain unchanged
        assert (
            response.data["billing_address"]["street_name"] == "Old Street"
        )  # Should remain unchanged
        assert response.data["billing_address"]["city"] == "Old City"  # Should remain unchanged
        assert response.data["billing_address"]["zip_code"] == "10001"  # Should be updated
        assert (
            response.data["billing_address"]["neighborhood"] == "New Neighborhood"
        )  # Should be updated

    def test_partial_update_billing_profile_not_found(self, auth_client, membership):
        """Test partially updating billing profile when it doesn't exist."""
        url = reverse("api:BillingProfile-partial_update")
        data = {"document_number": "NEW123"}

        response = auth_client.patch(url, data, format="json")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_partial_update_billing_profile_no_membership_forbidden(self, auth_client):
        """A user with no active organization membership cannot partially update a
        billing profile. `IsOrganizationAdmin` (gating writes, SHOULD-FIX 4) denies
        a membership-less caller before the lookup is even attempted."""
        url = reverse("api:BillingProfile-partial_update")
        data = {"document_number": "NEW123"}

        response = auth_client.patch(url, data, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_partial_update_billing_profile_non_admin_forbidden(
        self, auth_client, non_admin_membership, organization
    ):
        """A non-admin member may not partially update the organization's billing
        profile, even when one already exists."""
        baker.make(BillingProfile, organization=organization)

        url = reverse("api:BillingProfile-partial_update")
        data = {"document_number": "NEW123"}

        response = auth_client.patch(url, data, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_all_endpoints_require_authentication(self, anonymous_client):
        """Test that all billing profile endpoints require authentication."""
        endpoints = [
            ("api:BillingProfile-retrieve", "get"),
            ("api:BillingProfile-create", "post"),
            ("api:BillingProfile-update", "put"),
            ("api:BillingProfile-partial_update", "patch"),
        ]

        for endpoint_name, method in endpoints:
            url = reverse(endpoint_name)
            response = getattr(anonymous_client, method)(url, {}, format="json")
            assert response.status_code == status.HTTP_401_UNAUTHORIZED, (
                f"Failed for {endpoint_name}"
            )

    def test_billing_profile_relationships(self, organization):
        """Test that billing profile correctly relates to the organization and address."""
        # Create billing address and profile
        billing_address = baker.make(
            BillingAddress,
            street_name="Test Street",
            street_number="123",
            city="Test City",
            state="TS",
            country="US",
            zip_code="12345",
        )
        billing_profile = baker.make(
            BillingProfile,
            organization=organization,
            document_type="SSN",
            document_number="123456789",
            billing_address=billing_address,
        )

        # Test that the billing profile is correctly linked
        assert billing_profile.organization == organization
        assert billing_profile.billing_address == billing_address
        assert billing_address.billing_profile == billing_profile

        # Test that we can access the billing profile through the organization
        assert organization.billing_profile == billing_profile

    def test_create_billing_profile_with_minimal_address_data(self, auth_client, membership):
        """Test creating billing profile with minimal required address data."""
        url = reverse("api:BillingProfile-create")
        data = {
            "contact_first_name": "Ada",
            "contact_email": "billing@example.com",
            "document_type": "ID",
            "document_number": "987654321",
            "billing_address": {
                "street_name": "Simple St",
                "street_number": "1",
                "city": "Town",
                "state": "ST",
                "country": "US",
                "zip_code": "54321",
                # Optional fields like neighborhood and address_line_2 are omitted
            },
        }

        response = auth_client.post(url, data, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["billing_address"]["neighborhood"] == ""
        assert response.data["billing_address"]["address_line_2"] == ""
