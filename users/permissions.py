from typing import TYPE_CHECKING

from rest_framework.permissions import SAFE_METHODS, BasePermission


if TYPE_CHECKING:
    from users.models import Profile


class ProfileReadOnlyExceptYourOwn(BasePermission):
    def has_object_permission(self, request, view, obj: "Profile"):
        return request.method in SAFE_METHODS or (
            request.user.is_authenticated and request.user.pk == obj.pk
        )
