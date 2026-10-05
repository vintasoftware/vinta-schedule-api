"""Tests for the durationSeconds field on AppointmentType."""

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.models import AppointmentType
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


@pytest.fixture
def organization():
    return baker.make(Organization)


@pytest.fixture
def graphql_client(organization):
    system_user, token = PublicAPIAuthService().create_system_user(
        integration_name="test_integration", organization=organization
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.APPOINTMENT_TYPE
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
@pytest.mark.parametrize(("duration", "expected"), [(timedelta(minutes=30), 1800), (None, None)])
def test_appointment_type_duration_seconds(
    mock_rate_limiter, graphql_client, organization, duration, expected
):
    mock_rate_limiter.return_value = iter([None])
    appointment_type = baker.make(AppointmentType, organization=organization, duration=duration)
    query = "query($id: Int!) { appointmentType(appointmentTypeId: $id) { durationSeconds } }"

    response = graphql_client.post(
        "/graphql/",
        data=json.dumps({"query": query, "variables": {"id": appointment_type.id}}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json() == {"data": {"appointmentType": {"durationSeconds": expected}}}
