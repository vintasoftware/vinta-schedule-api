import strawberry
from strawberry.extensions import (
    MaxTokensLimiter,
    QueryDepthLimiter,
)
from strawberry_django.optimizer import DjangoOptimizerExtension

from public_api.extensions import OnBehalfOfMemberExtension, OrganizationRateLimiter
from public_api.mutations import Mutation
from public_api.queries import Query


schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=[
        DjangoOptimizerExtension,
        lambda: MaxTokensLimiter(max_token_count=1000),
        lambda: QueryDepthLimiter(max_depth=10),
        OrganizationRateLimiter,
        # After the rate limiter, so a rate-limited request is turned away
        # before it costs a membership query.
        OnBehalfOfMemberExtension,
    ],
)
