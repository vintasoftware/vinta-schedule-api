from common.types import RouteDict

from .views import NotificationViewSet


routes: list[RouteDict] = [
    {
        "regex": r"notifications",
        "viewset": NotificationViewSet,
        "basename": "Notifications",
    },
]
