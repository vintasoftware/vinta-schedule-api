"""Fixtures shared by the calendar integration tests.

A test module that defines a fixture with the same name uses its own instead.
"""

import datetime
from collections.abc import Iterator
from unittest.mock import Mock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import Calendar
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization
from users.models import Profile, User


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Calendar Org")


@pytest.fixture
def owner(db) -> User:
    return User.objects.create_user(email="provider@example.com", password="pw")  # noqa: S106


@pytest.fixture
def social_account(db) -> SocialAccount:
    """A Google social account, for a ``CalendarService`` that authenticates as a user."""
    user = User.objects.create_user(email="social@example.com", password="testpass123")  # noqa: S106
    Profile.objects.create(user=user)
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="88888")


@pytest.fixture
def social_token(social_account: SocialAccount) -> SocialToken:
    return SocialToken.objects.create(
        account=social_account,
        token="test_access_token",
        token_secret="test_refresh_token",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )


@pytest.fixture
def mock_google_adapter() -> Iterator[Mock]:
    """Replace ``GoogleCalendarAdapter`` with a mock and yield the adapter it returns."""
    with patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.GoogleCalendarAdapter"
    ) as mock_adapter_class:
        mock_adapter = Mock()
        mock_adapter.provider = CalendarProvider.GOOGLE
        # Avoid Django expression-resolution attribute hits on the Mock.
        del mock_adapter.resolve_expression
        del mock_adapter.get_source_expressions
        mock_adapter_class.return_value = mock_adapter
        mock_adapter_class.from_service_account_credentials.return_value = mock_adapter
        yield mock_adapter


@pytest.fixture
def google_calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Provider calendar",
        external_id="provider@example.com",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def write_adapter() -> Iterator[Mock]:
    """The adapter ``CalendarService`` writes to a provider through, as a mock."""
    adapter = Mock()
    with patch.object(CalendarService, "_get_write_adapter_for_calendar", return_value=adapter):
        yield adapter
