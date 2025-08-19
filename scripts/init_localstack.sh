#!/bin/bash
# Script to initialize LocalStack S3 bucket

echo "Waiting for LocalStack to be ready..."
sleep 5

# Set AWS credentials for LocalStack
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1

echo "Creating S3 bucket..."
aws --endpoint-url=http://localhost:4566 s3 mb s3://vinta_schedule --region us-east-1

echo "Setting CORS configuration..."
aws --endpoint-url=http://localhost:4566 s3api put-bucket-cors \
  --bucket vinta_schedule \
  --cors-configuration file://scripts/cors-config.json

echo "LocalStack S3 setup complete!"
