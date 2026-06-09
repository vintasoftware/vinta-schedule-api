import ipaddress as _ipaddress
import socket as _socket
from unittest.mock import MagicMock

import pytest
from rest_framework.test import APIClient


_ALLOWED_NETWORK_HOSTS = {
    "127.0.0.1",
    "::1",
    "localhost",
    "0.0.0.0",
    "",
    # docker-compose service hostnames
    "db",
    "broker",
    "result",
    "floci",
    "mailpit",
}


def _network_host_allowed(host) -> bool:
    if host in _ALLOWED_NETWORK_HOSTS:
        return True
    try:
        ip = _ipaddress.ip_address(host)
    except ValueError:
        return False  # unknown hostname -> assume external -> block
    return not ip.is_global  # loopback/private allowed, public internet blocked


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    """Tests must not touch the public internet.

    Allow loopback + docker-compose service hosts (postgres/redis/etc.); any connect to a
    public address fails fast with a clear error instead of hanging the suite / CI runner.
    """
    real_connect = _socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, (tuple, list)) and address else None
        if not _network_host_allowed(host):
            raise RuntimeError(
                f"Blocked external network access in test: {address!r}. "
                "Mock the external client (see conftest.mock_external_calendar_clients)."
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(_socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def mock_external_calendar_clients(monkeypatch):
    """Globally mock the external calendar provider clients so tests never hit their APIs.

    Covers the only external calendar APIs we consume:
      * Google Calendar  -> googleapiclient ``build`` + OAuth ``Credentials``/``Request``
      * Microsoft Outlook -> ``MSOutlookCalendarAPIClient`` (Graph)

    allauth's social-auth HTTP calls are caught by ``block_external_network``; tests that
    exercise those flows should mock the provider responses explicitly.
    """
    from calendar_integration.services.calendar_adapters import (
        google_calendar_adapter,
        ms_outlook_calendar_adapter,
    )

    # Google: build() returns a mock client; credentials never refresh over the network.
    # Configure the paginated list calls to return an empty page (no nextPageToken) so the
    # adapter's `while True` pagination loops terminate instead of spinning forever on a
    # truthy MagicMock token (which would OOM the worker).
    google_client = MagicMock(name="google_calendar_client")
    _empty_google_page = {"items": []}
    google_client.events.return_value.list.return_value.execute.return_value = _empty_google_page
    google_client.calendarList.return_value.list.return_value.execute.return_value = (
        _empty_google_page
    )
    monkeypatch.setattr(
        google_calendar_adapter, "build", MagicMock(name="google_build", return_value=google_client)
    )
    mock_credentials = MagicMock(name="GoogleCredentials")
    mock_credentials.return_value.valid = True
    monkeypatch.setattr(google_calendar_adapter, "Credentials", mock_credentials)
    monkeypatch.setattr(google_calendar_adapter, "Request", MagicMock(name="google_Request"))

    # Microsoft: the Graph API client is fully mocked (no test_connection / Graph calls).
    # Paginated reads return empty so the adapter's pagination loops terminate.
    ms_client = MagicMock(name="ms_outlook_client")
    ms_client.test_connection.return_value = True
    ms_client.list_events.return_value = []
    ms_client.list_calendars.return_value = []
    ms_client.get_room_events.return_value = []
    ms_client.get_events_delta.return_value = {"events": [], "next_link": None, "delta_link": None}
    ms_client.get_room_events_delta.return_value = {
        "events": [],
        "next_link": None,
        "delta_link": None,
    }
    monkeypatch.setattr(
        ms_outlook_calendar_adapter,
        "MSOutlookCalendarAPIClient",
        MagicMock(name="MSOutlookCalendarAPIClient", return_value=ms_client),
    )


@pytest.fixture
def user_password():
    from users.factories import DEFAULT_TEST_USER_PASSWORD

    return DEFAULT_TEST_USER_PASSWORD


@pytest.fixture
def user(user_password):
    from users.factories import UserFactory

    return UserFactory().create_user()


@pytest.fixture
def auth_client(user, user_password):
    client = APIClient()
    client.login(email=user.email, password=user_password)
    return client


@pytest.fixture
def anonymous_client():
    client = APIClient()
    return client


@pytest.fixture
def di_container():
    """Fixture to create a DI container."""
    from di_core.containers import container

    return container
