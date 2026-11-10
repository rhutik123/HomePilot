import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from apps.households.models import Household, HouseholdMembership

User = get_user_model()


@pytest.mark.django_db
class TestHouseholdManagement:
    def setup_method(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="owner_user",
            email="owner@example.com",
            password="Password123!",
            first_name="Family",
            last_name="Owner",
        )
        self.member = User.objects.create_user(
            username="member_user",
            email="member@example.com",
            password="Password123!",
            first_name="Family",
            last_name="Member",
        )
        self.stranger = User.objects.create_user(
            username="stranger_user",
            email="stranger@example.com",
            password="Password123!",
        )

    def test_create_household_auto_assigns_owner(self):
        """Creating a household sets creator as OWNER membership."""
        self.client.force_authenticate(user=self.owner)
        response = self.client.post("/api/households/", {"name": "The Smiths"}, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        household = Household.objects.get(id=response.data["id"])
        assert household.created_by == self.owner
        assert household.is_owner(self.owner)
        assert household.member_count == 1

    def test_user_can_list_only_own_households(self):
        """Strict multi-tenancy: users cannot see other households in list."""
        # Create household for owner
        self.client.force_authenticate(user=self.owner)
        self.client.post("/api/households/", {"name": "Smith Household"}, format="json")

        # Create separate household for stranger
        self.client.force_authenticate(user=self.stranger)
        self.client.post("/api/households/", {"name": "Stranger Household"}, format="json")

        # Stranger lists households -> only sees Stranger Household
        response = self.client.get("/api/households/")
        assert response.status_code == status.HTTP_200_OK
        results = response.data["results"] if "results" in response.data else response.data
        assert len(results) == 1
        assert results[0]["name"] == "Stranger Household"

    def test_user_cannot_access_other_household_detail(self):
        """Strict multi-tenancy: querying someone else's household returns 404."""
        self.client.force_authenticate(user=self.owner)
        create_res = self.client.post("/api/households/", {"name": "Smith Household"}, format="json")
        household_id = create_res.data["id"]

        # Stranger tries to fetch Smith Household
        self.client.force_authenticate(user=self.stranger)
        response = self.client.get(f"/api/households/{household_id}/")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_owner_can_add_member_by_email(self):
        """Household owner can add an existing registered user as a MEMBER."""
        self.client.force_authenticate(user=self.owner)
        create_res = self.client.post("/api/households/", {"name": "Smith Household"}, format="json")
        household_id = create_res.data["id"]

        response = self.client.post(
            f"/api/households/{household_id}/add_member/",
            {"username_or_email": "member@example.com", "role": "MEMBER"},
            format="json",
        )
        assert response.status_code == status.HTTP_201_CREATED

        household = Household.objects.get(id=household_id)
        assert household.is_member(self.member)
        assert not household.is_owner(self.member)
        assert household.member_count == 2

    def test_member_cannot_add_other_members(self):
        """Regular MEMBER cannot add members (only OWNER has permission)."""
        # Create household and add member
        self.client.force_authenticate(user=self.owner)
        create_res = self.client.post("/api/households/", {"name": "Smith Household"}, format="json")
        household_id = create_res.data["id"]

        self.client.post(
            f"/api/households/{household_id}/add_member/",
            {"username_or_email": "member@example.com", "role": "MEMBER"},
            format="json",
        )

        # Authenticate as member and attempt to add stranger
        self.client.force_authenticate(user=self.member)
        response = self.client.post(
            f"/api/households/{household_id}/add_member/",
            {"username_or_email": "stranger@example.com", "role": "MEMBER"},
            format="json",
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_cannot_add_duplicate_member(self):
        """Adding a user who is already in the household is rejected."""
        self.client.force_authenticate(user=self.owner)
        create_res = self.client.post("/api/households/", {"name": "Smith Household"}, format="json")
        household_id = create_res.data["id"]

        # First add
        self.client.post(
            f"/api/households/{household_id}/add_member/",
            {"username_or_email": "member@example.com"},
            format="json",
        )
        # Duplicate add
        response = self.client.post(
            f"/api/households/{household_id}/add_member/",
            {"username_or_email": "member@example.com"},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_owner_can_remove_member(self):
        """Owner can remove a member from the household."""
        self.client.force_authenticate(user=self.owner)
        create_res = self.client.post("/api/households/", {"name": "Smith Household"}, format="json")
        household_id = create_res.data["id"]

        self.client.post(
            f"/api/households/{household_id}/add_member/",
            {"username_or_email": "member@example.com"},
            format="json",
        )

        response = self.client.delete(f"/api/households/{household_id}/members/{self.member.id}/")
        assert response.status_code == status.HTTP_200_OK

        household = Household.objects.get(id=household_id)
        assert not household.is_member(self.member)

    def test_cannot_remove_sole_owner(self):
        """Cannot remove the sole owner of a household."""
        self.client.force_authenticate(user=self.owner)
        create_res = self.client.post("/api/households/", {"name": "Smith Household"}, format="json")
        household_id = create_res.data["id"]

        response = self.client.delete(f"/api/households/{household_id}/members/{self.owner.id}/")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Cannot remove the only owner" in response.data["error"]
