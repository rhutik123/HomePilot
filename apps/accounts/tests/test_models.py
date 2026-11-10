import pytest
from django.contrib.auth import get_user_model
from django.db.utils import IntegrityError

User = get_user_model()


@pytest.mark.django_db
class TestUserModel:
    def test_create_user_successful(self):
        """Verify normal user creation with email, username, and password."""
        user = User.objects.create_user(
            username="johndoe",
            email="john@example.com",
            password="SecurePassword123!",
            first_name="John",
            last_name="Doe",
        )
        assert user.id is not None
        assert user.email == "john@example.com"
        assert user.check_password("SecurePassword123!")
        assert user.is_active is True
        assert user.is_staff is False
        assert user.is_superuser is False
        assert str(user) == "John Doe"
        assert user.display_name == "John Doe"

    def test_display_name_fallback_to_username(self):
        """Verify display_name falls back to username when first/last names are absent."""
        user = User.objects.create_user(
            username="janedoe",
            email="jane@example.com",
            password="SecurePassword123!",
        )
        assert user.display_name == "janedoe"
        assert str(user) == "janedoe"

    def test_unique_email_enforcement(self):
        """Verify duplicate emails raise an IntegrityError."""
        User.objects.create_user(
            username="user1",
            email="same@example.com",
            password="Password123!",
        )
        with pytest.raises(IntegrityError):
            User.objects.create_user(
                username="user2",
                email="same@example.com",
                password="Password123!",
            )

    def test_create_superuser(self):
        """Verify superuser creation with admin privileges."""
        admin = User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="AdminPassword123!",
        )
        assert admin.is_staff is True
        assert admin.is_superuser is True
