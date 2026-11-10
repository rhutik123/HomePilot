from datetime import timedelta
from decimal import Decimal
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillStatus, BillCategory
from apps.payments.models import Payment, PaymentReceipt, PaymentAdjustment, PaymentMethod, PaymentStatus
from apps.payments.services.payment_service import PaymentService

User = get_user_model()


@pytest.mark.django_db
class TestPhase3PaymentsAndReceipts:
    def setup_method(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="p3_owner",
            email="p3_owner@example.com",
            password="SecurePassword123!",
            first_name="Alice",
            last_name="Owner",
        )
        self.member = User.objects.create_user(
            username="p3_member",
            email="p3_member@example.com",
            password="SecurePassword123!",
            first_name="Bob",
            last_name="Member",
        )
        self.stranger = User.objects.create_user(
            username="p3_stranger",
            email="p3_stranger@example.com",
            password="SecurePassword123!",
            first_name="Mallory",
            last_name="Stranger",
        )

        # Household A
        self.household = Household.objects.create(name="Phase 3 Household", created_by=self.owner)
        HouseholdMembership.objects.create(household=self.household, user=self.owner, role=HouseholdMembership.Role.OWNER)
        HouseholdMembership.objects.create(household=self.household, user=self.member, role=HouseholdMembership.Role.MEMBER)

        # Household B (Stranger)
        self.other_household = Household.objects.create(name="Other Household", created_by=self.stranger)
        HouseholdMembership.objects.create(household=self.other_household, user=self.stranger, role=HouseholdMembership.Role.OWNER)

        self.client.force_login(self.member)
        session = self.client.session
        session["active_household_id"] = self.household.id
        session.save()

    def test_void_payment_by_owner_updates_balance_and_preserves_history(self):
        """Household owner can void a payment; balance is restored and original payment row is preserved."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            provider_name="Adani Power",
            amount=Decimal("3500.00"),
            due_date=today + timedelta(days=5),
        )

        payment = PaymentService.record_payment(
            bill=bill,
            user=self.member,
            amount=Decimal("3500.00"),
            payment_method=PaymentMethod.UPI,
            transaction_reference="UPI/2026/001",
        )
        bill.refresh_from_db()
        assert bill.status == BillStatus.PAID
        assert bill.outstanding_balance == Decimal("0.00")

        # Void payment as owner via API
        self.client.force_login(self.owner)
        res_void = self.client.post(
            f"/api/v1/payments/{payment.id}/void/",
            {"reason": "Accidental double charge reported by bank"},
            format="json",
        )
        assert res_void.status_code == status.HTTP_200_OK
        assert res_void.data["payment_status"] == PaymentStatus.VOIDED
        assert res_void.data["bill_remaining_balance"] == "3500.00"
        assert res_void.data["bill_status"] == BillStatus.PENDING

        payment.refresh_from_db()
        # Original payment row amount is NOT deleted or set to zero
        assert payment.amount == Decimal("3500.00")
        assert payment.status == PaymentStatus.VOIDED
        assert payment.net_amount == Decimal("0.00")
        assert payment.adjustments.count() == 1
        adj = payment.adjustments.first()
        assert adj.amount == Decimal("-3500.00")
        assert "Accidental double charge" in adj.reason

        bill.refresh_from_db()
        assert bill.status == BillStatus.PENDING
        assert bill.outstanding_balance == Decimal("3500.00")
        assert bill.total_paid == Decimal("0.00")

    def test_non_owner_cannot_void_payment(self):
        """Regular household member cannot void payments."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Gas Corp",
            amount=Decimal("1200.00"),
            due_date=today + timedelta(days=5),
        )
        payment = PaymentService.record_payment(bill=bill, user=self.member, amount=Decimal("1200.00"))

        # Member tries to void -> rejected
        self.client.force_login(self.member)
        res = self.client.post(f"/api/v1/payments/{payment.id}/void/", {"reason": "Unauthorized void"}, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert "Only household owners" in str(res.data)

    def test_download_receipt_api_tenant_authorization(self):
        """Household member can download receipt via API; unauthorized stranger gets 404."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Broadband",
            amount=Decimal("999.00"),
            due_date=today + timedelta(days=5),
        )
        payment = PaymentService.record_payment(bill=bill, user=self.member, amount=Decimal("999.00"))
        receipt_bytes = b"%PDF-1.4\nReceipt Evidence Content\n%%EOF"
        receipt_file = SimpleUploadedFile("broadband_receipt.pdf", receipt_bytes, content_type="application/pdf")
        PaymentService.attach_receipt(payment, self.member, receipt_file)

        # 1. Member can download
        self.client.force_login(self.member)
        res_member = self.client.get(f"/api/v1/payments/{payment.id}/receipt/")
        assert res_member.status_code == status.HTTP_200_OK
        assert b"%PDF-1.4" in b"".join(res_member.streaming_content)

        # 2. Stranger from another household gets 404
        self.client.force_login(self.stranger)
        res_stranger = self.client.get(f"/api/v1/payments/{payment.id}/receipt/")
        assert res_stranger.status_code == status.HTTP_404_NOT_FOUND

    def test_web_bill_detail_view_renders_ledger_and_balances(self):
        """GET /bills/{bill_id}/ renders bill detail, progress bar, and payment history ledger."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            assigned_to=self.member,
            provider_name="Water Authority",
            category=BillCategory.WATER,
            amount=Decimal("2000.00"),
            due_date=today + timedelta(days=12),
        )

        PaymentService.record_payment(
            bill=bill,
            user=self.member,
            amount=Decimal("500.00"),
            payment_method=PaymentMethod.UPI,
            transaction_reference="UPI-REF-101",
        )
        bill.refresh_from_db()

        self.client.force_login(self.member)
        response = self.client.get(f"/bills/{bill.id}/")
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode("utf-8")

        assert "Water Authority" in content
        assert "₹2000.00" in content
        assert "₹500.00" in content
        assert "₹1500.00" in content
        assert "25% Settled" in content
        assert "UPI-REF-101" in content
        assert "Partially Paid" in content

    def test_web_record_partial_and_full_payment_flow(self):
        """User can submit payment form on web UI with receipt; balances update until fully settled."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            provider_name="Electricity Board",
            amount=Decimal("1500.00"),
            due_date=today + timedelta(days=8),
        )

        receipt_pdf = SimpleUploadedFile("bill_receipt.pdf", b"%PDF-1.4\nPaid Slip\n%%EOF", content_type="application/pdf")

        # 1. Partial payment: ₹1,000
        self.client.force_login(self.member)
        post_data = {
            "action": "record_payment",
            "amount": "1000.00",
            "payment_method": PaymentMethod.UPI,
            "payment_date": str(today),
            "transaction_reference": "UPI/998877",
            "notes": "Paid first installment via PhonePe",
            "receipt_file": receipt_pdf,
        }
        res1 = self.client.post(f"/bills/{bill.id}/", post_data, follow=True)
        assert res1.status_code == status.HTTP_200_OK

        bill.refresh_from_db()
        assert bill.status == BillStatus.PARTIALLY_PAID
        assert bill.total_paid == Decimal("1000.00")
        assert bill.outstanding_balance == Decimal("500.00")
        payment = bill.payments.first()
        assert payment is not None
        assert payment.amount == Decimal("1000.00")
        assert hasattr(payment, "receipt")
        assert payment.receipt.file_name == "bill_receipt.pdf"

        # 2. Settle remaining balance: ₹500
        post_data_final = {
            "action": "record_payment",
            "amount": "500.00",
            "payment_method": PaymentMethod.CREDIT_CARD,
            "payment_date": str(today),
            "notes": "Remaining settled",
        }
        res2 = self.client.post(f"/bills/{bill.id}/", post_data_final, follow=True)
        assert res2.status_code == status.HTTP_200_OK

        bill.refresh_from_db()
        assert bill.status == BillStatus.PAID
        assert bill.total_paid == Decimal("1500.00")
        assert bill.outstanding_balance == Decimal("0.00")
        assert bill.payments.count() == 2

    def test_web_payment_adjustment_by_owner(self):
        """Household owner can record an adjustment / partial reversal via web action."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            provider_name="Maintenance Fee",
            amount=Decimal("4000.00"),
            due_date=today + timedelta(days=10),
        )
        payment = PaymentService.record_payment(bill=bill, user=self.member, amount=Decimal("4000.00"))
        bill.refresh_from_db()
        assert bill.status == BillStatus.PAID

        # Owner records adjustment: -1,500
        self.client.force_login(self.owner)
        adj_payload = {
            "action": "adjust_payment",
            "payment_id": str(payment.id),
            "amount": "-1500.00",
            "reason": "Security deposit portion refunded",
        }
        res_adj = self.client.post(f"/bills/{bill.id}/", adj_payload, follow=True)
        assert res_adj.status_code == status.HTTP_200_OK

        bill.refresh_from_db()
        assert bill.status == BillStatus.PARTIALLY_PAID
        assert bill.total_paid == Decimal("2500.00")
        assert bill.outstanding_balance == Decimal("1500.00")

        payment.refresh_from_db()
        assert payment.status == PaymentStatus.PARTIALLY_REFUNDED
        assert payment.net_amount == Decimal("2500.00")

    def test_web_payment_adjustment_rejected_for_non_owner(self):
        """Regular household member cannot adjust payments via web."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Clubhouse",
            amount=Decimal("2000.00"),
            due_date=today + timedelta(days=5),
        )
        payment = PaymentService.record_payment(bill=bill, user=self.member, amount=Decimal("2000.00"))

        self.client.force_login(self.member)
        adj_payload = {
            "action": "adjust_payment",
            "payment_id": str(payment.id),
            "amount": "-500.00",
            "reason": "Unauthorized refund attempt",
        }
        res = self.client.post(f"/bills/{bill.id}/", adj_payload, follow=True)
        assert res.status_code == status.HTTP_200_OK
        content = res.content.decode("utf-8")
        assert "Only household owners are authorized" in content

        # Bill remains fully paid
        bill.refresh_from_db()
        assert bill.status == BillStatus.PAID
        assert bill.outstanding_balance == Decimal("0.00")

    def test_web_download_receipt_tenant_isolation(self):
        """Household member can download receipt via web; stranger gets 404."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Gym Membership",
            amount=Decimal("1800.00"),
            due_date=today + timedelta(days=5),
        )
        payment = PaymentService.record_payment(bill=bill, user=self.member, amount=Decimal("1800.00"))
        receipt_file = SimpleUploadedFile("gym_receipt.pdf", b"%PDF-1.4\nGym fee\n%%EOF", content_type="application/pdf")
        PaymentService.attach_receipt(payment, self.member, receipt_file)

        # Member can download
        self.client.force_login(self.member)
        res_member = self.client.get(f"/payments/{payment.id}/receipt/")
        assert res_member.status_code == status.HTTP_200_OK

        # Stranger gets 404
        self.client.force_login(self.stranger)
        res_stranger = self.client.get(f"/payments/{payment.id}/receipt/")
        assert res_stranger.status_code == status.HTTP_404_NOT_FOUND

    def test_web_void_payment_by_owner(self):
        """Owner can completely void a payment via web UI."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            provider_name="Annual Subscription",
            amount=Decimal("2400.00"),
            due_date=today + timedelta(days=10),
        )
        payment = PaymentService.record_payment(bill=bill, user=self.member, amount=Decimal("2400.00"))
        bill.refresh_from_db()
        assert bill.status == BillStatus.PAID

        # Owner voids payment
        self.client.force_login(self.owner)
        void_payload = {
            "action": "void_payment",
            "payment_id": str(payment.id),
            "reason": "Payment failed at bank gateway after recording",
        }
        res_void = self.client.post(f"/bills/{bill.id}/", void_payload, follow=True)
        assert res_void.status_code == status.HTTP_200_OK

        bill.refresh_from_db()
        assert bill.status == BillStatus.PENDING
        assert bill.outstanding_balance == Decimal("2400.00")
        assert bill.total_paid == Decimal("0.00")

        payment.refresh_from_db()
        assert payment.status == PaymentStatus.VOIDED
        assert payment.net_amount == Decimal("0.00")
