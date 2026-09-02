"""Celery broker options for Amazon SQS.

Lives outside the settings modules because the same options are built three ways
and the differences are easy to get subtly wrong:

* **deployed** -- ``sqs://`` with no credentials, so kombu falls through to
  boto3's default chain and resolves the ECS task role;
* **local** -- ``sqs://test:test@floci:4566``, where Floci emulates SQS over
  plain HTTP, so TLS has to be turned off;
* **neither** -- RabbitMQ or Redis, where none of this applies.

Kombu derives the boto3 endpoint from the broker URL's host and port, which is
what points it at Floci without any client wiring of our own. See
``kombu.transport.SQS.Channel.endpoint_url``.
"""

from typing import Any


SQS_BROKER_SCHEME = "sqs://"


def is_sqs_broker(broker_url: str) -> bool:
    """Whether this broker URL selects kombu's SQS transport."""
    return broker_url.startswith(SQS_BROKER_SCHEME)


def build_sqs_transport_options(
    *,
    region: str,
    queue_name: str,
    queue_url: str = "",
    visibility_timeout: int = 900,
    polling_interval: float = 1.0,
    wait_time_seconds: int = 20,
    is_secure: bool = True,
) -> dict[str, Any]:
    """Build ``CELERY_BROKER_TRANSPORT_OPTIONS`` for the SQS transport.

    Replaces the base settings' ``confirm_publish`` pair, which are
    Redis/RabbitMQ publisher options that mean nothing here.

    ``visibility_timeout`` has to match the queue's own setting.
    ``CELERY_TASK_ACKS_LATE`` is on, so a message is deleted only once its task
    finishes -- if the timeout expires first, SQS hands the same task to a second
    worker and it runs twice.

    ``queue_url`` is optional but worth setting: naming the queue up front is what
    lets the task role carry only the message actions on that one queue, because
    without it kombu calls ``ListQueues`` and ``CreateQueue`` to discover the URL
    itself. The flip side is that a queue which does not exist yet raises
    ``UndefinedQueueException`` instead of being created on demand -- locally that
    means ``make setup`` (``scripts/init_floci.py``) has to have run.
    """
    options: dict[str, Any] = {
        "region": region,
        "visibility_timeout": visibility_timeout,
        "polling_interval": polling_interval,
        # Long polling: an idle worker waits for work inside one API call instead
        # of billing a request per second to be told there is none.
        "wait_time_seconds": wait_time_seconds,
        # Floci speaks plain HTTP. Against real SQS this stays True.
        "is_secure": is_secure,
    }

    if queue_url:
        options["predefined_queues"] = {queue_name: {"url": queue_url}}

    return options
