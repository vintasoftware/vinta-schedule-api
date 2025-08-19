from common.types import RouteDict

from .views import ProfileViewSet


routes: list[RouteDict] = [
    {"regex": r"profile", "viewset": ProfileViewSet, "basename": "Profile"},
]
