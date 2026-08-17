"""``process_webhook_event``: the task body, not just the service method it calls.

The service half (``WebhookService.process_webhook_event``) is covered in
``webhooks/tests/test_services.py``. The *task* is what establishes the
organization binding, and since Phase 2b flipped ``webhooks`` onto
``SingleOrganizationModelMixin`` the ``WebhookEvent.objects.filter(...)`` inside
it is an implicitly scoped read: with nothing bound it raises
``OrganizationNotFoundError`` rather than returning the event. Nothing executed
this function's body before -- every existing test patches ``.delay`` /
``.apply_async`` and asserts the scheduling call.

Three rows are pinned: the happy path (the binding is in place and the event
transitions), the stale-``organization_id`` guard (returns before any scoped
read), and an event belonging to a different organization (not processed).
"""

from unittest.mock import Mock, patch

import pytest
from model_bakery import baker

from organizations.models import Organization
from webhooks.constants import WebhookEventType, WebhookStatus
from webhooks.models import WebhookConfiguration, WebhookEvent
from webhooks.services.webhook_service import WebhookService
from webhooks.tasks import process_webhook_event


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Task Org")


@pytest.fixture
def other_organization() -> Organization:
    return Organization.objects.create(name="Other Task Org")


def _pending_event(organization: Organization) -> WebhookEvent:
    configuration = baker.make(
        WebhookConfiguration,
        organization=organization,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/webhook",
        headers={},
    )
    return baker.make(
        WebhookEvent,
        organization=organization,
        configuration=configuration,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/webhook",
        headers={},
        payload={"event": "test"},
        status=WebhookStatus.PENDING,
    )


def _ok_response() -> Mock:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"received": True}
    response.headers = {"Content-Type": "application/json"}
    return response


class TestTheTaskRunsItsBody:
    def test_a_pending_event_is_sent_and_marked_successful(
        self, organization: Organization
    ) -> None:
        """The read at the top of the task is scoped; only the binding makes it work."""
        event = _pending_event(organization)

        with patch(
            "webhooks.services.webhook_service.requests.post", return_value=_ok_response()
        ) as mock_post:
            process_webhook_event(
                event_id=event.id,
                organization_id=organization.id,
                webhook_service=WebhookService(),
            )

        assert mock_post.call_count == 1
        assert mock_post.call_args.args[0] == "https://example.com/webhook"
        event.refresh_from_db()
        assert event.status == WebhookStatus.SUCCESS
        assert event.response_status == 200  # noqa: PLR2004


class TestTheTaskDeclinesToRun:
    def test_a_stale_organization_id_returns_before_any_scoped_read(
        self, organization: Organization
    ) -> None:
        """The organization was deleted between enqueue and execution.

        The guard has to come first: with the organization gone there is nothing
        to bind, and the scoped read below it would raise
        ``OrganizationNotFoundError`` and retry the task forever.
        """
        event = _pending_event(organization)
        stale_organization_id = organization.id
        Organization.objects.filter(id=stale_organization_id).delete()

        with patch("webhooks.services.webhook_service.requests.post") as mock_post:
            result = process_webhook_event(
                event_id=event.id,
                organization_id=stale_organization_id,
                webhook_service=WebhookService(),
            )

        assert result is None
        assert mock_post.call_count == 0

    def test_an_event_of_another_organization_is_not_processed(
        self, organization: Organization, other_organization: Organization
    ) -> None:
        event_of_other = _pending_event(other_organization)

        with patch("webhooks.services.webhook_service.requests.post") as mock_post:
            process_webhook_event(
                event_id=event_of_other.id,
                organization_id=organization.id,
                webhook_service=WebhookService(),
            )

        assert mock_post.call_count == 0
        event_of_other.refresh_from_db()
        assert event_of_other.status == WebhookStatus.PENDING

    def test_nothing_happens_without_an_injected_service(self, organization: Organization) -> None:
        """The ``webhook_service or return`` guard, which every other row assumes away."""
        event = _pending_event(organization)

        with patch("webhooks.services.webhook_service.requests.post") as mock_post:
            result = process_webhook_event(
                event_id=event.id, organization_id=organization.id, webhook_service=None
            )

        assert result is None
        assert mock_post.call_count == 0
        event.refresh_from_db()
        assert event.status == WebhookStatus.PENDING
