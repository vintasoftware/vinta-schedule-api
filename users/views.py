from django_virtual_models.generic_views import GenericVirtualModelViewMixin
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ViewSet

from users.serializers import ProfileSerializer

from .models import Profile
from .permissions import ProfileReadOnlyExceptYourOwn


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
