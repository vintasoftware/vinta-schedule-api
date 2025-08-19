from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status

from payments.models import BillingAddress, BillingProfile
from users.models import Profile


@pytest.mark.django_db
class TestBillingProfileViewSet:
    def test_retrieve_billing_profile_success(self, auth_client, user):
        """Test successfully retrieving a billing profile."""
        # Create a profile for the user
        _profile = baker.make(Profile, user=user, first_name="John", last_name="Doe")

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
            user=user,
            document_type="SSN",
            document_number="123456789",
            billing_address=billing_address,
        )

        url = reverse("api:BillingProfile-retrieve")
        response = auth_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        assert response.data["id"] == user.pk
        assert response.data["document_type"] == "SSN"
        assert response.data["document_number"] == "123456789"
        assert response.data["billing_address"]["street_name"] == "Main Street"
        assert response.data["billing_address"]["city"] == "New York"

    def test_retrieve_billing_profile_not_found(self, auth_client, user):
        """Test retrieving billing profile when it doesn't exist."""
        url = reverse("api:BillingProfile-retrieve")
        response = auth_client.get(url)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_retrieve_billing_profile_unauthenticated(self, anonymous_client):
        """Test retrieving billing profile without authentication."""
        url = reverse("api:BillingProfile-retrieve")
        response = anonymous_client.get(url)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_create_billing_profile_success(self, auth_client, user):
        """Test successfully creating a billing profile."""
        # Create a profile for the user
        baker.make(Profile, user=user, first_name="John", last_name="Doe")

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
                "neighborhood": "Manhattan",
                "address_line_2": "Apt 4B",
            },
        }

        response = auth_client.post(url, data, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["id"] == user.pk
        assert response.data["document_type"] == "SSN"
        assert response.data["document_number"] == "123456789"
        assert response.data["billing_address"]["street_name"] == "Main Street"

        # Verify the billing profile was created in the database
        billing_profile = BillingProfile.objects.get(user=user)
        assert billing_profile.document_type == "SSN"
        assert billing_profile.document_number == "123456789"
        assert billing_profile.billing_address.street_name == "Main Street"

    def test_create_billing_profile_invalid_data(self, auth_client, user):
        """Test creating billing profile with invalid data."""
        # Create a profile for the user
        baker.make(Profile, user=user, first_name="John", last_name="Doe")

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

    def test_update_billing_profile_success(self, auth_client, user):
        """Test successfully updating a billing profile with PUT."""
        # Create a profile for the user
        baker.make(Profile, user=user, first_name="John", last_name="Doe")

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
            user=user,
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

    def test_update_billing_profile_not_found(self, auth_client, user):
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

    def test_partial_update_billing_profile_success(self, auth_client, user):
        """Test successfully partially updating a billing profile with PATCH."""
        # Create a profile for the user
        baker.make(Profile, user=user, first_name="John", last_name="Doe")

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
            user=user,
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

    def test_partial_update_billing_profile_address_only(self, auth_client, user):
        """Test partially updating only the billing address."""
        # Create a profile for the user
        baker.make(Profile, user=user, first_name="John", last_name="Doe")

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
            user=user,
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

    def test_partial_update_billing_profile_not_found(self, auth_client, user):
        """Test partially updating billing profile when it doesn't exist."""
        url = reverse("api:BillingProfile-partial_update")
        data = {"document_number": "NEW123"}

        response = auth_client.patch(url, data, format="json")

        assert response.status_code == status.HTTP_404_NOT_FOUND

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
            assert (
                response.status_code == status.HTTP_401_UNAUTHORIZED
            ), f"Failed for {endpoint_name}"

    def test_billing_profile_relationships(self, auth_client, user):
        """Test that billing profile correctly relates to user and billing address."""
        # Create a profile for the user
        _profile = baker.make(Profile, user=user, first_name="John", last_name="Doe")

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
            user=user,
            document_type="SSN",
            document_number="123456789",
            billing_address=billing_address,
        )

        # Test that the billing profile is correctly linked
        assert billing_profile.user == user
        assert billing_profile.billing_address == billing_address
        assert billing_address.billing_profile == billing_profile

        # Test that we can access the billing profile through the user
        assert user.billing_profile == billing_profile

    def test_create_billing_profile_with_minimal_address_data(self, auth_client, user):
        """Test creating billing profile with minimal required address data."""
        # Create a profile for the user
        baker.make(Profile, user=user, first_name="John", last_name="Doe")

        url = reverse("api:BillingProfile-create")
        data = {
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
