import strawberry
import strawberry_django

from organizations.models import Organization


@strawberry_django.type(Organization)
class OrganizationGraphQLType:
    name: strawberry.auto
    should_sync_rooms: strawberry.auto
