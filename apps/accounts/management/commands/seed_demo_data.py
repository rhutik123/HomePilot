from datetime import timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillStatus, BillCategory
from apps.reminders.services.reminder_service import ReminderService

User = get_user_model()


class Command(BaseCommand):
    help = "Populates realistic demo users, households, bills, and reminders."

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Seeding HomePilot demo data..."))

        # 1. Create Demo Users
        alice, _ = User.objects.get_or_create(
            username="alice",
            defaults={
                "email": "alice@homepilot.local",
                "first_name": "Alice",
                "last_name": "Sharma",
                "phone_number": "+91-9876543210",
                "is_staff": True,
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
                "phone_number": "+91-9876543211",
            },
        )
        bob.set_password("Password123!")
        bob.save()

        # 2. Create Household
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

        # 3. Create Realistic Demo Bills
        today = timezone.localdate()

        bills_data = [
            {
                "provider_name": "Tata Power Delhi",
                "category": BillCategory.ELECTRICITY,
                "amount": Decimal("2450.00"),
                "currency": "INR",
                "billing_period": "October 2026",
                "due_date": today + timedelta(days=5),
                "reference_number": "ELE-9988231",
                "status": BillStatus.PENDING,
                "assigned_to": alice,
            },
            {
                "provider_name": "Airtel Xstream Fiber",
                "category": BillCategory.INTERNET,
                "amount": Decimal("1179.00"),
                "currency": "INR",
                "billing_period": "November 2026",
                "due_date": today + timedelta(days=12),
                "reference_number": "AIR-5544110",
                "status": BillStatus.PENDING,
                "assigned_to": bob,
            },
            {
                "provider_name": "Delhi Jal Board",
                "category": BillCategory.WATER,
                "amount": Decimal("480.00"),
                "currency": "INR",
                "billing_period": "Q3 2026",
                "due_date": today - timedelta(days=2),
                "reference_number": "DJB-0019283",
                "status": BillStatus.OVERDUE,
                "assigned_to": alice,
            },
            {
                "provider_name": "Indraprastha Gas Limited",
                "category": BillCategory.GAS,
                "amount": Decimal("865.00"),
                "currency": "INR",
                "billing_period": "September 2026",
                "due_date": today - timedelta(days=15),
                "reference_number": "IGL-8822199",
                "status": BillStatus.PAID,
                "paid_at": timezone.now() - timedelta(days=16),
                "assigned_to": bob,
            },
        ]

        for item in bills_data:
            bill, created = Bill.objects.get_or_create(
                household=household,
                provider_name=item["provider_name"],
                defaults={
                    "uploaded_by": alice,
                    "category": item["category"],
                    "amount": item["amount"],
                    "currency": item["currency"],
                    "billing_period": item["billing_period"],
                    "due_date": item["due_date"],
                    "reference_number": item["reference_number"],
                    "status": item["status"],
                    "paid_at": item.get("paid_at"),
                    "assigned_to": item["assigned_to"],
                },
            )
            if created and bill.status != BillStatus.PAID:
                ReminderService.create_reminders_for_bill(bill)

        self.stdout.write(self.style.SUCCESS("Demo data seeded successfully!"))
        self.stdout.write(self.style.SUCCESS("Demo credentials: alice / Password123! or bob / Password123!"))
