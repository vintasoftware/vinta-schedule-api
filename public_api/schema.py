import strawberry
from strawberry.extensions import (
    MaxTokensLimiter,
    QueryDepthLimiter,
)
from strawberry_django.optimizer import DjangoOptimizerExtension

from public_api.extensions import OrganizationRateLimiter
from public_api.mutations import Mutation
from public_api.queries import Query


schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=[
        DjangoOptimizerExtension,
        MaxTokensLimiter(max_token_count=1000),
        QueryDepthLimiter(max_depth=10),
        OrganizationRateLimiter(),
    ],
)
