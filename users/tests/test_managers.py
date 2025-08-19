import pytest

from users.models import User


@pytest.mark.django_db
def test_create_user():
    """Test creating a regular user"""
    user = User.objects.create_user(email="test@example.com", password="testpassword123")
    assert user.email == "test@example.com"
    assert user.check_password("testpassword123")
    assert not user.is_superuser
    assert not user.is_staff
    assert user.is_active


@pytest.mark.django_db
def test_create_user_with_additional_fields():
    """Test creating a user with additional fields"""
    user = User.objects.create_user(
        email="test@example.com", password="testpassword123", phone_number="1234567890"
    )
    assert user.email == "test@example.com"
    assert user.phone_number == "1234567890"
    assert not user.is_superuser


@pytest.mark.django_db
def test_create_superuser():
    """Test creating a superuser"""
    admin_user = User.objects.create_superuser(
        email="admin@example.com", password="adminpassword123"
    )
    assert admin_user.email == "admin@example.com"
    assert admin_user.check_password("adminpassword123")
    assert admin_user.is_superuser
    assert admin_user.is_staff
    assert admin_user.is_active


@pytest.mark.django_db
def test_email_normalization():
    """Test email normalization during user creation"""
    user = User.objects.create_user(email="Test@EXAMPLE.com", password="testpassword123")
    # BaseUserManager.normalize_email() typically only normalizes the domain part
    assert user.email == "Test@example.com"
