from typing import TypedDict

from rest_framework.viewsets import GenericViewSet, ModelViewSet, ViewSet, ViewSetMixin


class RouteDict(TypedDict):
    """
    A dictionary representing a route with a name and a list of points.
    """

    regex: str
    viewset: type[GenericViewSet] | type[ViewSet] | type[ModelViewSet] | type[ViewSetMixin]
    basename: str
