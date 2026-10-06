"""Integration tests for ``GoogleWriteAccessService.verify``.

Only Google's own credential builder and API client are mocked; the real
``GoogleCalendarAdapter`` runs, so the scope it asks for and the error mapping it
applies are part of what is tested.
"""

import datetime
from collections.abc import Iterator
from unittest.mock import Mock, patch

from django.utils import timezone

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from model_bakery import baker

from calendar_integration.models import Calendar, GoogleCalendarServiceAccount
from calendar_integration.services.calendar_adapters.google_calendar_adapter import (
    _SA_WRITE_SCOPES,
)
from calendar_integration.services.dataclasses import GoogleWriteAccessResult
from calendar_integration.services.google_write_access_service import GoogleWriteAccessService
from common.redis import ResilientLimiter
from organizations.models import Organization


ADAPTER_MODULE = "calendar_integration.services.calendar_adapters.google_calendar_adapter"


@pytest.fixture
def google_directory() -> Iterator[tuple[Mock, Mock]]:
    """Patch Google's credential builder and ``build``; yield (from_info, admin_client)."""
    admin_client = Mock()
    with (
        patch(
            f"{ADAPTER_MODULE}.google_service_account.Credentials.from_service_account_info"
        ) as from_info,
        patch(
            f"{ADAPTER_MODULE}.build",
            side_effect=lambda service, *_, **__: admin_client if service == "admin" else Mock(),
        ),
        patch(f"{ADAPTER_MODULE}.read_quote_limiter", spec=ResilientLimiter),
        patch(f"{ADAPTER_MODULE}.write_quote_limiter", spec=ResilientLimiter),
    ):
        yield from_info, admin_client


def _buildings_list(admin_client: Mock) -> Mock:
    return admin_client.resources.return_value.buildings.return_value.list


def _http_error(status: int) -> HttpError:
    return HttpError(Mock(status=status, reason=""), b'{"error": {"message": "nope"}}')


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


def _service_account(organization: Organization, **kwargs) -> GoogleCalendarServiceAccount:
    return GoogleCalendarServiceAccount.objects.create(
        organization=organization,
        email="service@example.com",
        admin_email="admin@example.com",
        private_key_id="key-id",
        private_key="private-key",
        **kwargs,
    )


@pytest.mark.django_db
class TestVerify:
    def test_success_enables_writes_with_the_write_scope(self, organization, google_directory):
        from_info, admin_client = google_directory
        account = _service_account(organization)
        before = timezone.now()

        result = GoogleWriteAccessService().verify(organization)

        account.refresh_from_db()
        assert account.write_enabled is True
        assert account.write_verified_at is not None
        assert account.write_verified_at >= before
        assert result == GoogleWriteAccessResult(
            write_enabled=True, write_verified_at=account.write_verified_at, error=""
        )
        assert from_info.call_args.kwargs["scopes"] == _SA_WRITE_SCOPES
        from_info.return_value.with_subject.assert_called_once_with("admin@example.com")
        _buildings_list(admin_client).assert_called_once_with(customer="my_customer", maxResults=1)

    @pytest.mark.parametrize(
        "failure",
        [
            RefreshError("unauthorized_client: Client is unauthorized to retrieve access tokens"),
            _http_error(403),
        ],
    )
    def test_missing_scope_disables_writes_and_explains_the_fix(
        self, organization, google_directory, failure
    ):
        _, admin_client = google_directory
        verified_at = timezone.now() - datetime.timedelta(days=1)
        account = _service_account(organization, write_enabled=True, write_verified_at=verified_at)
        _buildings_list(admin_client).return_value.execute.side_effect = failure

        result = GoogleWriteAccessService().verify(organization)

        account.refresh_from_db()
        assert account.write_enabled is False
        assert account.write_verified_at == verified_at
        assert result == GoogleWriteAccessResult(
            write_enabled=False,
            write_verified_at=verified_at,
            error=GoogleWriteAccessService.REMEDIATION_MESSAGE,
        )
        assert "admin.directory.resource.calendar" in result.error

    def test_transient_failure_changes_nothing(self, organization, google_directory):
        _, admin_client = google_directory
        verified_at = timezone.now() - datetime.timedelta(days=1)
        account = _service_account(organization, write_enabled=True, write_verified_at=verified_at)
        _buildings_list(admin_client).return_value.execute.side_effect = _http_error(503)

        result = GoogleWriteAccessService().verify(organization)

        account.refresh_from_db()
        assert account.write_enabled is True
        assert account.write_verified_at == verified_at
        assert result == GoogleWriteAccessResult(
            write_enabled=True,
            write_verified_at=verified_at,
            error=GoogleWriteAccessService.UNAVAILABLE_MESSAGE,
        )

    def test_unloadable_key_disables_writes(self, organization, google_directory):
        from_info, admin_client = google_directory
        account = _service_account(organization, write_enabled=True)
        from_info.side_effect = ValueError("Could not deserialize key data.")

        result = GoogleWriteAccessService().verify(organization)

        account.refresh_from_db()
        assert account.write_enabled is False
        assert result.error == GoogleWriteAccessService.INVALID_KEY_MESSAGE
        _buildings_list(admin_client).assert_not_called()

    def test_without_an_org_level_service_account(self, organization, google_directory):
        from_info, _ = google_directory
        calendar = baker.make(Calendar, organization=organization)
        calendar_account = _service_account(organization, calendar=calendar)

        result = GoogleWriteAccessService().verify(organization)

        assert result == GoogleWriteAccessResult(
            write_enabled=False,
            write_verified_at=None,
            error=GoogleWriteAccessService.NO_SERVICE_ACCOUNT_MESSAGE,
        )
        calendar_account.refresh_from_db()
        assert calendar_account.write_enabled is False
        from_info.assert_not_called()

    def test_only_the_organizations_own_account_is_used(self, organization, google_directory):
        other = _service_account(baker.make(Organization))

        result = GoogleWriteAccessService().verify(organization)

        assert result.error == GoogleWriteAccessService.NO_SERVICE_ACCOUNT_MESSAGE
        other.refresh_from_db()
        assert other.write_enabled is False
