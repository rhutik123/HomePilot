# WhatsApp Business Platform Setup & Integration Guide

This guide details the exact prerequisites, credentials, and verification steps required to enable official WhatsApp notifications in **HomePilot** via Meta's WhatsApp Cloud API.

---

## 1. Prerequisites & Account Setup

1. **Meta for Developers Account:**
   - Register at [developers.facebook.com](https://developers.facebook.com/).
   - Create a new App of type **Business**.
2. **Meta Business Portfolio (Business Manager):**
   - The developer app must be linked to a verified Meta Business Account.
   - Complete Meta Business Verification (requires business license, utility bill, or tax ID).
3. **WhatsApp Business Account (WABA):**
   - Within your Business Manager, add a WhatsApp Business Account.
   - Link a dedicated phone number (must NOT be registered on standard consumer WhatsApp or WhatsApp Business mobile app).

---

## 2. Message Templates & Approval Process

Meta strictly enforces that business-initiated messages (such as bill payment reminders) sent outside a 24-hour customer service window **must** use pre-approved Message Templates.

### Sample Template Specification:
- **Template Name:** `homepilot_bill_reminder`
- **Category:** `UTILITY`
- **Languages:** `en_US` (or localized variants)
- **Header:** None or Text (`HomePilot Bill Reminder`)
- **Body:**
  ```text
  Hello {{1}}, this is a payment reminder from HomePilot for household '{{2}}'.
  Your {{3}} bill of {{4}} {{5}} is due on {{6}}.
  Please record your payment in the HomePilot dashboard once completed.
  ```
- **Buttons / Quick Actions:**
  - URL Button: `View Bill` -> `https://your-homepilot-domain.com/bills/{{7}}/`

### Meta Review Timeline:
Template submissions are evaluated by Meta's automated and manual compliance review, typically requiring between 1 to 24 hours for approval.

---

## 3. Permanent System User Access Token

To prevent token expiry:
1. In Meta Business Settings -> **System Users**, create an Admin System User (e.g., `homepilot-worker`).
2. Generate a token with the following permissions:
   - `whatsapp_business_messaging`
   - `whatsapp_business_management`
3. Store the token securely as `WHATSAPP_API_TOKEN`.

---

## 4. Webhook Configuration (Delivery Receipts)

To track message statuses (`sent`, `delivered`, `read`, `failed`):
1. In the WhatsApp App Dashboard -> Configuration, set Callback URL:
   `https://your-homepilot-domain.com/api/v1/reminders/webhooks/whatsapp/`
2. Configure a Verify Token and subscribe to the `messages` webhook field.

---

## 5. HomePilot Environment Configuration

Set the following variables in your `.env` configuration:

```env
# Official WhatsApp Cloud API Credentials
WHATSAPP_API_TOKEN=EAAxxxx...
WHATSAPP_PHONE_NUMBER_ID=109876543210xxx
WHATSAPP_BUSINESS_ACCOUNT_ID=102345678901xxx
WHATSAPP_TEMPLATE_NAME=homepilot_bill_reminder

# Testing & Sandbox Simulation Flag
# Set to 'True' only during development/testing when live Meta credentials are unavailable
WHATSAPP_SIMULATION_ENABLED=False
```

When `WHATSAPP_SIMULATION_ENABLED` is `False` and credentials are unconfigured, HomePilot safely logs unconfigured warnings and reports failure rather than falsely indicating delivery to users.
