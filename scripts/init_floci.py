#!/usr/bin/env python
"""
Script to initialize the Floci resources development needs: the S3 bucket, and the
SQS queues Celery brokers over.
This script should be run after Floci is up and running.
"""

import json
import os
import time

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError


def wait_for_floci(s3_client, retries=30, delay=1):
    """Block until Floci is accepting connections."""
    for attempt in range(1, retries + 1):
        try:
            s3_client.list_buckets()
            return
        except EndpointConnectionError:
            print(f"Waiting for Floci to be ready... ({attempt}/{retries})")
            time.sleep(delay)
    raise RuntimeError("Floci did not become ready in time")


def floci_client(service):
    """A boto3 client pointed at Floci rather than AWS."""
    return boto3.client(
        service,
        endpoint_url=os.getenv("FLOCI_ENDPOINT", "http://localhost:4566"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
        region_name=os.getenv("AWS_S3_REGION_NAME", "us-east-1"),
        use_ssl=False,
    )


def init_s3_bucket():
    """Initialize S3 bucket in Floci"""

    # Floci S3 configuration
    endpoint_url = os.getenv("FLOCI_ENDPOINT", "http://localhost:4566")
    bucket_name = os.getenv("S3_BUCKET_NAME", "vinta_schedule")
    region = os.getenv("AWS_S3_REGION_NAME", "us-east-1")
    access_key = os.getenv("AWS_ACCESS_KEY_ID", "test")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "test")

    # Create S3 client
    s3_client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )

    wait_for_floci(s3_client)

    try:
        # Check if bucket already exists
        s3_client.head_bucket(Bucket=bucket_name)
        print(f"Bucket '{bucket_name}' already exists")
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "404":
            # Bucket doesn't exist, create it
            try:
                s3_client.create_bucket(Bucket=bucket_name)
                print(f"Created bucket '{bucket_name}' successfully")

                # Set CORS configuration for the bucket
                cors_configuration = {
                    "CORSRules": [
                        {
                            "AllowedHeaders": ["*"],
                            "AllowedMethods": ["GET", "PUT", "POST", "DELETE", "HEAD"],
                            "AllowedOrigins": ["*"],
                            "ExposeHeaders": [],
                            "MaxAgeSeconds": 3000,
                        }
                    ]
                }

                s3_client.put_bucket_cors(Bucket=bucket_name, CORSConfiguration=cors_configuration)
                print(f"Set CORS configuration for bucket '{bucket_name}'")

            except ClientError as create_error:
                print(f"Error creating bucket: {create_error}")
        else:
            print(f"Error checking bucket: {e}")


def _ensure_queue(sqs_client, name, attributes):
    """Create the queue, or bring an existing one up to these attributes.

    CreateQueue is only idempotent when the attributes match, so a changed
    visibility timeout has to go through SetQueueAttributes instead of failing the
    whole setup.
    """
    try:
        url = sqs_client.get_queue_url(QueueName=name)["QueueUrl"]
    except ClientError:
        url = sqs_client.create_queue(QueueName=name, Attributes=attributes)["QueueUrl"]
        print(f"Created queue '{name}'")
        return url

    sqs_client.set_queue_attributes(QueueUrl=url, Attributes=attributes)
    print(f"Queue '{name}' already exists; attributes refreshed")
    return url


def init_sqs_queues():
    """Create the Celery broker queue and its dead-letter queue in Floci.

    Mirrors what Terraform provisions for the deployed environments
    (infrastructure/modules/app-platform/queue.tf) -- same visibility timeout, same
    redrive policy -- so a task that misbehaves locally misbehaves the same way in
    staging. Floci enforces both, not just stores them.
    """
    queue_name = os.getenv("CELERY_TASK_DEFAULT_QUEUE", "vinta-schedule-local-celery")
    dlq_name = f"{queue_name}-dlq"
    # One value drives the queue and the app (settings/base.py reads the same var),
    # the way Terraform drives both sides from one input.
    visibility_timeout = os.getenv("CELERY_SQS_VISIBILITY_TIMEOUT", "900")
    max_receive_count = os.getenv("CELERY_SQS_MAX_RECEIVE_COUNT", "5")

    sqs_client = floci_client("sqs")

    dlq_url = _ensure_queue(sqs_client, dlq_name, {"MessageRetentionPeriod": "1209600"})
    dlq_arn = sqs_client.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]

    queue_url = _ensure_queue(
        sqs_client,
        queue_name,
        {
            "VisibilityTimeout": visibility_timeout,
            # Long polling, matching the deployed queue.
            "ReceiveMessageWaitTimeSeconds": "20",
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": int(max_receive_count)}
            ),
        },
    )

    # Floci reports the URL with the hostname it was configured with, which only
    # resolves inside the compose network. Print both so whichever one matches how
    # you run the app can go into CELERY_SQS_QUEUE_URL -- the same in-container vs
    # on-host split FLOCI_ENDPOINT / FLOCI_EXTERNAL_ENDPOINT already handles for S3.
    print(f"  CELERY_SQS_QUEUE_URL (in container): {queue_url}")
    external = os.getenv("FLOCI_EXTERNAL_ENDPOINT", "http://localhost:4566")
    print(f"  CELERY_SQS_QUEUE_URL (on host):      {external}/000000000000/{queue_name}")


if __name__ == "__main__":
    init_s3_bucket()
    init_sqs_queues()
