from django.db import transaction
from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import Household, HouseholdMembership
from apps.accounts.serializers import UserSerializer
from apps.audit.models import AuditEvent
from apps.audit.services import AuditService

User = get_user_model()


class HouseholdMemberSerializer(serializers.ModelSerializer):
    """Serializer representing a user's membership details in a household."""
    user = UserSerializer(read_only=True)

    class Meta:
        model = HouseholdMembership
        fields = ("id", "user", "role", "joined_at")
        read_only_fields = ("id", "joined_at")


class HouseholdSerializer(serializers.ModelSerializer):
    """Detailed representation of a household."""
    created_by = UserSerializer(read_only=True)
    memberships = HouseholdMemberSerializer(many=True, read_only=True)
    member_count = serializers.IntegerField(read_only=True)
    current_user_role = serializers.SerializerMethodField()

    class Meta:
        model = Household
        fields = (
            "id",
            "name",
            "created_by",
            "member_count",
            "current_user_role",
            "memberships",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_by", "created_at", "updated_at")

    def get_current_user_role(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        membership = obj.memberships.filter(user=request.user).first()
        return membership.role if membership else None


class HouseholdCreateSerializer(serializers.ModelSerializer):
    """
    Serializer used specifically for creating a new household.
    Automatically establishes the creator as the initial OWNER.
    """
    class Meta:
        model = Household
        fields = ("id", "name", "created_at")
        read_only_fields = ("id", "created_at")

    def create(self, validated_data):
        request = self.context.get("request")
        user = request.user

        with transaction.atomic():
            household = Household.objects.create(
                name=validated_data["name"],
                created_by=user,
            )
            HouseholdMembership.objects.create(
                household=household,
                user=user,
                role=HouseholdMembership.Role.ROLE_OWNER if hasattr(HouseholdMembership.Role, "ROLE_OWNER") else HouseholdMembership.Role.OWNER,
            )
            AuditService.log(
                action=AuditEvent.Action.HOUSEHOLD_CREATED,
                actor=user,
                household=household,
                entity=household,
                description=f"Household '{household.name}' created by {user.display_name}.",
            )
        return household


class AddMemberSerializer(serializers.Serializer):
    """
    Serializer for adding an existing registered user to a household.
    Accepts either email or username.
    """
    username_or_email = serializers.CharField(
        required=True,
        help_text="Username or email of the registered user to add."
    )
    role = serializers.ChoiceField(
        choices=HouseholdMembership.Role.choices,
        default=HouseholdMembership.Role.MEMBER,
        required=False,
    )

    def validate_username_or_email(self, value):
        user = None
        if "@" in value:
            user = User.objects.filter(email__iexact=value).first()
        else:
            user = User.objects.filter(username__iexact=value).first()

        if not user:
            raise serializers.ValidationError("No registered user found with this email or username.")

        self.context["target_user"] = user
        return value

    def validate(self, attrs):
        household = self.context.get("household")
        target_user = self.context.get("target_user")

        if household.memberships.filter(user=target_user).exists():
            raise serializers.ValidationError(
                {"username_or_email": f"{target_user.username} is already a member of this household."}
            )

        return attrs

    def save(self):
        household = self.context["household"]
        target_user = self.context["target_user"]
        role = self.validated_data.get("role", HouseholdMembership.Role.MEMBER)

        membership = HouseholdMembership.objects.create(
            household=household,
            user=target_user,
            role=role,
        )
        return membership
