import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillDocument, BillCategory, BillStatus
from apps.audit.models import AuditEvent

User = get_user_model()


@pytest.mark.django_db
class TestWebHouseholdMemberManagement:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="web_owner",
            email="web_owner@example.com",
            password="Password123!",
            first_name="Alice",
            last_name="Owner",
        )
        self.member = User.objects.create_user(
            username="web_member",
            email="web_member@example.com",
            password="Password123!",
            first_name="Bob",
            last_name="Member",
        )
        self.second_owner = User.objects.create_user(
            username="web_owner2",
            email="web_owner2@example.com",
            password="Password123!",
            first_name="Charlie",
            last_name="Owner2",
        )
        self.household = Household.objects.create(name="Web Family", created_by=self.owner)
        self.m_owner = HouseholdMembership.objects.create(
            household=self.household,
            user=self.owner,
            role=HouseholdMembership.Role.OWNER,
        )
        self.m_member = HouseholdMembership.objects.create(
            household=self.household,
            user=self.member,
            role=HouseholdMembership.Role.MEMBER,
        )

    def test_household_view_displays_members(self):
        self.client.force_login(self.owner)
        res = self.client.get(f"/household/?household={self.household.id}")
        assert res.status_code == 200
        content = res.content.decode()
        assert "Web Family" in content
        assert "Alice Owner" in content
        assert "Bob Member" in content
        assert "Promote to Owner" in content

    def test_household_change_role_promote_and_demote(self):
        self.client.force_login(self.owner)

        # 1. Promote member to owner
        res_promote = self.client.post(
            f"/household/members/{self.m_member.id}/change-role/",
            {"role": "OWNER"},
        )
        assert res_promote.status_code == 302
        self.m_member.refresh_from_db()
        assert self.m_member.role == HouseholdMembership.Role.OWNER

        # Check audit event
        audit = AuditEvent.objects.filter(
            action=AuditEvent.Action.ROLE_CHANGED,
            household=self.household,
        ).first()
        assert audit is not None
        assert "OWNER" in audit.description

        # 2. Demote back to member (since self.owner is still an owner, there are 2 owners)
        res_demote = self.client.post(
            f"/household/members/{self.m_member.id}/change-role/",
            {"role": "MEMBER"},
        )
        assert res_demote.status_code == 302
        self.m_member.refresh_from_db()
        assert self.m_member.role == HouseholdMembership.Role.MEMBER

    def test_household_cannot_demote_sole_owner(self):
        self.client.force_login(self.owner)

        # Try to demote the sole owner
        res = self.client.post(
            f"/household/members/{self.m_owner.id}/change-role/",
            {"role": "MEMBER"},
        )
        assert res.status_code == 302
        self.m_owner.refresh_from_db()
        # Role must remain OWNER
        assert self.m_owner.role == HouseholdMembership.Role.OWNER

    def test_household_remove_member_web(self):
        self.client.force_login(self.owner)

        res = self.client.post(
            f"/household/members/{self.m_member.id}/remove/",
        )
        assert res.status_code == 302
        assert not HouseholdMembership.objects.filter(id=self.m_member.id).exists()
        # User account is preserved
        assert User.objects.filter(id=self.member.id).exists()

        # Audit event logged
        audit = AuditEvent.objects.filter(
            action=AuditEvent.Action.MEMBER_REMOVED,
            household=self.household,
        ).first()
        assert audit is not None
        assert "Bob Member" in audit.description

    def test_household_cannot_remove_sole_owner(self):
        self.client.force_login(self.owner)

        res = self.client.post(
            f"/household/members/{self.m_owner.id}/remove/",
        )
        assert res.status_code == 302
        # Membership must not be deleted
        assert HouseholdMembership.objects.filter(id=self.m_owner.id).exists()

    def test_non_owner_cannot_change_role_or_remove(self):
        self.client.force_login(self.member)

        # Attempt role change
        res_role = self.client.post(
            f"/household/members/{self.m_owner.id}/change-role/",
            {"role": "MEMBER"},
        )
        assert res_role.status_code == 302
        self.m_owner.refresh_from_db()
        assert self.m_owner.role == HouseholdMembership.Role.OWNER

        # Attempt member removal
        res_rem = self.client.post(
            f"/household/members/{self.m_owner.id}/remove/",
        )
        assert res_rem.status_code == 302
        assert HouseholdMembership.objects.filter(id=self.m_owner.id).exists()


@pytest.mark.django_db
class TestWebDocumentManagementAndRetry:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="doc_owner",
            email="doc_owner@example.com",
            password="Password123!",
            first_name="Dana",
            last_name="Owner",
        )
        self.household = Household.objects.create(name="Doc Family", created_by=self.owner)
        HouseholdMembership.objects.create(
            household=self.household,
            user=self.owner,
            role=HouseholdMembership.Role.OWNER,
        )

    def test_delete_draft_bill_document(self):
        # Create draft document with no linked bill
        test_file = SimpleUploadedFile("bill_draft.pdf", b"%PDF-1.4 mock content", content_type="application/pdf")
        doc = BillDocument.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            file=test_file,
            file_name="bill_draft.pdf",
            file_size=len(test_file),
            mime_type="application/pdf",
        )

        self.client.force_login(self.owner)

        # Delete document via web action
        res = self.client.post(f"/bills/documents/{doc.id}/delete/")
        assert res.status_code == 302
        assert not BillDocument.objects.filter(id=doc.id).exists()

        # Check audit event
        audit = AuditEvent.objects.filter(
            action=AuditEvent.Action.DOCUMENT_DELETED,
            household=self.household,
        ).first()
        assert audit is not None
        assert "bill_draft.pdf" in audit.description

    def test_cannot_delete_document_linked_to_bill(self):
        test_file = SimpleUploadedFile("bill_final.pdf", b"%PDF-1.4 mock content", content_type="application/pdf")
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            provider_name="Electric Co",
            amount="500.00",
            category=BillCategory.ELECTRICITY,
            due_date="2026-11-20",
        )
        doc = BillDocument.objects.create(
            bill=bill,
            household=self.household,
            uploaded_by=self.owner,
            file=test_file,
            file_name="bill_final.pdf",
            file_size=len(test_file),
            mime_type="application/pdf",
        )

        self.client.force_login(self.owner)

        # Attempt to delete finalized bill document
        res = self.client.post(f"/bills/documents/{doc.id}/delete/")
        assert res.status_code == 302
        # Must still exist
        assert BillDocument.objects.filter(id=doc.id).exists()

    def test_retry_document_extraction(self):
        test_file = SimpleUploadedFile("bill_retry.pdf", b"%PDF-1.4 dummy", content_type="application/pdf")
        doc = BillDocument.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            file=test_file,
            file_name="bill_retry.pdf",
            file_size=len(test_file),
            mime_type="application/pdf",
            extraction_status=BillDocument.ExtractionStatus.FAILED,
        )

        self.client.force_login(self.owner)

        res = self.client.post(f"/bills/documents/{doc.id}/retry/")
        assert res.status_code == 302
        assert f"doc_id={doc.id}" in res.url
        doc.refresh_from_db()
        assert doc.extraction_status in (
            BillDocument.ExtractionStatus.SUCCESS,
            BillDocument.ExtractionStatus.NEEDS_REVIEW,
        )
