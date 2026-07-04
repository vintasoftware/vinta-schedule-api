from common.types import RouteDict

from .views import ConsentViewSet, PolicyDocumentViewSet


routes: list[RouteDict] = [
    {
        "regex": r"policy-documents",
        "viewset": PolicyDocumentViewSet,
        "basename": "PolicyDocuments",
    },
    {
        "regex": r"consents",
        "viewset": ConsentViewSet,
        "basename": "Consents",
    },
]
