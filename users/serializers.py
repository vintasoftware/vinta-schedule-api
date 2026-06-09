from rest_framework import serializers

from common.utils.serializer_utils import VirtualModelSerializer

from .models import Profile, User
from .virtual_models import ProfileVirtualModel, UserVirtualModel


class ProfilePictureUploadParamsRequestSerializer(serializers.Serializer):
    file_name = serializers.CharField()
    file_type = serializers.CharField()
    file_size = serializers.IntegerField(min_value=1)


class ProfilePictureUploadParamsSerializer(serializers.Serializer):
    object_key = serializers.CharField()
    access_key_id = serializers.CharField(allow_null=True)
    session_token = serializers.CharField(allow_null=True)
    region = serializers.CharField()
    bucket = serializers.CharField()
    endpoint = serializers.CharField()
    acl = serializers.CharField()
    allow_existence_optimization = serializers.BooleanField()


class ProfileBasicSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="user_id", read_only=True)  # noqa: A003

    class Meta:
        model = Profile
        fields = ("id", "first_name", "last_name", "profile_picture")


class ProfileSerializer(VirtualModelSerializer):
    id = serializers.IntegerField(source="user_id", read_only=True)  # noqa: A003

    class Meta:  # type: ignore
        model = Profile
        virtual_model = ProfileVirtualModel
        fields = (
            "id",
            "first_name",
            "last_name",
            "profile_picture",
        )


class UserSerializer(VirtualModelSerializer):
    profile = ProfileSerializer()

    class Meta:  # type: ignore
        model = User
        virtual_model = UserVirtualModel
        fields = [  # noqa: RUF012
            "id",
            "email",
            "phone_number",
            "profile",
            "is_active",
            "is_staff",
            "is_superuser",
            "created",
            "modified",
            "last_login",
        ]
        read_only_fields = (
            "id",
            "email",
            "phone_number",
            "is_active",
            "is_staff",
            "is_superuser",
            "created",
            "modified",
            "last_login",
        )
