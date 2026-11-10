import os
from datetime import timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillStatus, BillCategory
from apps.documents.models import Document, DocumentProcessingJob, ExtractionResult, BillDocumentLink
from apps.payments.models import Payment, PaymentMethod, PaymentAdjustment
from apps.recurring.models import RecurringSchedule, RecurringOccurrence, RecurrenceFrequency
from apps.reminders.models import (
    BillReminder,
    ReminderType,
    NotificationPreference,
    InAppNotification,
    NotificationDelivery,
    NotificationChannel,
)
from apps.reminders.services.reminder_service import ReminderService
from apps.ai.models import (
    AIConversation,
    AIMessage,
    AIToolExecution,
    AIUsageRecord,
    SpendingInsight,
)

User = get_user_model()


class Command(BaseCommand):
    help = "Populates a rich, realistic, full-spectrum dataset across all HomePilot domains."

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Seeding comprehensive HomePilot demo dataset..."))

        # ----------------------------------------------------------------------
        # 1. Users
        # ----------------------------------------------------------------------
        alice, _ = User.objects.get_or_create(
            username="alice",
            defaults={
                "email": "alice@homepilot.local",
                "first_name": "Alice",
                "last_name": "Sharma",
                "phone_number": "+919876543210",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        alice.set_password("Password123!")
        alice.save()

        bob, _ = User.objects.get_or_create(
            username="bob",
            defaults={
                "email": "bob@homepilot.local",
                "first_name": "Bob",
                "last_name": "Sharma",
                "phone_number": "+919876543211",
            },
        )
        bob.set_password("Password123!")
        bob.save()

        carol, _ = User.objects.get_or_create(
            username="carol",
            defaults={
                "email": "carol@homepilot.local",
                "first_name": "Carol",
                "last_name": "Sharma",
                "phone_number": "+919876543212",
            },
        )
        carol.set_password("Password123!")
        carol.save()

        # ----------------------------------------------------------------------
        # 2. Household & Memberships
        # ----------------------------------------------------------------------
        household, _ = Household.objects.get_or_create(
            name="The Sharma Family",
            defaults={"created_by": alice},
        )

        HouseholdMembership.objects.get_or_create(
            household=household,
            user=alice,
            defaults={"role": HouseholdMembership.Role.OWNER},
        )
        HouseholdMembership.objects.get_or_create(
            household=household,
            user=bob,
            defaults={"role": HouseholdMembership.Role.MEMBER},
        )
        HouseholdMembership.objects.get_or_create(
            household=household,
            user=carol,
            defaults={"role": HouseholdMembership.Role.MEMBER},
        )

        today = timezone.localdate()

        # ----------------------------------------------------------------------
        # 3. Documents & Extraction Pipeline
        # ----------------------------------------------------------------------
        doc1, _ = Document.objects.get_or_create(
            household=household,
            file_name="tata_power_oct2026.pdf",
            defaults={
                "uploaded_by": alice,
                "file_size": 245100,
                "file_hash": "a1b2c3d4e5f67890123456789abcdef0123456789abcdef0123456789abcdef0",
                "mime_type": "application/pdf",
            },
        )
        job1, _ = DocumentProcessingJob.objects.get_or_create(
            document=doc1,
            defaults={
                "status": DocumentProcessingJob.Status.SUCCESS,
                "attempts": 1,
                "completed_at": timezone.now(),
            },
        )
        ExtractionResult.objects.get_or_create(
            document=doc1,
            defaults={
                "job": job1,
                "raw_ocr_text": "Tata Power Delhi Distribution Limited\nBill Amount: 2450.00 INR\nDue Date: 2026-11-15\nBilling Period: October 2026",
                "extracted_data": {
                    "provider_name": "Tata Power Delhi",
                    "category": "ELECTRICITY",
                    "amount": 2450.00,
                    "currency": "INR",
                    "due_date": (today + timedelta(days=5)).isoformat(),
                    "billing_period": "October 2026",
                },
                "confidence_score": Decimal("0.96"),
                "provider_name": "google-gemini",
                "model_version": "gemini-2.5-flash",
            },
        )

        # ----------------------------------------------------------------------
        # 4. Master Bills Catalog (Historical + Current)
        # ----------------------------------------------------------------------
        bills_specs = [
            # Historical bills (Months ago for Analytics trend charts)
            {
                "provider": "Tata Power Delhi",
                "category": BillCategory.ELECTRICITY,
                "amount": Decimal("1850.00"),
                "paid": Decimal("1850.00"),
                "outstanding": Decimal("0.00"),
                "status": BillStatus.PAID,
                "due": today - timedelta(days=95),
                "assigned": alice,
            },
            {
                "provider": "Tata Power Delhi",
                "category": BillCategory.ELECTRICITY,
                "amount": Decimal("1920.00"),
                "paid": Decimal("1920.00"),
                "outstanding": Decimal("0.00"),
                "status": BillStatus.PAID,
                "due": today - timedelta(days=65),
                "assigned": alice,
            },
            {
                "provider": "Tata Power Delhi",
                "category": BillCategory.ELECTRICITY,
                "amount": Decimal("1900.00"),
                "paid": Decimal("1900.00"),
                "outstanding": Decimal("0.00"),
                "status": BillStatus.PAID,
                "due": today - timedelta(days=35),
                "assigned": alice,
            },
            {
                "provider": "Airtel Xstream Fiber",
                "category": BillCategory.INTERNET,
                "amount": Decimal("1179.00"),
                "paid": Decimal("1179.00"),
                "outstanding": Decimal("0.00"),
                "status": BillStatus.PAID,
                "due": today - timedelta(days=40),
                "assigned": bob,
            },
            {
                "provider": "Indraprastha Gas Limited",
                "category": BillCategory.GAS,
                "amount": Decimal("750.00"),
                "paid": Decimal("750.00"),
                "outstanding": Decimal("0.00"),
                "status": BillStatus.PAID,
                "due": today - timedelta(days=45),
                "assigned": carol,
            },
            # Current Active Bills
            {
                # Recent Spike: 3150 vs ~1890 avg (+66% spike triggers anomaly insight)
                "provider": "Tata Power Delhi",
                "category": BillCategory.ELECTRICITY,
                "amount": Decimal("3150.00"),
                "paid": Decimal("1500.00"),
                "outstanding": Decimal("1650.00"),
                "status": BillStatus.PARTIALLY_PAID,
                "due": today + timedelta(days=4),
                "assigned": alice,
            },
            {
                "provider": "Airtel Xstream Fiber",
                "category": BillCategory.INTERNET,
                "amount": Decimal("1179.00"),
                "paid": Decimal("0.00"),
                "outstanding": Decimal("1179.00"),
                "status": BillStatus.PENDING,
                "due": today + timedelta(days=10),
                "assigned": bob,
            },
            {
                "provider": "Delhi Jal Board",
                "category": BillCategory.WATER,
                "amount": Decimal("480.00"),
                "paid": Decimal("0.00"),
                "outstanding": Decimal("480.00"),
                "status": BillStatus.OVERDUE,
                "due": today - timedelta(days=3),
                "assigned": bob,
            },
            {
                "provider": "Indraprastha Gas Limited",
                "category": BillCategory.GAS,
                "amount": Decimal("865.00"),
                "paid": Decimal("865.00"),
                "outstanding": Decimal("0.00"),
                "status": BillStatus.PAID,
                "due": today - timedelta(days=12),
                "assigned": carol,
            },
            {
                "provider": "Resident Welfare Maintenance",
                "category": BillCategory.OTHER,
                "amount": Decimal("2500.00"),
                "paid": Decimal("2500.00"),
                "outstanding": Decimal("0.00"),
                "status": BillStatus.PAID,
                "due": today - timedelta(days=18),
                "assigned": alice,
            },
        ]

        created_bills = []
        for spec in bills_specs:
            b, _ = Bill.objects.get_or_create(
                household=household,
                provider_name=spec["provider"],
                due_date=spec["due"],
                defaults={
                    "uploaded_by": alice,
                    "assigned_to": spec["assigned"],
                    "category": spec["category"],
                    "amount": spec["amount"],
                    "total_paid": spec["paid"],
                    "outstanding_balance": spec["outstanding"],
                    "status": spec["status"],
                    "currency": "INR",
                    "billing_period": spec["due"].strftime("%B %Y"),
                    "issue_date": spec["due"] - timedelta(days=20),
                    "reference_number": f"REF-{spec['provider'][:3].upper()}-{spec['due'].strftime('%m%d')}",
                },
            )
            created_bills.append(b)

            # Auto-schedule reminders for pending/partially paid/overdue bills
            if b.status != BillStatus.PAID:
                ReminderService.create_reminders_for_bill(b)

        # Link document to primary active electricity bill
        active_elec_bill = next((b for b in created_bills if b.provider_name == "Tata Power Delhi" and b.status == BillStatus.PARTIALLY_PAID), None)
        if active_elec_bill:
            BillDocumentLink.objects.get_or_create(
                bill=active_elec_bill,
                document=doc1,
            )

        # ----------------------------------------------------------------------
        # 5. Payments, Partial Payments & Adjustments
        # ----------------------------------------------------------------------
        # Partial payment on active electricity bill
        if active_elec_bill:
            p_partial, _ = Payment.objects.get_or_create(
                bill=active_elec_bill,
                amount=Decimal("1500.00"),
                defaults={
                    "household": household,
                    "paid_by": alice,
                    "payment_date": today - timedelta(days=1),
                    "payment_method": PaymentMethod.UPI,
                    "transaction_reference": "UPI/9988223311/AXIS",
                    "notes": "Paid first installment via Google Pay.",
                },
            )

        # Paid gas bill with correction adjustment record
        gas_bill = next((b for b in created_bills if b.provider_name == "Indraprastha Gas Limited" and b.status == BillStatus.PAID), None)
        if gas_bill:
            p_gas, _ = Payment.objects.get_or_create(
                bill=gas_bill,
                amount=Decimal("865.00"),
                defaults={
                    "household": household,
                    "paid_by": carol,
                    "payment_date": today - timedelta(days=12),
                    "payment_method": PaymentMethod.NET_BANKING,
                    "transaction_reference": "NETB/7711223344",
                    "notes": "Direct net banking payment from HDFC account.",
                },
            )
            PaymentAdjustment.objects.get_or_create(
                payment=p_gas,
                adjusted_by=alice,
                defaults={
                    "amount": Decimal("15.00"),
                    "reason": "Reconciled against bank statement: included 15 INR payment gateway charge.",
                },
            )

        # ----------------------------------------------------------------------
        # 6. Recurring Schedules & Generated Occurrences
        # ----------------------------------------------------------------------
        rec_internet, _ = RecurringSchedule.objects.get_or_create(
            household=household,
            provider_name="Airtel Xstream Fiber",
            defaults={
                "created_by": alice,
                "assigned_to": bob,
                "category": BillCategory.INTERNET,
                "expected_amount": Decimal("1179.00"),
                "currency": "INR",
                "frequency": RecurrenceFrequency.MONTHLY,
                "interval_count": 1,
                "day_of_month": 10,
                "next_due_date": today + timedelta(days=10),
                "is_active": True,
                "notes": "300 Mbps Unlimited Fiber Plan.",
            },
        )
        rec_water, _ = RecurringSchedule.objects.get_or_create(
            household=household,
            provider_name="Delhi Jal Board",
            defaults={
                "created_by": alice,
                "assigned_to": bob,
                "category": BillCategory.WATER,
                "expected_amount": Decimal("480.00"),
                "currency": "INR",
                "frequency": RecurrenceFrequency.QUARTERLY,
                "interval_count": 1,
                "day_of_month": 15,
                "next_due_date": today + timedelta(days=70),
                "is_active": True,
            },
        )
        # Recurring occurrence tracking
        if active_elec_bill:
            RecurringOccurrence.objects.get_or_create(
                schedule=rec_internet,
                period_identifier=(today - timedelta(days=40)).strftime("%Y-%m"),
                defaults={"bill": next(b for b in created_bills if b.provider_name == "Airtel Xstream Fiber" and b.status == BillStatus.PAID)},
            )

        # ----------------------------------------------------------------------
        # 7. Notification Preferences, Deliveries & In-App Alerts
        # ----------------------------------------------------------------------
        NotificationPreference.objects.update_or_create(
            user=alice,
            defaults={
                "email_enabled": True,
                "in_app_enabled": True,
                "whatsapp_enabled": True,
                "whatsapp_phone_number": "+919876543210",
            },
        )
        NotificationPreference.objects.update_or_create(
            user=bob,
            defaults={
                "email_enabled": True,
                "in_app_enabled": True,
                "whatsapp_enabled": False,
            },
        )

        # In-App Alerts Inbox
        if active_elec_bill:
            InAppNotification.objects.get_or_create(
                user=alice,
                bill=active_elec_bill,
                title="Partial Payment Received: Tata Power Delhi",
                defaults={
                    "message": "Payment of 1,500.00 INR recorded. Remaining balance: 1,650.00 INR due in 4 days.",
                    "is_read": False,
                },
            )
        water_overdue_bill = next((b for b in created_bills if b.status == BillStatus.OVERDUE), None)
        if water_overdue_bill:
            InAppNotification.objects.get_or_create(
                user=bob,
                bill=water_overdue_bill,
                title="⚠️ Overdue Alert: Delhi Jal Board",
                defaults={
                    "message": "Your water bill of 480.00 INR was due 3 days ago. Please pay immediately.",
                    "is_read": False,
                },
            )

        # Multi-Channel Delivery Audit Log
        if active_elec_bill:
            rem = active_elec_bill.reminders.first()
            if rem:
                NotificationDelivery.objects.get_or_create(
                    reminder=rem,
                    channel=NotificationChannel.EMAIL,
                    defaults={
                        "bill": active_elec_bill,
                        "recipient": alice,
                        "status": NotificationDelivery.Status.SUCCESS,
                        "recipient_address": "alice@homepilot.local",
                        "subject": "HomePilot Reminder: Tata Power Delhi due soon",
                        "provider_response_id": "smtp_deliv_77192",
                    },
                )
                NotificationDelivery.objects.get_or_create(
                    reminder=rem,
                    channel=NotificationChannel.WHATSAPP,
                    defaults={
                        "bill": active_elec_bill,
                        "recipient": alice,
                        "status": NotificationDelivery.Status.SUCCESS,
                        "recipient_address": "+919876543210",
                        "subject": "HomePilot Milestone: THREE_DAYS_BEFORE",
                        "provider_response_id": "wa_mock_msg_88129",
                    },
                )

        # ----------------------------------------------------------------------
        # 8. AI Conversations, Tool Executions & Spending Insights
        # ----------------------------------------------------------------------
        ai_conv, _ = AIConversation.objects.get_or_create(
            household=household,
            user=alice,
            title="October Expense Review & Planning",
        )
        msg_user, _ = AIMessage.objects.get_or_create(
            conversation=ai_conv,
            role=AIMessage.Role.USER,
            content="What are my currently unpaid and upcoming household bills?",
        )
        AIMessage.objects.get_or_create(
            conversation=ai_conv,
            role=AIMessage.Role.ASSISTANT,
            defaults={
                "content": (
                    f"Household '{household.name}' currently has 3 pending or partially paid bills totaling "
                    f"3,309.00 INR outstanding: \n"
                    f"1. Tata Power Delhi: 1,650.00 INR remaining (Due in 4 days)\n"
                    f"2. Airtel Xstream Fiber: 1,179.00 INR (Due in 10 days)\n"
                    f"3. Delhi Jal Board: 480.00 INR (OVERDUE by 3 days)\n\n"
                    f"Please prioritize the overdue water bill to avoid service disruption."
                )
            },
        )
        AIToolExecution.objects.get_or_create(
            conversation=ai_conv,
            message=msg_user,
            tool_name="get_unpaid_bills",
            defaults={
                "tool_arguments": {},
                "execution_result": {
                    "household": household.name,
                    "unpaid_bills_count": 3,
                    "total_outstanding_amount": "3309.00",
                    "currency": "INR",
                },
                "is_success": True,
            },
        )
        AIUsageRecord.objects.get_or_create(
            household=household,
            user=alice,
            provider="google-gemini",
            model_name="gemini-2.5-flash",
            operation_type="CHAT_ASSISTANT",
            defaults={
                "prompt_tokens": 280,
                "completion_tokens": 120,
                "total_tokens": 400,
                "estimated_cost_usd": Decimal("0.00012"),
            },
        )

        # Spending Anomaly Insights
        SpendingInsight.objects.get_or_create(
            household=household,
            insight_type=SpendingInsight.InsightType.EXPENSE_SPIKE,
            category=BillCategory.ELECTRICITY,
            defaults={
                "title": "Unusual Electricity Bill Spike: Tata Power Delhi",
                "description": (
                    "Your recent electricity bill of 3,150.00 INR is 1,260.00 INR (+66%) higher than your "
                    "historical 3-month average of 1,890.00 INR. Check AC appliance usage or meter reading."
                ),
                "severity": SpendingInsight.Severity.WARNING,
                "amount_difference": Decimal("1260.00"),
                "is_dismissed": False,
            },
        )
        SpendingInsight.objects.get_or_create(
            household=household,
            insight_type=SpendingInsight.InsightType.UPCOMING_EXPENSE_FORECAST,
            defaults={
                "title": f"Upcoming 30-Day Household Expenses ({today.strftime('%B %Y')})",
                "description": "You have 2 upcoming bills and 1 overdue bill totaling 3,309.00 INR due across your household.",
                "severity": SpendingInsight.Severity.INFO,
                "amount_difference": Decimal("3309.00"),
                "is_dismissed": False,
            },
        )

        self.stdout.write(self.style.SUCCESS("Comprehensive HomePilot demo data seeded successfully!"))
        self.stdout.write(self.style.SUCCESS("Household: The Sharma Family"))
        self.stdout.write(self.style.SUCCESS("Login credentials:"))
        self.stdout.write(self.style.SUCCESS("  - Alice (Owner / Admin): alice / Password123!"))
        self.stdout.write(self.style.SUCCESS("  - Bob   (Member):        bob   / Password123!"))
        self.stdout.write(self.style.SUCCESS("  - Carol (Member):        carol / Password123!"))
