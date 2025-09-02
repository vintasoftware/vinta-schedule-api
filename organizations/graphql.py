import strawberry
import strawberry_django

from organizations.models import Organization, OrganizationTier


@strawberry_django.type(OrganizationTier)
class OrganizationTierGraphQLType:
    name: strawberry.auto


@strawberry_django.type(Organization)
class OrganizationGraphQLType:
    name: strawberry.auto
    tier: OrganizationTierGraphQLType = strawberry_django.field()
    should_sync_rooms: strawberry.auto
