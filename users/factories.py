from collections.abc import Callable

from cuid2 import cuid_wrapper
from model_bakery import baker

from users.models import Profile, User


cuid_generator: Callable[[], str] = cuid_wrapper()


DEFAULT_TEST_USER_PASSWORD = "123456"  # noqa: S105


class UserFactory:
    def create_user(self, is_seed_data=False, **kwargs) -> User:
        try:
            return User.objects.get(email=kwargs.get("email", ""))
        except User.DoesNotExist:
            pass

        # Extract profile-related fields
        first_name = kwargs.pop("first_name", "")
        last_name = kwargs.pop("last_name", "")

        user = baker.prepare(User, email=kwargs.get("email", f"user{cuid_generator()}@example.com"))
        user.set_password(kwargs.get("password", DEFAULT_TEST_USER_PASSWORD))

        # Add seed data flag to meta field
        if is_seed_data:
            user.meta = {"is_seed_data": True}

        user.save()

        ProfileFactory().create_profile(
            user=user, is_seed_data=is_seed_data, first_name=first_name, last_name=last_name
        )
        return user


class ProfileFactory:
    def create_profile(self, user, is_seed_data=False, **kwargs) -> Profile:
        profile = baker.make(Profile, user=user, **kwargs)

        # Add seed data flag to meta field
        if is_seed_data:
            profile.meta = {"is_seed_data": True}
            profile.save()

        return profile
