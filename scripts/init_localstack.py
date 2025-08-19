#!/usr/bin/env python
"""
Script to initialize LocalStack S3 bucket for development.
This script should be run after LocalStack is up and running.
"""

import os

import boto3
from botocore.exceptions import ClientError


def init_s3_bucket():
    """Initialize S3 bucket in LocalStack"""

    # LocalStack S3 configuration
    endpoint_url = os.getenv("LOCALSTACK_ENDPOINT", "http://localhost:4566")
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


if __name__ == "__main__":
    init_s3_bucket()
