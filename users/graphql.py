import datetime

import strawberry
import strawberry_django

from users.models import Profile, User


@strawberry_django.type(Profile)
class ProfileGraphQLType:
    first_name: strawberry.auto
    last_name: strawberry.auto
    profile_picture: str | None
    created: datetime.datetime
    modified: datetime.datetime


@strawberry_django.type(User)
class UserGraphQLType:
    id: strawberry.auto  # noqa: A003
    email: strawberry.auto
    phone_number: strawberry.auto
    is_active: strawberry.auto
    is_staff: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime
    profile: ProfileGraphQLType = strawberry_django.field()
