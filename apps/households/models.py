from django.db import models
from django.conf import settings


class Household(models.Model):
    """
    A Household represents a single family unit or cohabiting group.
    Acts as the primary multi-tenant isolation boundary for bills.
    """
    name = models.CharField(max_length=150, help_text="e.g., The Sharma Family, Apartment 4B")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_households",
        help_text="User who initially founded the household."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "homepilot_households"
        verbose_name = "Household"
        verbose_name_plural = "Households"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def is_member(self, user):
        """Check if a given user is a member of this household."""
        if not user or not user.is_authenticated:
            return False
        return self.memberships.filter(user=user).exists()

    def is_owner(self, user):
        """Check if a given user has the OWNER role in this household."""
        if not user or not user.is_authenticated:
            return False
        return self.memberships.filter(
            user=user,
            role=HouseholdMembership.Role.OWNER
        ).exists()

    @property
    def member_count(self):
        return self.memberships.count()


class HouseholdMembership(models.Model):
    """
    Explicit through model linking Users to Households.
    Enables storing user roles and joined timestamps per household.
    """
    class Role(models.TextChoices):
        OWNER = "OWNER", "Owner"
        MEMBER = "MEMBER", "Member"

    household = models.ForeignKey(
        Household,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="household_memberships",
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.MEMBER,
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_household_memberships"
        verbose_name = "Household Membership"
        verbose_name_plural = "Household Memberships"
        unique_together = ("household", "user")
        ordering = ["joined_at"]

    def __str__(self):
        return f"{self.user} - {self.household.name} ({self.role})"
