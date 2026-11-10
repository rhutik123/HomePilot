from datetime import timedelta
from decimal import Decimal
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillStatus
from apps.payments.models import Payment, PaymentReceipt, PaymentAdjustment, PaymentMethod
from apps.payments.services.payment_service import PaymentService
from apps.reminders.services.reminder_service import ReminderService
from apps.reminders.models import BillReminder

User = get_user_model()


@pytest.mark.django_db
class TestPaymentManagement:
    def setup_method(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="pay_owner",
            email="pay_owner@example.com",
            password="Password123!",
            first_name="Pay",
            last_name="Owner",
        )
        self.member = User.objects.create_user(
            username="pay_member",
            email="pay_member@example.com",
            password="Password123!",
            first_name="Pay",
            last_name="Member",
        )
        self.stranger = User.objects.create_user(
            username="pay_stranger",
            email="pay_stranger@example.com",
            password="Password123!",
        )

        # Primary Household
        self.household = Household.objects.create(name="Payment Test Household", created_by=self.owner)
        HouseholdMembership.objects.create(household=self.household, user=self.owner, role=HouseholdMembership.Role.OWNER)
        HouseholdMembership.objects.create(household=self.household, user=self.member, role=HouseholdMembership.Role.MEMBER)

        # Stranger Household
        self.other_household = Household.objects.create(name="Stranger Household", created_by=self.stranger)
        HouseholdMembership.objects.create(household=self.other_household, user=self.stranger, role=HouseholdMembership.Role.OWNER)

        self.client.force_authenticate(user=self.member)

    def test_record_full_payment_updates_balance_and_status(self):
        """Paying a bill in full updates total_paid, sets balance to 0, status to PAID, and clears reminders."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            assigned_to=self.member,
            provider_name="Tata Power",
            amount=Decimal("2500.00"),
            due_date=today + timedelta(days=10),
        )
        ReminderService.create_reminders_for_bill(bill)
        assert bill.reminders.count() == 4

        payload = {
            "bill_id": bill.id,
            "amount": "2500.00",
            "payment_method": "UPI",
            "transaction_reference": "UPI/2026/889911",
            "notes": "Paid full bill via GPay",
        }
        response = self.client.post("/api/v1/payments/", payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["bill_remaining_balance"] == "0.00"
        assert response.data["bill_status"] == "PAID"

        bill.refresh_from_db()
        assert bill.status == BillStatus.PAID
        assert bill.total_paid == Decimal("2500.00")
        assert bill.outstanding_balance == Decimal("0.00")
        assert bill.paid_at is not None
        # Pending reminders must be cancelled, preserving history
        assert bill.reminders.filter(status=BillReminder.ReminderStatus.PENDING).count() == 0
        assert bill.reminders.filter(status=BillReminder.ReminderStatus.CANCELLED).count() == 4

    def test_record_partial_payments_sequence(self):
        """Multiple partial payments incrementally reduce balance until full settlement."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            assigned_to=self.member,
            provider_name="City Internet",
            amount=Decimal("5000.00"),
            due_date=today + timedelta(days=10),
        )
        ReminderService.create_reminders_for_bill(bill)

        # 1. First partial payment: 2,000
        res1 = self.client.post(
            "/api/v1/payments/",
            {"bill_id": bill.id, "amount": "2000.00", "payment_method": "UPI"},
            format="json",
        )
        assert res1.status_code == status.HTTP_201_CREATED
        assert res1.data["bill_status"] == "PARTIALLY_PAID"
        assert res1.data["bill_remaining_balance"] == "3000.00"

        bill.refresh_from_db()
        assert bill.status == BillStatus.PARTIALLY_PAID
        assert bill.total_paid == Decimal("2000.00")
        assert bill.outstanding_balance == Decimal("3000.00")
        assert bill.paid_at is None
        # Reminders should still exist for remaining balance
        assert bill.reminders.filter(status=BillReminder.ReminderStatus.PENDING).count() == 4

        # 2. Second partial payment: 1,000
        res2 = self.client.post(
            "/api/v1/payments/",
            {"bill_id": bill.id, "amount": "1000.00", "payment_method": "BANK_TRANSFER"},
            format="json",
        )
        assert res2.status_code == status.HTTP_201_CREATED
        assert res2.data["bill_remaining_balance"] == "2000.00"

        bill.refresh_from_db()
        assert bill.total_paid == Decimal("3000.00")
        assert bill.outstanding_balance == Decimal("2000.00")
        assert bill.status == BillStatus.PARTIALLY_PAID

        # 3. Final payment settling remainder: 2,000
        res3 = self.client.post(
            "/api/v1/payments/",
            {"bill_id": bill.id, "amount": "2000.00", "payment_method": "CREDIT_CARD"},
            format="json",
        )
        assert res3.status_code == status.HTTP_201_CREATED
        assert res3.data["bill_status"] == "PAID"
        assert res3.data["bill_remaining_balance"] == "0.00"

        bill.refresh_from_db()
        assert bill.status == BillStatus.PAID
        assert bill.total_paid == Decimal("5000.00")
        assert bill.outstanding_balance == Decimal("0.00")
        assert bill.paid_at is not None
        assert bill.reminders.filter(status=BillReminder.ReminderStatus.PENDING).count() == 0
        assert bill.reminders.filter(status=BillReminder.ReminderStatus.CANCELLED).count() == 4

    def test_overpayment_rejected(self):
        """Payment amount exceeding outstanding balance is strictly rejected."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Water Dept",
            amount=Decimal("1000.00"),
            due_date=today + timedelta(days=5),
        )

        response = self.client.post(
            "/api/v1/payments/",
            {"bill_id": bill.id, "amount": "1200.00"},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "exceeds" in str(response.data)

        bill.refresh_from_db()
        assert bill.total_paid == Decimal("0.00")
        assert bill.outstanding_balance == Decimal("1000.00")

    def test_zero_and_negative_payment_rejected(self):
        """Zero or negative payment amounts are rejected."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Gas Corp",
            amount=Decimal("800.00"),
            due_date=today + timedelta(days=5),
        )

        # Zero payment
        res_zero = self.client.post("/api/v1/payments/", {"bill_id": bill.id, "amount": "0.00"}, format="json")
        assert res_zero.status_code == status.HTTP_400_BAD_REQUEST

        # Negative payment
        res_neg = self.client.post("/api/v1/payments/", {"bill_id": bill.id, "amount": "-150.00"}, format="json")
        assert res_neg.status_code == status.HTTP_400_BAD_REQUEST

    def test_payment_on_already_paid_bill_rejected(self):
        """Attempting to pay an already settled bill is rejected."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Electricity",
            amount=Decimal("1500.00"),
            due_date=today + timedelta(days=5),
        )
        bill.mark_as_paid()

        response = self.client.post(
            "/api/v1/payments/",
            {"bill_id": bill.id, "amount": "500.00"},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "already been paid" in str(response.data)

    def test_payment_with_receipt_attachment(self):
        """Uploading a receipt image or PDF with payment saves PaymentReceipt."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Mobile Bill",
            amount=Decimal("699.00"),
            due_date=today + timedelta(days=5),
        )

        receipt_pdf = SimpleUploadedFile("payment_receipt.pdf", b"%PDF-1.4\nReceipt content\n%%EOF", content_type="application/pdf")

        response = self.client.post(
            "/api/v1/payments/",
            {
                "bill_id": bill.id,
                "amount": "699.00",
                "payment_method": "UPI",
                "receipt_file": receipt_pdf,
            },
            format="multipart",
        )
        assert response.status_code == status.HTTP_201_CREATED

        payment_id = response.data["payment"]["id"]
        payment = Payment.objects.get(id=payment_id)
        assert hasattr(payment, "receipt")
        assert payment.receipt.file_name == "payment_receipt.pdf"

    def test_payment_adjustment_by_owner(self):
        """Household owner can record an adjustment / reversal which updates bill balance."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Refundable Expense",
            amount=Decimal("3000.00"),
            due_date=today + timedelta(days=10),
        )

        payment = PaymentService.record_payment(
            bill=bill,
            user=self.member,
            amount=Decimal("3000.00"),
        )
        bill.refresh_from_db()
        assert bill.status == BillStatus.PAID
        assert bill.outstanding_balance == Decimal("0.00")

        # Owner records adjustment (e.g. -1,000 refund/chargeback)
        self.client.force_authenticate(user=self.owner)
        res_adjust = self.client.post(
            f"/api/v1/payments/{payment.id}/adjust/",
            {"amount": "-1000.00", "reason": "Partial bank chargeback reversed"},
            format="json",
        )
        assert res_adjust.status_code == status.HTTP_200_OK
        assert res_adjust.data["new_payment_amount"] == "2000.00"
        assert res_adjust.data["bill_remaining_balance"] == "1000.00"
        assert res_adjust.data["bill_status"] == "PARTIALLY_PAID"

        bill.refresh_from_db()
        assert bill.status == BillStatus.PARTIALLY_PAID
        assert bill.total_paid == Decimal("2000.00")
        assert bill.outstanding_balance == Decimal("1000.00")

    def test_non_owner_cannot_adjust_payment(self):
        """Regular household member cannot adjust payments (restricted to owners)."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Expense",
            amount=Decimal("1000.00"),
            due_date=today + timedelta(days=5),
        )
        payment = PaymentService.record_payment(bill=bill, user=self.member, amount=Decimal("1000.00"))

        # Member attempts adjustment -> rejected
        self.client.force_authenticate(user=self.member)
        response = self.client.post(
            f"/api/v1/payments/{payment.id}/adjust/",
            {"amount": "-200.00", "reason": "Unauthorized adjustment"},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Only household owners" in str(response.data)

    def test_nested_bill_payments_endpoint(self):
        """GET and POST /api/v1/payments/bills/{bill_id}/payments/ correctly scopes to bill."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Nested Endpoint Test",
            amount=Decimal("4000.00"),
            due_date=today + timedelta(days=5),
        )

        # Record payment via nested endpoint
        post_res = self.client.post(
            f"/api/v1/payments/bills/{bill.id}/payments/",
            {"amount": "1500.00", "payment_method": "UPI"},
            format="json",
        )
        assert post_res.status_code == status.HTTP_201_CREATED
        assert post_res.data["bill_remaining_balance"] == "2500.00"

        # List payments via nested endpoint
        get_res = self.client.get(f"/api/v1/payments/bills/{bill.id}/payments/")
        assert get_res.status_code == status.HTTP_200_OK
        results = get_res.data.get("results", get_res.data)
        assert len(results) == 1
        assert results[0]["amount"] == "1500.00"

    def test_payment_multi_tenant_isolation(self):
        """User cannot record payment for another household's bill or view other payments."""
        today = timezone.localdate()
        stranger_bill = Bill.objects.create(
            household=self.other_household,
            uploaded_by=self.stranger,
            provider_name="Secret Stranger Bill",
            amount=Decimal("5000.00"),
            due_date=today + timedelta(days=5),
        )

        # Member from Household A tries to record payment for Household B's bill
        response = self.client.post(
            "/api/v1/payments/",
            {"bill_id": stranger_bill.id, "amount": "1000.00"},
            format="json",
        )
        assert response.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND]
