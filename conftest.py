import pytest
from rest_framework.test import APIClient


@pytest.fixture
def user_password():
    from users.factories import DEFAULT_TEST_USER_PASSWORD

    return DEFAULT_TEST_USER_PASSWORD


@pytest.fixture
def user(user_password):
    from users.factories import UserFactory

    return UserFactory().create_user()


@pytest.fixture
def auth_client(user, user_password):
    client = APIClient()
    client.login(username=user.username, password=user_password)
    return client


@pytest.fixture
def anonymous_client():
    client = APIClient()
    return client


@pytest.fixture
def di_container():
    """Fixture to create a DI container."""
    from di_core.containers import container

    return container
