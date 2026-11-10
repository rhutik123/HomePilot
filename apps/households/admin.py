from django.contrib import admin
from .models import Household, HouseholdMembership


class HouseholdMembershipInline(admin.TabularInline):
    model = HouseholdMembership
    extra = 1
    autocomplete_fields = ["user"]


@admin.register(Household)
class HouseholdAdmin(admin.ModelAdmin):
    list_display = ("name", "created_by", "member_count", "created_at")
    search_fields = ("name", "created_by__username", "created_by__email")
    inlines = [HouseholdMembershipInline]


@admin.register(HouseholdMembership)
class HouseholdMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "household", "role", "joined_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__email", "household__name")
