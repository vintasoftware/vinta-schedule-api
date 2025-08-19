from rest_framework import serializers

from common.utils.serializer_utils import VirtualModelSerializer

from .models import Profile, User
from .virtual_models import ProfileVirtualModel, UserVirtualModel


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
            "username",
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
