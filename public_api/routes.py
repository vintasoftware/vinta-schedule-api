from common.types import RouteDict
from public_api.views import SystemUserTokenViewSet


routes: list[RouteDict] = [
    {
        "regex": r"public-api-tokens",
        "viewset": SystemUserTokenViewSet,
        "basename": "PublicAPITokens",
    },
]
