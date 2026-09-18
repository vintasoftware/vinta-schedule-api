import strawberry
from strawberry.extensions import (
    MaxTokensLimiter,
    QueryDepthLimiter,
)
from strawberry_django.optimizer import DjangoOptimizerExtension

from public_api.aggregations.nested import NestedAggregateExtension
from public_api.extensions import OrganizationRateLimiter
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
        # Listed last on purpose. graphql-core chains resolve middleware so that
        # the last extension is the outermost, and this one must see a queryset
        # `DjangoOptimizerExtension` has already hinted and fetched -- ahead of
        # it, recording a parent list would evaluate the queryset first and
        # leave the optimizer nothing to optimize. See
        # `public_api/aggregations/nested.py`.
        NestedAggregateExtension,
    ],
)
