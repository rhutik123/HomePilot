from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillStatus, BillCategory, BillDocument
from apps.reminders.models import BillReminder, ReminderType, NotificationLog
from apps.reminders.services.reminder_service import ReminderService
from apps.reminders.tasks import send_bill_reminder_task
from apps.audit.models import AuditEvent
from apps.payments.models import Payment

User = get_user_model()


@pytest.mark.django_db
class TestPhase0SecurityAndStabilization:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="alice_sec",
            email="alice_sec@example.com",
            password="SecurePassword123!",
            first_name="Alice",
            last_name="Security",
        )
        self.family_member = User.objects.create_user(
            username="bob_sec",
            email="bob_sec@example.com",
            password="SecurePassword123!",
            first_name="Bob",
            last_name="Security",
        )
        self.stranger = User.objects.create_user(
            username="stranger_sec",
            email="stranger_sec@example.com",
            password="SecurePassword123!",
        )

        # Primary Household
        self.household = Household.objects.create(name="Secure Home A", created_by=self.user)
        HouseholdMembership.objects.create(household=self.household, user=self.user, role=HouseholdMembership.Role.OWNER)
        HouseholdMembership.objects.create(household=self.household, user=self.family_member, role=HouseholdMembership.Role.MEMBER)

        # Separate Household
        self.other_household = Household.objects.create(name="Secure Home B", created_by=self.stranger)
        HouseholdMembership.objects.create(household=self.other_household, user=self.stranger, role=HouseholdMembership.Role.OWNER)

        self.client.force_authenticate(user=self.user)

    def test_cannot_reassign_bill_to_another_household_on_patch(self):
        """Defect #3: BillSerializer must reject changing the household on update."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            assigned_to=self.user,
            provider_name="City Power",
            amount=Decimal("1500.00"),
            due_date=today + timedelta(days=10),
            status=BillStatus.PENDING,
        )

        response = self.client.patch(
            f"/api/bills/{bill.id}/",
            {"household": self.other_household.id},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "household" in response.data
        assert "Household cannot be changed" in str(response.data["household"])

        bill.refresh_from_db()
        assert bill.household == self.household

    def test_cannot_create_bill_for_foreign_household(self):
        """Defect #3: User cannot create a bill belonging to a household they are not a member of."""
        today = timezone.localdate()
        payload = {
            "household": self.other_household.id,
            "provider_name": "Infiltrator Utility",
            "amount": "2000.00",
            "due_date": str(today + timedelta(days=5)),
        }
        response = self.client.post("/api/bills/", payload, format="json")
        assert response.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND]

    def test_status_is_read_only_and_cannot_be_patched_to_paid(self):
        """Defect #4: status must be read-only in BillSerializer and cannot be directly patched."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            assigned_to=self.user,
            provider_name="Water Dept",
            amount=Decimal("700.00"),
            due_date=today + timedelta(days=5),
            status=BillStatus.PENDING,
        )

        # Attempt to patch status directly to PAID
        response = self.client.patch(
            f"/api/bills/{bill.id}/",
            {"status": "PAID"},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK

        bill.refresh_from_db()
        # Status must remain PENDING because status is read-only
        assert bill.status == BillStatus.PENDING
        assert bill.paid_at is None

    def test_updating_due_date_reschedules_unsent_reminders(self):
        """Defect #5: Changing a bill's due_date updates the remind_at of unsent reminders."""
        today = timezone.localdate()
        initial_due = today + timedelta(days=10)
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            assigned_to=self.family_member,
            provider_name="Broadband",
            amount=Decimal("1200.00"),
            due_date=initial_due,
        )
        ReminderService.create_reminders_for_bill(bill)
        initial_reminders = {r.reminder_type: r.remind_at for r in bill.reminders.all()}
        assert initial_reminders[ReminderType.THREE_DAYS_BEFORE] == initial_due - timedelta(days=3)

        # Postpone due date by 10 days via API
        new_due = initial_due + timedelta(days=10)
        response = self.client.patch(
            f"/api/bills/{bill.id}/",
            {"due_date": str(new_due)},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK

        bill.refresh_from_db()
        assert bill.due_date == new_due

        # Verify reminders were rescheduled
        updated_reminders = {r.reminder_type: r.remind_at for r in bill.reminders.all()}
        assert updated_reminders[ReminderType.THREE_DAYS_BEFORE] == new_due - timedelta(days=3)
        assert updated_reminders[ReminderType.ONE_DAY_BEFORE] == new_due - timedelta(days=1)
        assert updated_reminders[ReminderType.DUE_DATE] == new_due

    def test_updating_assignee_updates_unsent_reminder_recipients(self):
        """Defect #5: Changing assigned_to updates sent_to on unsent reminders."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            assigned_to=self.user,
            provider_name="Electricity",
            amount=Decimal("1800.00"),
            due_date=today + timedelta(days=10),
        )
        ReminderService.create_reminders_for_bill(bill)

        for r in bill.reminders.all():
            assert r.sent_to == self.user

        # Reassign to family member via API
        response = self.client.patch(
            f"/api/bills/{bill.id}/",
            {"assigned_to": self.family_member.id},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK

        bill.refresh_from_db()
        assert bill.assigned_to == self.family_member

        for r in bill.reminders.all():
            assert r.sent_to == self.family_member

    def test_reminder_task_does_not_mark_sent_when_recipient_has_no_email(self):
        """Defect #6: Reminder task must not mark is_sent=True when delivery fails due to missing email."""
        no_email_user = User.objects.create_user(
            username="no_email_user",
            email="",
            password="Password123!",
        )
        HouseholdMembership.objects.create(household=self.household, user=no_email_user, role=HouseholdMembership.Role.MEMBER)

        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            assigned_to=no_email_user,
            provider_name="No Email Utility",
            amount=Decimal("500.00"),
            due_date=today + timedelta(days=3),
        )
        reminders = ReminderService.create_reminders_for_bill(bill)
        reminder = reminders[0]

        result = send_bill_reminder_task(reminder.id)
        assert result is False

        reminder.refresh_from_db()
        assert reminder.is_sent is False
        assert reminder.sent_at is None

        # Verify failed audit log exists
        failed_log = NotificationLog.objects.filter(reminder=reminder, status=NotificationLog.Status.FAILED).first()
        assert failed_log is not None
        assert "No recipient email" in failed_log.error_message

    def test_notification_failure_log_persists_when_send_mail_raises_exception(self):
        """Defect #7: NotificationLog for failure must NOT be rolled back if send_mail raises an exception."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            assigned_to=self.user,
            provider_name="Exception Utility",
            amount=Decimal("950.00"),
            due_date=today + timedelta(days=3),
        )
        reminders = ReminderService.create_reminders_for_bill(bill)
        reminder = reminders[0]

        # Simulate SMTP/network socket failure
        with patch("apps.reminders.services.email_service.send_mail", side_effect=ConnectionError("SMTP connection timed out")):
            try:
                send_bill_reminder_task(reminder.id)
            except Exception:
                pass

        # Failure log must exist and not have been rolled back
        failed_log = NotificationLog.objects.filter(reminder=reminder, status=NotificationLog.Status.FAILED).first()
        assert failed_log is not None
        assert "SMTP connection timed out" in failed_log.error_message

        # Reminder is_sent must still be False
        reminder.refresh_from_db()
        assert reminder.is_sent is False

    def test_marking_bill_paid_does_not_cascade_delete_notification_logs(self):
        """Defect #8: Deleting pending reminders must NOT delete NotificationLog history."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            assigned_to=self.user,
            provider_name="Water Audit Test",
            amount=Decimal("450.00"),
            due_date=today + timedelta(days=3),
        )
        reminders = ReminderService.create_reminders_for_bill(bill)
        reminder = reminders[0]

        # Create a notification log for this reminder
        log = NotificationLog.objects.create(
            reminder=reminder,
            bill=bill,
            recipient_email=self.user.email,
            subject="Test Reminder Log",
            channel="EMAIL",
            status=NotificationLog.Status.SUCCESS,
        )

        # Mark bill as paid (which cancels pending reminders, preserving audit history)
        bill.mark_as_paid()
        reminder.refresh_from_db()
        assert reminder.status == BillReminder.ReminderStatus.CANCELLED

        # Even if a reminder is subsequently deleted, the log MUST still exist with reminder set to None (SET_NULL)
        reminder.delete()
        log.refresh_from_db()
        assert log.reminder is None
        assert log.bill == bill
        assert log.status == NotificationLog.Status.SUCCESS

    def test_production_settings_rejects_missing_secret_key(self, monkeypatch):
        """Defect #9: production.py raises ImproperlyConfigured when DJANGO_SECRET_KEY is insecure or unset."""
        import importlib
        monkeypatch.setenv("DJANGO_SECRET_KEY", "django-insecure-fallback-development-key-homepilot-2026-default")
        monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "homepilot.example.com")

        with pytest.raises(ImproperlyConfigured) as exc_info:
            import config.settings.production
            importlib.reload(config.settings.production)
        assert "DJANGO_SECRET_KEY must be set" in str(exc_info.value)

    def test_web_logout_rejects_get_and_requires_post(self):
        """Web logout rejects GET requests with redirect+warning, but succeeds on POST."""
        self.client.force_login(self.user)
        # Attempt logout via GET
        get_response = self.client.get("/logout/")
        assert get_response.status_code == status.HTTP_302_FOUND
        assert get_response.url == "/dashboard/"
        # User must still be authenticated
        session = self.client.session
        assert "_auth_user_id" in session

        # Legitimate logout via POST
        post_response = self.client.post("/logout/")
        assert post_response.status_code == status.HTTP_302_FOUND
        assert "/login/" in post_response.url
        session = self.client.session
        assert "_auth_user_id" not in session

    def test_web_registration_rejects_weak_password(self):
        """Web registration must enforce Django password validators and reject weak passwords."""
        payload = {
            "username": "weakling",
            "email": "weakling@example.com",
            "first_name": "Weak",
            "last_name": "User",
            "password": "123",
            "password_confirm": "123",
        }
        response = self.client.post("/register/", payload)
        assert response.status_code == status.HTTP_200_OK
        assert b"short" in response.content.lower() or b"entirely numeric" in response.content.lower() or b"too short" in response.content.lower()
        assert not User.objects.filter(username="weakling").exists()

    def test_web_registration_rejects_mismatched_passwords(self):
        """Web registration must reject requests where passwords do not match."""
        payload = {
            "username": "mismatch_user",
            "email": "mismatch@example.com",
            "first_name": "Mis",
            "last_name": "Match",
            "password": "SecurePassword123!",
            "password_confirm": "DifferentPassword123!",
        }
        response = self.client.post("/register/", payload)
        assert response.status_code == status.HTTP_200_OK
        assert b"do not match" in response.content.lower()
        assert not User.objects.filter(username="mismatch_user").exists()

    def test_web_bill_upload_rejects_non_household_assignee(self):
        """Assigning a bill via web form to a user outside the active household must be rejected."""
        self.client.force_login(self.user)
        # Set active household in session
        session = self.client.session
        session["active_household_id"] = self.household.id
        session.save()

        # Create a document for review flow
        doc = BillDocument.objects.create(
            household=self.household,
            file_name="test_gas.pdf",
            file_size=1024,
            mime_type="application/pdf",
        )

        today = timezone.localdate()
        payload = {
            "action": "confirm_bill",
            "doc_id": doc.id,
            "provider_name": "City Gas",
            "amount": "450.00",
            "category": BillCategory.GAS,
            "due_date": str(today + timedelta(days=10)),
            "assigned_to": self.stranger.id,  # Stranger is NOT a member of self.household
        }
        response = self.client.post("/bills/upload/", payload)
        assert response.status_code == status.HTTP_200_OK
        assert b"not an active member" in response.content.lower()
        assert not Bill.objects.filter(provider_name="City Gas").exists()

    def test_household_add_and_remove_member_creates_audit_events(self):
        """HouseholdViewSet add_member and remove_member operations must generate compliance audit events."""
        new_member = User.objects.create_user(
            username="audit_member",
            email="audit_member@example.com",
            password="SecurePassword123!",
        )

        # 1. Add member
        add_response = self.client.post(
            f"/api/households/{self.household.id}/add_member/",
            {"username_or_email": new_member.email, "role": HouseholdMembership.Role.MEMBER},
            format="json",
        )
        assert add_response.status_code == status.HTTP_201_CREATED

        add_event = AuditEvent.objects.filter(
            household=self.household,
            action=AuditEvent.Action.MEMBER_ADDED,
        ).first()
        assert add_event is not None
        assert add_event.actor == self.user
        assert add_event.metadata.get("user_id") == new_member.id

        # 2. Remove member
        remove_response = self.client.delete(
            f"/api/households/{self.household.id}/members/{new_member.id}/",
        )
        assert remove_response.status_code == status.HTTP_200_OK

        remove_event = AuditEvent.objects.filter(
            household=self.household,
            action=AuditEvent.Action.MEMBER_REMOVED,
        ).first()
        assert remove_event is not None
        assert remove_event.actor == self.user
        assert remove_event.metadata.get("removed_user_id") == new_member.id

    def test_bill_creation_and_payment_creates_audit_events(self):
        """Creating and marking a bill paid via API creates BILL_CREATED, BILL_PAID, and PAYMENT_RECORDED events."""
        today = timezone.localdate()
        payload = {
            "household": self.household.id,
            "provider_name": "Audit Broadband",
            "amount": "999.00",
            "due_date": str(today + timedelta(days=7)),
            "category": BillCategory.INTERNET,
        }
        create_resp = self.client.post("/api/bills/", payload, format="json")
        assert create_resp.status_code == status.HTTP_201_CREATED
        bill_id = create_resp.data["id"]

        create_event = AuditEvent.objects.filter(
            household=self.household,
            action=AuditEvent.Action.BILL_CREATED,
            entity_id=bill_id,
        ).first()
        assert create_event is not None
        assert create_event.actor == self.user

        # Mark paid
        pay_resp = self.client.post(f"/api/bills/{bill_id}/mark_paid/")
        assert pay_resp.status_code == status.HTTP_200_OK

        pay_event = AuditEvent.objects.filter(
            household=self.household,
            action=AuditEvent.Action.BILL_PAID,
            entity_id=bill_id,
        ).first()
        assert pay_event is not None

        payment_record_event = AuditEvent.objects.filter(
            household=self.household,
            action=AuditEvent.Action.PAYMENT_RECORDED,
        ).first()
        assert payment_record_event is not None

