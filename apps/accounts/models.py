from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Custom User model for HomePilot.
    Inherits from AbstractUser to preserve full compatibility with
    Django's built-in auth, admin, and permissions system.
    """
    email = models.EmailField(
        unique=True,
        help_text="Required. Must be a valid and unique email address."
    )
    phone_number = models.CharField(
        max_length=20,
        blank=True,
        help_text="Optional contact number for reminders."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "homepilot_users"
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["-date_joined"]

    def __str__(self):
        return self.get_full_name() or self.username or self.email

    @property
    def display_name(self):
        """Returns the user's full name if available, otherwise username."""
        full_name = self.get_full_name().strip()
        return full_name if full_name else self.username
