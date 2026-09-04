import pytest

from common.celery_sqs import build_sqs_transport_options, is_sqs_broker


@pytest.mark.parametrize(
    ("broker_url", "expected"),
    [
        # Deployed: no credentials, so kombu falls through to the ECS task role.
        ("sqs://", True),
        # Local: Floci, reached at the host and port kombu reads off this URL.
        ("sqs://test:test@floci:4566", True),
        ("amqp://localhost:5672//", False),
        ("redis://localhost:6379", False),
        ("", False),
    ],
)
def test_is_sqs_broker_recognises_only_the_sqs_scheme(broker_url, expected):
    assert is_sqs_broker(broker_url) is expected


def test_deployed_options_keep_tls_on_and_pin_the_queue():
    options = build_sqs_transport_options(
        region="us-east-1",
        queue_name="vinta-schedule-staging-celery",
        queue_url="https://sqs.us-east-1.amazonaws.com/1234/vinta-schedule-staging-celery",
        visibility_timeout=900,
    )

    assert options["is_secure"] is True
    assert options["region"] == "us-east-1"
    assert options["visibility_timeout"] == 900
    # Pinning the URL is what keeps the task role down to the message actions on
    # this one queue -- without it kombu calls ListQueues to find it.
    assert options["predefined_queues"] == {
        "vinta-schedule-staging-celery": {
            "url": "https://sqs.us-east-1.amazonaws.com/1234/vinta-schedule-staging-celery"
        }
    }


def test_local_options_turn_tls_off_for_floci():
    """Floci serves plain HTTP; left on, every broker call fails the handshake."""
    options = build_sqs_transport_options(
        region="us-east-1",
        queue_name="vinta-schedule-local-celery",
        queue_url="http://floci:4566/000000000000/vinta-schedule-local-celery",
        is_secure=False,
    )

    assert options["is_secure"] is False


def test_queue_stays_undeclared_when_no_url_is_known():
    """Without a URL kombu discovers (and creates) the queue itself, which needs
    broader SQS permissions -- allowed, but never the deployed configuration."""
    options = build_sqs_transport_options(region="us-east-1", queue_name="celery")

    assert "predefined_queues" not in options


def test_confirm_publish_is_not_carried_over():
    """The base settings set `confirm_publish` for Redis/RabbitMQ. SQS has no such
    concept, and these options replace that dict rather than extending it."""
    options = build_sqs_transport_options(region="us-east-1", queue_name="celery")

    assert "confirm_publish" not in options
    assert "confirm_timeout" not in options
