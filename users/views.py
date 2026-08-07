from django.conf import settings

import boto3
from botocore.config import Config as BotoConfig
from django_virtual_models.generic_views import GenericVirtualModelViewMixin
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet
from s3direct.utils import get_aws_credentials, get_key, get_s3direct_destinations

from users.serializers import (
    ProfilePictureUploadParamsRequestSerializer,
    ProfilePictureUploadParamsSerializer,
    ProfileSerializer,
)

from .models import Profile
from .permissions import ProfileReadOnlyExceptYourOwn


# Long enough for a slow mobile connection to finish a photo upload, short enough that a
# leaked URL is not a lasting write grant on the bucket.
PROFILE_PICTURE_UPLOAD_URL_EXPIRY_SECONDS = 900


class ProfileViewSet(GenericVirtualModelViewMixin, ViewSet, RetrieveUpdateAPIView):
    serializer_class = ProfileSerializer
    queryset = Profile.objects.all()
    lookup_url_kwarg = "pk"
    lookup_field = "pk"
    permission_classes = (
        IsAuthenticated,
        ProfileReadOnlyExceptYourOwn,
    )

    def get_object(self):
        """
        Returns the profile of the currently authenticated user.
        """
        if self.kwargs.get("pk") == "me":
            return self.get_queryset().filter(pk=self.request.user.pk).first()

        return super().get_object()

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "user",
                location="path",
                required=True,
                description="User ID to retrieve or update the profile. Use 'me' to refer to the currently authenticated user.",
                type={"type": "string"},
            )
        ],
    )
    def retrieve(self, request, *args, **kwargs):
        """
        Retrieve the profile of the currently authenticated user or a specific user.
        """
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "user",
                location="path",
                required=True,
                description="User ID to update the profile. Use 'me' to refer to the currently authenticated user.",
                type={"type": "string"},
            )
        ],
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "user",
                location="path",
                required=True,
                description="User ID to update the profile. Use 'me' to refer to the currently authenticated user.",
                type={"type": "string"},
            )
        ],
    )
    def partial_update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        summary="Get S3 upload params for profile picture",
        description=(
            "Returns the parameters needed to upload a profile picture directly to S3. "
            "Only works for your own profile."
        ),
        request=ProfilePictureUploadParamsRequestSerializer,
        responses={200: ProfilePictureUploadParamsSerializer},
        parameters=[
            OpenApiParameter(
                "user",
                location="path",
                required=True,
                description="User ID. Use 'me' to refer to the currently authenticated user.",
                type={"type": "string"},
            )
        ],
    )
    @action(detail=True, methods=["post"], url_path="profile-picture-upload-params")
    def profile_picture_upload_params(self, request, pk=None):
        self.get_object()  # enforces object-level permissions (own profile only)

        request_serializer = ProfilePictureUploadParamsRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        file_name = request_serializer.validated_data["file_name"]

        file_type = request_serializer.validated_data["file_type"]

        dest = get_s3direct_destinations().get("profile_pictures")

        bucket = dest.get("bucket", getattr(settings, "AWS_STORAGE_BUCKET_NAME", None))
        region = dest.get("region", getattr(settings, "AWS_S3_REGION_NAME", None))
        endpoint = dest.get("endpoint", getattr(settings, "AWS_S3_ENDPOINT_URL", None))
        aws_credentials = get_aws_credentials()

        if not bucket or not region or not endpoint:
            raise ValidationError("S3 configuration is incomplete.")

        object_key = get_key(dest["key"], file_name, dest)

        s3_client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint,
            aws_access_key_id=aws_credentials.access_key,
            aws_secret_access_key=aws_credentials.secret_key,
            aws_session_token=aws_credentials.token,
            config=BotoConfig(
                signature_version="s3v4",
                # Floci (local) only answers path-style; on AWS "auto" picks
                # virtual-hosted. Mirrors what django-storages reads for the same call.
                s3={"addressing_style": getattr(settings, "AWS_S3_ADDRESSING_STYLE", "auto")},
            ),
        )

        # No ACL in the signed params: the media bucket sets Object Ownership to
        # BucketOwnerEnforced, which rejects any upload carrying an x-amz-acl header.
        # Objects stay private through the bucket's public-access block, and are served
        # only via the signed-URL CloudFront distribution.
        upload_url = s3_client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": object_key, "ContentType": file_type},
            ExpiresIn=PROFILE_PICTURE_UPLOAD_URL_EXPIRY_SECONDS,
            HttpMethod="PUT",
        )

        upload_data = {
            "object_key": object_key,
            "upload_url": upload_url,
            "expires_in": PROFILE_PICTURE_UPLOAD_URL_EXPIRY_SECONDS,
        }

        serializer = ProfilePictureUploadParamsSerializer(upload_data)
        return Response(serializer.data, status=status.HTTP_200_OK)
