from rest_framework import permissions
from .models import Household, HouseholdMembership


class IsHouseholdMember(permissions.BasePermission):
    """
    Permission check to verify if the requesting user belongs to the household.
    """
    message = "You do not have permission to access this household."

    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False

        if isinstance(obj, Household):
            return obj.is_member(request.user)
        # If obj has a `household` attribute (e.g. Bill)
        if hasattr(obj, "household"):
            return obj.household.is_member(request.user)
        return False


class IsHouseholdOwner(permissions.BasePermission):
    """
    Permission check to verify if the requesting user is an OWNER of the household.
    Required for administrative actions like adding members or renaming the household.
    """
    message = "Only household owners can perform this action."

    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False

        if isinstance(obj, Household):
            return obj.is_owner(request.user)
        if hasattr(obj, "household"):
            return obj.household.is_owner(request.user)
        return False
