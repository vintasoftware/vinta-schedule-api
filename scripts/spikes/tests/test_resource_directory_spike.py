"""Tests for the resource-directory spike CLI: parsing, and that a dry run sends nothing."""

import base64
import json
import re
import socket
import urllib.request
from typing import Any, ClassVar

import pytest

from scripts.spikes import resource_directory_spike as spike


SUBCOMMANDS = ("google-create-delete", "ms-create-delete", "ms-description")

CREDENTIAL_ENV_VARS = (
    spike.ENV_GOOGLE_SA_JSON,
    spike.ENV_GOOGLE_ADMIN_EMAIL,
    spike.ENV_GOOGLE_CUSTOMER,
    spike.ENV_MS_TENANT_ID,
    spike.ENV_MS_CLIENT_ID,
    spike.ENV_MS_CLIENT_SECRET,
    spike.ENV_MS_FLOOR_ID,
)


class NetworkCalledError(AssertionError):
    pass


def _fail(*args: Any, **kwargs: Any) -> None:
    raise NetworkCalledError("the spike tried to reach the network")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every way the script could send a request fails the test."""
    from google.auth.transport import requests as google_requests

    monkeypatch.setattr(urllib.request, "urlopen", _fail)
    monkeypatch.setattr(spike.UrllibTransport, "send", _fail)
    monkeypatch.setattr(google_requests.Request, "__call__", _fail)
    monkeypatch.setattr(socket, "create_connection", _fail)
    monkeypatch.setattr(socket.socket, "connect", _fail)


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class RecordingDryRunTransport(spike.DryRunTransport):
    instances: ClassVar[list["RecordingDryRunTransport"]] = []

    def __init__(self) -> None:
        super().__init__()
        RecordingDryRunTransport.instances.append(self)


@pytest.fixture
def recorded_calls(monkeypatch: pytest.MonkeyPatch) -> list[RecordingDryRunTransport]:
    RecordingDryRunTransport.instances = []
    monkeypatch.setattr(spike, "DryRunTransport", RecordingDryRunTransport)
    return RecordingDryRunTransport.instances


def test_help_lists_the_three_subcommands(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exc_info:
        spike.build_parser().parse_args(["--help"])

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for name in SUBCOMMANDS:
        assert name in out


def test_a_subcommand_is_required(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exc_info:
        spike.build_parser().parse_args([])

    assert exc_info.value.code == 2
    assert "SUBCOMMAND" in capsys.readouterr().err


@pytest.mark.parametrize("command", SUBCOMMANDS)
def test_execute_defaults_to_off(command: str):
    args = spike.build_parser().parse_args([command])

    assert args.command == command
    assert args.execute is False


def test_google_arguments_parse():
    args = spike.build_parser().parse_args(
        ["google-create-delete", "--building-id", "b-1", "--execute"]
    )

    assert (args.command, args.building_id, args.execute) == ("google-create-delete", "b-1", True)


def test_microsoft_arguments_parse_with_defaults():
    args = spike.build_parser().parse_args(["ms-create-delete", "--floor-id", "floor-1"])

    assert vars(args) == {
        "verbose": False,
        "command": "ms-create-delete",
        "floor_id": "floor-1",
        "places_base": "https://graph.microsoft.com/beta",
        "poll_interval": 10.0,
        "provision_timeout": 600.0,
        "post_delete_wait": 60.0,
        "execute": False,
    }


@pytest.mark.parametrize("command", SUBCOMMANDS)
def test_dry_run_makes_no_http_calls(
    command: str,
    no_network: None,
    no_credentials: None,
    capsys: pytest.CaptureFixture[str],
):
    exit_code = spike.main([command])

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["command"] == command
    assert summary["dry_run"] is True


def test_google_dry_run_walks_every_step(
    no_network: None, no_credentials: None, recorded_calls: list[RecordingDryRunTransport]
):
    spike.main(["google-create-delete"])

    [transport] = recorded_calls
    methods = [method for method, _ in transport.calls]
    assert methods == [
        "GET",  # buildings.list
        "POST",  # insert without a building
        "POST",  # insert
        "POST",  # replayed insert
        "GET",  # read back
        "PATCH",  # capacity
        "GET",  # read back after patch
        "POST",  # duplicate name
        "DELETE",  # the room
        "GET",  # after delete
        "DELETE",  # cleanup: duplicate-name room
        "DELETE",  # cleanup: no-building room
    ]


def test_ms_create_delete_dry_run_walks_every_step(
    no_network: None, no_credentials: None, recorded_calls: list[RecordingDryRunTransport]
):
    spike.main(["ms-create-delete"])

    [transport] = recorded_calls
    methods = [method for method, _ in transport.calls]
    assert methods == [
        "POST",  # create
        "GET",  # poll for the email address
        "GET",  # tag filter lookup
        "GET",  # poll the mailbox calendar
        "POST",  # getSchedule
        "PATCH",  # capacity
        "GET",  # read back after patch
        "DELETE",  # the room
        "GET",  # place after delete
        "GET",  # mailbox user after delete
        "GET",  # mailbox calendar after delete
    ]


def test_ms_description_dry_run_probes_every_candidate(
    no_network: None, no_credentials: None, recorded_calls: list[RecordingDryRunTransport]
):
    spike.main(["ms-description"])

    [transport] = recorded_calls
    patched = [op for method, op in transport.calls if method == "PATCH"]
    assert patched == [
        f"microsoft.places.patch(dry-run-place-id, {field})"
        for field in spike.MS_DESCRIPTION_CANDIDATES
    ]
    assert transport.calls[-1] == ("DELETE", "microsoft.places.delete(dry-run-place-id)")


@pytest.mark.parametrize(
    "argv",
    [
        ["google-create-delete", "--execute"],
        ["ms-create-delete", "--execute"],
        ["ms-description", "--execute"],
    ],
)
def test_execute_without_credentials_stops_before_any_call(
    argv: list[str], no_network: None, no_credentials: None
):
    assert spike.main(argv) == 1


class ScriptedTransport:
    """Answers each request with a 2xx, except the operation told to raise."""

    def __init__(self, raise_on: str) -> None:
        self.raise_on = raise_on
        self.calls: list[tuple[str, str]] = []

    def send(
        self,
        operation: str,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, str] | None = None,
    ) -> spike.HttpResponse:
        self.calls.append((method, operation))
        if self.raise_on in operation:
            raise RuntimeError("provider blew up")
        body = dict(json_body or {})
        body.setdefault("id", "place-1")
        return spike.HttpResponse(status=200, body=body)


def test_google_room_is_deleted_when_a_later_step_fails(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(spike, "google_access_token", lambda *, dry_run: "token")
    transport = ScriptedTransport(raise_on="replay")
    args = spike.build_parser().parse_args(
        ["google-create-delete", "--building-id", "b-1", "--execute"]
    )

    with pytest.raises(RuntimeError, match="provider blew up"):
        spike.run(args, transport=transport)

    deletes = [op for method, op in transport.calls if method == "DELETE"]
    # Newest first: the room, then the no-building room created before it.
    assert len(deletes) == 2
    assert re.fullmatch(r"google\.delete\(vinta-spike-[0-9a-f]{12}\)", deletes[0])
    assert re.fullmatch(r"google\.delete\(vinta-spike-[0-9a-f]{12}-nobuilding\)", deletes[1])


def test_microsoft_room_is_deleted_when_a_later_step_fails(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(spike, "microsoft_access_token", lambda transport, *, dry_run: "token")
    transport = ScriptedTransport(raise_on="filter by tag")
    args = spike.build_parser().parse_args(
        ["ms-create-delete", "--floor-id", "floor-1", "--execute", "--provision-timeout", "0"]
    )

    with pytest.raises(RuntimeError, match="provider blew up"):
        spike.run(args, transport=transport)

    assert transport.calls[-1] == ("DELETE", "microsoft.places.delete(place-1)")


def test_microsoft_findings_hold_no_room_content(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(spike, "microsoft_access_token", lambda transport, *, dry_run: "token")
    transport = ScriptedTransport(raise_on="never-matches")

    def send_with_email(*args: Any, **kwargs: Any) -> spike.HttpResponse:
        response = ScriptedTransport.send(transport, *args, **kwargs)
        body = {**response.body, "emailAddress": "room-secret@tenant.example"}
        return spike.HttpResponse(status=response.status, body=body)

    monkeypatch.setattr(transport, "send", send_with_email)
    args = spike.build_parser().parse_args(
        [
            "ms-create-delete",
            "--floor-id",
            "floor-1",
            "--execute",
            "--provision-timeout",
            "0",
            "--post-delete-wait",
            "0",
        ]
    )

    summary = spike.run(args, transport=transport)

    serialized = json.dumps(summary)
    assert "room-secret" not in serialized
    assert "Vinta spike" not in serialized
    assert summary["findings"]["email_address_hash"] == spike.opaque("room-secret@tenant.example")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"error": {"code": 409, "errors": [{"reason": "duplicate", "message": "x"}]}},
            "duplicate",
        ),
        (
            {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "x"}},
            "INVALID_ARGUMENT",
        ),
        ({"error": {"code": "ErrorItemNotFound", "message": "x"}}, "ErrorItemNotFound"),
        ({"error": "nope"}, None),
        ({}, None),
    ],
)
def test_provider_error_code_never_returns_the_message(body: dict[str, Any], expected: str | None):
    assert spike.provider_error_code(spike.HttpResponse(status=400, body=body)) == expected


def test_jwt_roles_reads_the_roles_claim():
    claims = {"roles": ["Place.ReadWrite.All", "Calendars.Read"], "tid": "t"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = f"header.{payload}.signature"

    assert spike.jwt_roles(token) == ["Calendars.Read", "Place.ReadWrite.All"]


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.!!!.c"])
def test_jwt_roles_tolerates_garbage(token: str):
    assert spike.jwt_roles(token) == []


def test_urllib_transport_refuses_plain_http():
    with pytest.raises(spike.SpikeError, match="non-HTTPS"):
        spike.UrllibTransport().send("op", "GET", "http://example.invalid/")
