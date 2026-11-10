from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import Household, HouseholdMembership
from .permissions import IsHouseholdMember, IsHouseholdOwner
from .serializers import (
    HouseholdSerializer,
    HouseholdCreateSerializer,
    HouseholdMemberSerializer,
    AddMemberSerializer,
)
from apps.audit.models import AuditEvent
from apps.audit.services import AuditService


class HouseholdViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing households.
    Enforces strict multi-tenant isolation: users can ONLY view/manipulate
    households to which they belong.
    """
    permission_classes = [IsAuthenticated, IsHouseholdMember]

    def get_queryset(self):
        # Strict isolation: Only return households where the current user has active membership
        return (
            Household.objects.filter(memberships__user=self.request.user)
            .select_related("created_by")
            .prefetch_related("memberships__user")
            .distinct()
        )

    def get_serializer_class(self):
        if self.action == "create":
            return HouseholdCreateSerializer
        return HouseholdSerializer

    def get_permissions(self):
        """
        - Creating a household requires being an authenticated user.
        - Managing household and members (edit, delete, add/remove member) requires being an OWNER.
        - Retrieving/Listing requires being a MEMBER.
        """
        if self.action == "create":
            return [IsAuthenticated()]
        if self.action in ["update", "partial_update", "destroy", "add_member", "remove_member"]:
            return [IsAuthenticated(), IsHouseholdOwner()]
        return [IsAuthenticated(), IsHouseholdMember()]

    @action(detail=True, methods=["get"], permission_classes=[IsAuthenticated, IsHouseholdMember])
    def members(self, request, pk=None):
        """List all members of this household with their roles."""
        household = self.get_object()
        memberships = household.memberships.select_related("user").all()
        serializer = HouseholdMemberSerializer(memberships, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsHouseholdOwner])
    def add_member(self, request, pk=None):
        """
        Add an existing registered user to this household.
        Restricted strictly to household owners.
        """
        household = self.get_object()
        serializer = AddMemberSerializer(
            data=request.data,
            context={"household": household, "request": request},
        )
        if serializer.is_valid():
            membership = serializer.save()
            AuditService.log(
                action=AuditEvent.Action.MEMBER_ADDED,
                actor=request.user,
                household=household,
                entity=membership,
                description=f"Added {membership.user.display_name} to {household.name} as {membership.role}",
                metadata={"user_id": membership.user_id, "role": membership.role},
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            return Response(
                {
                    "message": f"Successfully added {membership.user.display_name} to {household.name}.",
                    "membership": HouseholdMemberSerializer(membership).data,
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(
        detail=True,
        methods=["delete"],
        url_path=r"members/(?P<user_id>\d+)",
        permission_classes=[IsAuthenticated, IsHouseholdOwner],
    )
    def remove_member(self, request, pk=None, user_id=None):
        """
        Remove a member from the household.
        Owners cannot remove themselves if they are the sole owner.
        """
        household = self.get_object()
        membership = household.memberships.filter(user_id=user_id).first()

        if not membership:
            return Response(
                {"error": "User is not a member of this household."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Safety check: prevent leaving household without an owner
        if membership.role == HouseholdMembership.Role.OWNER:
            owner_count = household.memberships.filter(
                role=HouseholdMembership.Role.OWNER
            ).count()
            if owner_count <= 1:
                return Response(
                    {"error": "Cannot remove the only owner of the household."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        AuditService.log(
            action=AuditEvent.Action.MEMBER_REMOVED,
            actor=request.user,
            household=household,
            description=f"Removed user {membership.user.display_name} (ID: {membership.user_id}) from household {household.name}",
            metadata={"removed_user_id": membership.user_id, "role": membership.role},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        membership.delete()
        return Response(
            {"message": "Member removed successfully."},
            status=status.HTTP_200_OK,
        )
