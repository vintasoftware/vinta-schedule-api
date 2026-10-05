"""Fixtures shared by the calendar_integration tests."""

from collections.abc import Iterator
from unittest.mock import Mock, patch

import pytest
from dependency_injector import providers

from calendar_integration.services.calendar_adapters import ms_outlook_calendar_adapter
from calendar_integration.services.calendar_clients.ms_app_only_token import (
    MicrosoftAppOnlyTokenProvider,
)
from calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client import (
    MSOutlookCalendarAPIClient,
)
from calendar_integration.tests.ms_room_graph import FakeRoomCalendarGraph


CLIENT_MODULE = "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client"


@pytest.fixture
def ms_room_token_provider() -> Mock:
    provider = Mock(spec=MicrosoftAppOnlyTokenProvider)
    provider.get_token.return_value = Mock(access_token="app-only-token")
    return provider


@pytest.fixture
def ms_room_graph(
    monkeypatch, di_container, ms_room_token_provider
) -> Iterator[FakeRoomCalendarGraph]:
    """Route the app-only room sync's Graph calls to a ``FakeRoomCalendarGraph``.

    The test sets the events with ``ms_room_graph.events``; it starts empty.
    """
    graph = FakeRoomCalendarGraph(events=[])
    monkeypatch.setattr(
        ms_outlook_calendar_adapter, "MSOutlookCalendarAPIClient", MSOutlookCalendarAPIClient
    )
    session = Mock(headers={})
    session.request.side_effect = graph
    with (
        patch(f"{CLIENT_MODULE}.requests.Session", return_value=session),
        patch(f"{CLIENT_MODULE}.quote_limiter"),
        di_container.microsoft_app_only_token_provider.override(
            providers.Object(ms_room_token_provider)
        ),
    ):
        yield graph
