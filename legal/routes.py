from common.types import RouteDict

from .views import PolicyDocumentViewSet


routes: list[RouteDict] = [
    {
        "regex": r"policy-documents",
        "viewset": PolicyDocumentViewSet,
        "basename": "PolicyDocuments",
    },
]
