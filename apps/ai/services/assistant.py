import logging
import re
from decimal import Decimal
from typing import Tuple, List, Dict, Any, Optional
from django.conf import settings
from django.utils import timezone

from apps.ai.models import AIConversation, AIMessage, AIToolExecution, AIUsageRecord
from apps.ai.services.tools import execute_tool_call, TOOL_REGISTRY
from apps.bills.models import Bill, BillCategory

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are HomePilot AI, a knowledgeable, accurate household financial assistant.
Your job is to help families and household members track, explain, and manage their utility and living expenses.

CRITICAL ARCHITECTURAL MANDATES:
1. All financial numbers, totals, and balances MUST be derived from tool results. NEVER fabricate, estimate, or guess monetary figures.
2. If the user asks about bills, spending, due dates, payments, or subscriptions, ALWAYS use the provided tools to fetch real data before answering.
3. Be clear, encouraging, and actionable. Alert the user to overdue items and upcoming payment milestones.
4. Respect household security boundaries. Only discuss financial data returned by the household-scoped tools.
"""


def _extract_quantity_limit(prompt: str) -> Optional[int]:
    """
    Extracts requested quantity/limit from user prompt (e.g. 'show 1 bill', 'top 3',
    'only any one', 'just one', 'first 3', 'all').
    """
    prompt_clean = prompt.lower().strip()

    # Explicit request for all items
    if re.search(r"\b(?:all|every|complete\s+list)\b", prompt_clean):
        return None

    # Compound single expressions: "only any one", "any one", "only one", "just one", "a single"
    if re.search(r"\b(?:only\s+any\s+one|any\s+one|only\s+one|just\s+one|a\s+single|just\s+1|only\s+1)\b", prompt_clean):
        return 1

    words_to_num = {
        "one": 1,
        "single": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }

    # Match numeric patterns: "first 3", "top 5", "show 1", "3 bills", "limit 5"
    match_digits = re.search(
        r"\b(?:top|first|last|show|list|get|give me|only|just|any)?\s*(\d+)\s*(?:upcoming|overdue|unpaid|recent|recurring)?\s*(?:bills?|payments?|schedules?|items?|subscriptions?)?\b",
        prompt_clean,
    )
    if match_digits and match_digits.group(1):
        try:
            val = int(match_digits.group(1))
            if 1 <= val <= 100:
                return val
        except Exception:
            pass

    # Check words: "one", "two", "three", etc.
    for word, num in words_to_num.items():
        if re.search(
            rf"\b(?:top|first|last|show|list|get|give me|only|just|any)?\s*{word}\s*(?:upcoming|overdue|unpaid|recent|recurring)?\s*(?:bills?|payments?|schedules?|items?|subscriptions?)?\b",
            prompt_clean,
        ):
            return num

    return None


class AIAssistantService:
    """
    Orchestrates stateful natural language conversations with HomePilot AI.
    Executes controlled tools within household security boundaries, enforces
    deterministic financial calculations, and logs token usage.
    """
    @classmethod
    def send_message(
        cls,
        conversation: AIConversation,
        user,
        prompt: str,
    ) -> Tuple[AIMessage, List[AIToolExecution]]:
        household = conversation.household

        # 1. Save incoming user message
        user_msg = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.USER,
            content=prompt,
        )

        api_key = getattr(settings, "GEMINI_API_KEY", "") or ""
        backend = str(getattr(settings, "AI_ASSISTANT_BACKEND", getattr(settings, "BILL_EXTRACTION_BACKEND", "mock"))).lower()

        # 2. Check if live Google Gemini client should be used
        if backend in ("gemini", "google", "llm"):
            if not api_key:
                err_text = (
                    "Google Gemini AI Assistant is configured (`AI_ASSISTANT_BACKEND=gemini`), "
                    "but `GEMINI_API_KEY` is missing from the environment settings. "
                    "Please set your `GEMINI_API_KEY` in `.env` or set `AI_ASSISTANT_BACKEND=mock` for offline operation."
                )
                assistant_msg = AIMessage.objects.create(
                    conversation=conversation,
                    role=AIMessage.Role.ASSISTANT,
                    content=err_text,
                )
                return assistant_msg, []
            return cls._call_gemini_assistant(conversation, user, user_msg, prompt)
        else:
            return cls._call_mock_assistant(conversation, user, user_msg, prompt)

    @classmethod
    def _call_gemini_assistant(
        cls,
        conversation: AIConversation,
        user,
        user_msg: AIMessage,
        prompt: str,
    ) -> Tuple[AIMessage, List[AIToolExecution]]:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=settings.GEMINI_API_KEY)
            household = conversation.household

            # Build history from previous messages
            contents = []
            for msg in conversation.messages.order_by("created_at")[:15]:
                role = "user" if msg.role == AIMessage.Role.USER else "model"
                contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg.content)]))

            # Register tools
            tools = [
                types.Tool(function_declarations=[
                    types.FunctionDeclaration(
                        name="get_upcoming_bills",
                        description="Fetches upcoming bills due within the specified number of days (default 30).",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "days": types.Schema(type=types.Type.INTEGER, description="Days ahead to check"),
                                "only_assigned_to_me": types.Schema(type=types.Type.BOOLEAN, description="Filter only bills assigned to the calling user"),
                                "limit": types.Schema(type=types.Type.INTEGER, description="Maximum number of bills to return"),
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="get_overdue_bills",
                        description="Fetches all household bills past their due date that have outstanding balances.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "only_assigned_to_me": types.Schema(type=types.Type.BOOLEAN, description="Filter only bills assigned to the calling user"),
                                "limit": types.Schema(type=types.Type.INTEGER, description="Maximum number of bills to return"),
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="get_household_spending",
                        description="Calculates exact total spent, paid, and outstanding for a given month or the current calendar month.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "year": types.Schema(type=types.Type.INTEGER, description="Year (e.g. 2026)"),
                                "month": types.Schema(type=types.Type.INTEGER, description="Month (1-12)"),
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="get_category_spending",
                        description="Calculates spending history for a specific category (e.g. ELECTRICITY, WATER, INTERNET) over the past N months.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "category": types.Schema(type=types.Type.STRING, description="Category name (e.g. ELECTRICITY, WATER, INTERNET)"),
                                "months": types.Schema(type=types.Type.INTEGER, description="Number of months to analyze"),
                            },
                            required=["category"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="get_provider_spending",
                        description="Analyzes spending and rate changes for a specific service provider over recent months.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "provider_name": types.Schema(type=types.Type.STRING, description="Provider name to analyze"),
                                "months": types.Schema(type=types.Type.INTEGER, description="Number of months to analyze"),
                            },
                            required=["provider_name"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="get_payment_summary",
                        description="Fetches recent payment transactions recorded in the household with receipt status.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "limit": types.Schema(type=types.Type.INTEGER, description="Maximum number of payments to return"),
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="get_recurring_bills",
                        description="Analyzes active recurring bill schedules and calculates total monthly recurring commitments.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "limit": types.Schema(type=types.Type.INTEGER, description="Maximum number of schedules to return"),
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="get_unpaid_bills",
                        description="Fetches all unpaid or partially paid bills for the household.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "only_assigned_to_me": types.Schema(type=types.Type.BOOLEAN, description="Filter only bills assigned to the calling user"),
                                "limit": types.Schema(type=types.Type.INTEGER, description="Maximum number of bills to return"),
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="explain_bill",
                        description="Inspects detailed payment history, receipts, and attached files for a single bill.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={"bill_id": types.Schema(type=types.Type.INTEGER, description="ID of the bill")},
                            required=["bill_id"],
                        ),
                    ),
                ])
            ]

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=tools,
                    temperature=0.2,
                ),
            )

            tool_executions = []
            if response.function_calls:
                for fc in response.function_calls:
                    fname = fc.name
                    fargs = dict(fc.args) if fc.args else {}
                    tool_result = execute_tool_call(fname, fargs, household, user)

                    exec_record = AIToolExecution.objects.create(
                        conversation=conversation,
                        message=user_msg,
                        tool_name=fname,
                        tool_arguments=fargs,
                        execution_result=tool_result,
                        is_success="error" not in tool_result,
                        error_message=tool_result.get("error", ""),
                    )
                    tool_executions.append(exec_record)

                tool_response_parts = [
                    types.Part.from_function_response(
                        name=te.tool_name,
                        response={"result": te.execution_result},
                    )
                    for te in tool_executions
                ]
                final_response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=contents + [response.candidates[0].content, types.Content(parts=tool_response_parts)],
                    config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.2),
                )
                assistant_text = final_response.text
                prompt_toks = getattr(final_response.usage_metadata, "prompt_token_count", 250) or 250
                compl_toks = getattr(final_response.usage_metadata, "candidates_token_count", 150) or 150
            else:
                assistant_text = response.text or "I have reviewed your request."
                prompt_toks = getattr(response.usage_metadata, "prompt_token_count", 150) or 150
                compl_toks = getattr(response.usage_metadata, "candidates_token_count", 80) or 80

            total_toks = prompt_toks + compl_toks
            AIUsageRecord.objects.create(
                household=household,
                user=user,
                provider="google-gemini",
                model_name="gemini-2.5-flash",
                prompt_tokens=prompt_toks,
                completion_tokens=compl_toks,
                total_tokens=total_toks,
                estimated_cost_usd=Decimal(str(total_toks * 0.0000003)),
                operation_type="CHAT_ASSISTANT",
            )

            assistant_msg = AIMessage.objects.create(
                conversation=conversation,
                role=AIMessage.Role.ASSISTANT,
                content=assistant_text,
            )
            for te in tool_executions:
                te.message = assistant_msg
                te.save(update_fields=["message"])

            return assistant_msg, tool_executions

        except Exception as exc:
            logger.exception(f"Gemini assistant error: {exc}.")
            allow_fallback = getattr(settings, "AI_ASSISTANT_FALLBACK_TO_MOCK", False)
            if allow_fallback:
                logger.warning("Falling back to deterministic mock assistant due to Gemini failure.")
                fallback_msg, tools = cls._call_mock_assistant(conversation, user, user_msg, prompt)
                fallback_msg.content = f"*⚠️ Notice: Gemini AI service was unavailable ({exc.__class__.__name__}). Displaying verified offline calculations:*\n\n" + fallback_msg.content
                fallback_msg.save(update_fields=["content"])
                return fallback_msg, tools

            err_text = f"The Gemini AI Assistant encountered an error while processing your request: {str(exc)}. Please check your internet connection or API quota."
            assistant_msg = AIMessage.objects.create(
                conversation=conversation,
                role=AIMessage.Role.ASSISTANT,
                content=err_text,
            )
            return assistant_msg, []

    @classmethod
    def _call_mock_assistant(
        cls,
        conversation: AIConversation,
        user,
        user_msg: AIMessage,
        prompt: str,
    ) -> Tuple[AIMessage, List[AIToolExecution]]:
        """
        Deterministic offline assistant handler for tests and local development.
        Directly executes the corresponding household financial tool and returns
        truthful facts calculated by Django ORM / Decimal arithmetic.
        """
        household = conversation.household
        prompt_lower = prompt.lower().strip()
        tool_executions = []
        assistant_content = ""

        # Intent 1: Bill explanation
        bill_id_match = re.search(r"bill\s*#?(\d+)", prompt_lower)
        if "explain" in prompt_lower:
            target_bill = None
            if bill_id_match:
                bill_id = int(bill_id_match.group(1))
                target_bill = Bill.objects.filter(household=household, id=bill_id).first()
            else:
                # Check category mention (e.g. "explain my electricity bill")
                matched_cat = None
                for c_val, c_lbl in BillCategory.choices:
                    if c_val.lower() in prompt_lower or c_lbl.lower() in prompt_lower:
                        matched_cat = c_val
                        break
                if matched_cat:
                    target_bill = Bill.objects.filter(household=household, category=matched_cat).order_by("-due_date").first()
                else:
                    for p in ["tata", "airtel", "bses", "adani", "bescom", "water", "gas", "netflix"]:
                        if p in prompt_lower:
                            target_bill = Bill.objects.filter(household=household, provider_name__icontains=p).order_by("-due_date").first()
                            break

            if target_bill:
                res = execute_tool_call("explain_bill", {"bill_id": target_bill.id}, household, user)
                exec_rec = AIToolExecution.objects.create(
                    conversation=conversation,
                    message=user_msg,
                    tool_name="explain_bill",
                    tool_arguments={"bill_id": target_bill.id},
                    execution_result=res,
                    is_success="error" not in res,
                    error_message=res.get("error", ""),
                )
                tool_executions.append(exec_rec)
                assistant_content = (
                    f"**Bill #{res['bill_id']} ({res['provider_name']})**\n\n"
                    f"- **Total Amount:** {res['amount']} {res['currency']}\n"
                    f"- **Outstanding Balance:** {res['outstanding_balance']} {res['currency']}\n"
                    f"- **Status:** `{res['status']}`\n"
                    f"- **Assigned To:** {res['assigned_to']}\n"
                    f"- **Due Date:** {res['due_date'] or 'N/A'}\n"
                    f"- **Payments Recorded:** {res['payments_count']}\n"
                    f"- **Attached Documents:** {res['attached_documents_count']}"
                )
            else:
                assistant_content = (
                    "Could not find a matching bill in your household records to explain. "
                    "Please specify the bill number (e.g. *'Explain bill #5'*) or the service name (e.g. *'Explain my electricity bill'*)."
                )

        # Intent 2: Overdue bills
        elif any(k in prompt_lower for k in ["overdue", "past due", "late bills", "late bill"]):
            only_me = any(k in prompt_lower for k in ["my", "assigned to me", "for me"])
            limit = _extract_quantity_limit(prompt)
            tool_args = {"only_assigned_to_me": only_me}
            if limit:
                tool_args["limit"] = limit
            res = execute_tool_call("get_overdue_bills", tool_args, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_overdue_bills",
                tool_arguments=tool_args,
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            total = res["total_overdue_amount"]
            count = len(res["bills"])
            total_count = res.get("total_count", count)
            scope = "assigned to you" if only_me else "in the household"
            if total_count == 0:
                assistant_content = f"Great news! There are currently no overdue bills {scope}."
            else:
                items = [f"- **{b['provider_name']}** ({b['category_display']}): **{b['outstanding_balance']} INR** (overdue by {b['days_overdue']} days, due {b['due_date']})" for b in res["bills"]]
                items_str = "\n".join(items)
                if limit and limit < total_count:
                    header = f"Showing **{count} of {total_count} overdue bills** {scope} totaling **{total} INR**:\n\n{items_str}"
                else:
                    header = f"There {'is' if total_count == 1 else 'are'} **{total_count} overdue bill{'s' if total_count != 1 else ''}** {scope} totaling **{total} INR**:\n\n{items_str}"
                assistant_content = header

        # Intent 3: Upcoming bills
        elif any(k in prompt_lower for k in ["upcoming", "due next", "due soon", "coming up"]):
            only_me = any(k in prompt_lower for k in ["my", "assigned to me", "for me"])
            days = 7 if "week" in prompt_lower else 30
            limit = _extract_quantity_limit(prompt)
            tool_args = {"days": days, "only_assigned_to_me": only_me}
            if limit:
                tool_args["limit"] = limit
            res = execute_tool_call("get_upcoming_bills", tool_args, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_upcoming_bills",
                tool_arguments=tool_args,
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            total = res["total_upcoming_amount"]
            count = len(res["bills"])
            total_count = res.get("total_count", count)
            scope = "assigned to you" if only_me else "for household"
            if total_count == 0:
                assistant_content = f"You have no upcoming bills due in the next {days} days."
            else:
                items = [f"- **{b['provider_name']}** ({b['category_display']}): **{b['outstanding_balance']} INR** (due {b['due_date']}, {b['days_until_due']} days remaining)" for b in res["bills"]]
                items_str = "\n".join(items)
                if limit and limit < total_count:
                    header = f"Showing **{count} of {total_count} upcoming bills** {scope} due in the next {days} days (totaling **{total} INR**):\n\n{items_str}"
                else:
                    header = f"You have **{total_count} upcoming bill{'s' if total_count != 1 else ''}** {scope} due in the next {days} days totaling **{total} INR**:\n\n{items_str}"
                assistant_content = header

        # Intent 4: Total Outstanding Question (e.g. "How much is outstanding?", "How much do I owe?")
        elif any(k in prompt_lower for k in ["how much is outstanding", "how much do i owe", "what is my balance", "what is outstanding", "how much outstanding", "total outstanding balance", "total balance"]):
            only_me = any(k in prompt_lower for k in ["my", "do i owe", "for me"])
            tool_args = {"only_assigned_to_me": only_me}
            res = execute_tool_call("get_unpaid_bills", tool_args, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_unpaid_bills",
                tool_arguments=tool_args,
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            total = res["total_outstanding_amount"]
            count = res["unpaid_bills_count"]
            scope = "assigned to you" if only_me else "across the household"
            if count == 0:
                assistant_content = f"You have zero outstanding balance {scope}. All recorded bills have been settled!"
            else:
                assistant_content = (
                    f"The total outstanding balance {scope} is **{total} INR** across **{count} unpaid bill{'s' if count != 1 else ''}**."
                )

        # Intent 5: Unpaid bills listing (e.g. "Show me one unpaid bill", "Show me only any one unpaid bill", "Show all unpaid bills")
        elif any(k in prompt_lower for k in ["unpaid", "pending bills", "bills to pay", "pending bill"]):
            only_me = any(k in prompt_lower for k in ["my", "assigned to me", "for me", "only my"])
            limit = _extract_quantity_limit(prompt)
            tool_args = {"only_assigned_to_me": only_me}
            if limit:
                tool_args["limit"] = limit
            res = execute_tool_call("get_unpaid_bills", tool_args, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_unpaid_bills",
                tool_arguments=tool_args,
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            count = len(res["bills"])
            total_count = res.get("unpaid_bills_count", count)
            total = res["total_outstanding_amount"]
            scope = "assigned to you" if only_me else "in the household"
            if total_count == 0:
                assistant_content = f"There are currently no unpaid bills {scope}."
            else:
                items = [f"- **{b['provider_name']}** ({b['category_display']}): **{b['outstanding_balance']} INR** (status: `{b['status']}`, due {b['due_date']})" for b in res["bills"]]
                items_str = "\n".join(items)
                if limit and limit < total_count:
                    header = f"Showing **{count} of {total_count} unpaid bills** {scope} (Total outstanding: **{total} INR**):\n\n{items_str}"
                else:
                    header = f"Found **{total_count} unpaid bill{'s' if total_count != 1 else ''}** {scope} totaling **{total} INR**:\n\n{items_str}"
                assistant_content = header

        # Intent 6: Category spending / history
        elif any(c in prompt_lower for c in ["electricity", "water", "internet", "gas", "rent", "groceries", "insurance"]):
            # Detect category
            matched_cat = "OTHER"
            for c_val, c_lbl in BillCategory.choices:
                if c_val.lower() in prompt_lower or c_lbl.lower() in prompt_lower:
                    matched_cat = c_val
                    break

            months = 1 if "last month" in prompt_lower else 6
            res = execute_tool_call("get_category_spending", {"category": matched_cat, "months": months}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_category_spending",
                tool_arguments={"category": matched_cat, "months": months},
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            cat_display = res["category_display"]
            total = res["total_spent"]
            avg = res["average_spent"]
            count = res["bills_count"]
            assistant_content = (
                f"Household spend on **{cat_display}** over the last {months} month{'s' if months != 1 else ''} "
                f"totals **{total} INR** across {count} bill{'s' if count != 1 else ''} (averaging **{avg} INR** per bill)."
            )

        # Intent 7: Recurring bills / subscriptions
        elif any(k in prompt_lower for k in ["recurring", "subscription", "subscriptions", "autopay"]):
            limit = _extract_quantity_limit(prompt)
            tool_args = {}
            if limit:
                tool_args["limit"] = limit
            res = execute_tool_call("get_recurring_bills", tool_args, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_recurring_bills",
                tool_arguments=tool_args,
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            count = len(res["schedules"])
            total_count = res.get("active_schedules_count", count)
            monthly_total = res["estimated_monthly_recurring_total"]
            if total_count == 0:
                assistant_content = "There are currently no active recurring schedules configured for this household."
            else:
                items = [f"- **{s['provider_name']}** ({s['frequency_display']}): {s['expected_amount']} INR (~{s['monthly_equivalent']} INR/mo, next due: {s['next_due_date']})" for s in res["schedules"]]
                items_str = "\n".join(items)
                if limit and limit < total_count:
                    header = f"Showing **{count} of {total_count} active recurring schedules** totaling **{monthly_total} INR/month**:\n\n{items_str}"
                else:
                    header = f"Household has **{total_count} active recurring schedule{'s' if total_count != 1 else ''}** totaling **{monthly_total} INR/month**:\n\n{items_str}"
                assistant_content = header

        # Intent 8: Payment summary / receipts
        elif any(k in prompt_lower for k in ["payment", "payments", "receipt", "receipts", "settled"]):
            limit = _extract_quantity_limit(prompt) or 5
            tool_args = {"limit": limit}
            res = execute_tool_call("get_payment_summary", tool_args, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_payment_summary",
                tool_arguments=tool_args,
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            total_settled = res["total_amount_settled"]
            count = res["total_payments_count"]
            if count == 0:
                assistant_content = "No payment records found for this household."
            else:
                items = [f"- **{p['provider_name']}**: **{p['amount']} INR** paid on {p['payment_date']} by {p['paid_by']} ({'Receipt attached' if p['has_receipt'] else 'No receipt'})" for p in res["recent_payments"]]
                items_str = "\n".join(items)
                assistant_content = (
                    f"Recorded a total of **{count} payments** totaling **{total_settled} INR** across the household.\n\n"
                    f"**Recent Payments:**\n{items_str}"
                )

        # Intent 9: Provider spending
        elif "provider" in prompt_lower or any(p in prompt_lower for p in ["tata power", "airtel", "comcast", "netflix", "spotify"]):
            pname = "Tata Power" if "tata" in prompt_lower else ("Airtel" if "airtel" in prompt_lower else "Provider")
            res = execute_tool_call("get_provider_spending", {"provider_name": pname, "months": 6}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_provider_spending",
                tool_arguments={"provider_name": pname, "months": 6},
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            assistant_content = (
                f"Historical spend for **{res['provider_query']}** over the last 6 months totals **{res['total_spent']} INR** "
                f"across {res['bills_count']} bills.\n"
                f"- **Rate Trend:** {res['rate_change_summary']}"
            )

        # Intent 10: Household spending / monthly expenses summary
        elif any(k in prompt_lower for k in ["month's", "this month", "monthly", "spending", "expenses", "summary"]):
            res = execute_tool_call("get_household_spending", {}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_household_spending",
                tool_arguments={},
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            assistant_content = (
                f"### Spending Summary for {res['period']}\n"
                f"- **Total Billed:** **{res['total_billed']} INR** across {res['bills_count']} bills\n"
                f"- **Total Paid:** **{res['total_paid']} INR**\n"
                f"- **Outstanding Balance:** **{res['total_outstanding']} INR**\n"
                f"- **Compared to Previous Month ({res['previous_period']}):** {res['percentage_change']} change ({res['difference']} INR)\n"
            )

        # Intent 11: General / Out-of-scope questions (e.g. weather, sports, jokes, news)
        elif any(k in prompt_lower for k in ["weather", "temperature", "forecast", "joke", "recipe", "news", "movie", "game", "who won", "president", "capital of", "who is", "what time"]):
            assistant_content = (
                "I am **HomePilot AI**, your dedicated household financial and utility assistant. "
                "Answering general questions like weather, trivia, or general news is outside my supported capabilities.\n\n"
                "I can help you with:\n"
                "- 📅 **Upcoming & Overdue Bills**: *'Show my upcoming bills'* or *'Which bills are overdue?'*\n"
                "- 💳 **Unpaid Liabilities**: *'Show me one unpaid bill'* or *'How much is outstanding?'*\n"
                "- 📊 **Spending Analysis**: *'How much did we spend on electricity?'* or *'Summarize monthly expenses'*\n"
                "- 🔁 **Recurring Subscriptions**: *'What are our recurring subscriptions?'*"
            )

        # Intent 12: Greetings / Capabilities
        elif any(k in prompt_lower for k in ["hello", "hi", "hey", "who are you", "what can you do", "help me", "help"]):
            assistant_content = (
                "Hello! I am **HomePilot AI**, your household utility and financial assistant.\n\n"
                "Here is how I can help your family:\n"
                "- 📅 **Track Bills**: Ask *'Show upcoming bills'* or *'Show overdue bills'*.\n"
                "- 💳 **Outstanding Balances**: Ask *'How much is outstanding?'* or *'Show me one unpaid bill'*.\n"
                "- 📊 **Expense Insights**: Ask *'How much did we spend on electricity last month?'* or *'Summarize this month's spending'*.\n"
                "- 🔁 **Subscriptions**: Ask *'What are our recurring subscriptions?'*."
            )

        # Intent 13: Ambiguous or Unrecognized
        else:
            assistant_content = (
                "I'm not sure how to answer that request. As your household utility assistant, I can check your bills, "
                "summarize expenses, track recurring subscriptions, and calculate outstanding balances.\n\n"
                "Try asking:\n"
                "- *'Show me one unpaid bill'*\n"
                "- *'How much is outstanding?'*\n"
                "- *'Show upcoming bills'* or *'Which bills are overdue?'*"
            )

        # Track usage
        AIUsageRecord.objects.create(
            household=household,
            user=user,
            provider="mock-ai",
            model_name="deterministic-assistant",
            prompt_tokens=100,
            completion_tokens=80,
            total_tokens=180,
            estimated_cost_usd=Decimal("0.00000"),
            operation_type="CHAT_ASSISTANT",
        )

        assistant_msg = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=assistant_content,
        )
        for te in tool_executions:
            te.message = assistant_msg
            te.save(update_fields=["message"])

        return assistant_msg, tool_executions
