import pytest


@pytest.fixture
def profile_data():
    return {
        "first_name": "Updated",
        "last_name": "Name",
    }
