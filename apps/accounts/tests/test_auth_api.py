import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

User = get_user_model()


@pytest.mark.django_db
class TestAuthenticationAPI:
    def setup_method(self):
        self.client = APIClient()

    def test_registration_successful(self):
        """Valid registration creates user and returns 201."""
        payload = {
            "username": "alice",
            "email": "alice@example.com",
            "password": "StrongPassword123!",
            "password_confirm": "StrongPassword123!",
            "first_name": "Alice",
            "last_name": "Smith",
        }
        response = self.client.post("/api/auth/register/", payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["user"]["username"] == "alice"
        assert response.data["user"]["email"] == "alice@example.com"
        assert User.objects.filter(username="alice").exists()

    def test_registration_password_mismatch(self):
        """Mismatched passwords must fail registration."""
        payload = {
            "username": "bob",
            "email": "bob@example.com",
            "password": "Password123!",
            "password_confirm": "DifferentPassword123!",
        }
        response = self.client.post("/api/auth/register/", payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "password_confirm" in response.data

    def test_registration_short_password(self):
        """Passwords failing length/complexity validators must fail."""
        payload = {
            "username": "charlie",
            "email": "charlie@example.com",
            "password": "123",
            "password_confirm": "123",
        }
        response = self.client.post("/api/auth/register/", payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "password" in response.data

    def test_login_with_username_success(self):
        """User can log in using their username."""
        User.objects.create_user(username="david", email="david@example.com", password="Password123!")
        response = self.client.post(
            "/api/auth/login/",
            {"username_or_email": "david", "password": "Password123!"},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["user"]["username"] == "david"

    def test_login_with_email_success(self):
        """User can log in using their email address."""
        User.objects.create_user(username="emma", email="emma@example.com", password="Password123!")
        response = self.client.post(
            "/api/auth/login/",
            {"username_or_email": "emma@example.com", "password": "Password123!"},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["user"]["email"] == "emma@example.com"

    def test_login_invalid_password(self):
        """Login fails with invalid password."""
        User.objects.create_user(username="frank", email="frank@example.com", password="Password123!")
        response = self.client.post(
            "/api/auth/login/",
            {"username_or_email": "frank", "password": "WrongPassword!"},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_current_user_me_authenticated(self):
        """Authenticated user can fetch their profile."""
        user = User.objects.create_user(username="grace", email="grace@example.com", password="Password123!")
        self.client.force_authenticate(user=user)
        response = self.client.get("/api/auth/me/")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["username"] == "grace"

    def test_current_user_me_unauthenticated(self):
        """Unauthenticated request to /api/auth/me/ is rejected."""
        response = self.client.get("/api/auth/me/")
        assert response.status_code in [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN]

    def test_logout(self):
        """User can log out cleanly."""
        user = User.objects.create_user(username="helen", email="helen@example.com", password="Password123!")
        self.client.force_authenticate(user=user)
        response = self.client.post("/api/auth/logout/")
        assert response.status_code == status.HTTP_200_OK
