from common.types import RouteDict
from public_api.views import PublicApiDocsViewSet, SystemUserTokenViewSet


routes: list[RouteDict] = [
    {
        "regex": r"public-api-tokens",
        "viewset": SystemUserTokenViewSet,
        "basename": "PublicAPITokens",
    },
    {
        "regex": r"public-api-docs",
        "viewset": PublicApiDocsViewSet,
        "basename": "PublicAPIDocs",
    },
]
