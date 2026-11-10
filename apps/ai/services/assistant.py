import logging
import re
from decimal import Decimal
from typing import Tuple, List, Dict, Any
from django.conf import settings
from django.utils import timezone

from apps.ai.models import AIConversation, AIMessage, AIToolExecution, AIUsageRecord
from apps.ai.services.tools import execute_tool_call, TOOL_REGISTRY
from apps.bills.models import BillCategory

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are HomePilot AI, a knowledgeable, accurate household financial assistant.
Your job is to help families and household members track, explain, and manage their utility and living expenses.

CRITICAL ARCHITECTURAL MANDATES:
1. All financial numbers, totals, and balances MUST be derived from tool results. NEVER fabricate, estimate, or guess monetary figures.
2. If the user asks about bills, spending, due dates, payments, or subscriptions, ALWAYS use the provided tools to fetch real data before answering.
3. Be clear, encouraging, and actionable. Alert the user to overdue items and upcoming payment milestones.
4. Respect household security boundaries. Only discuss financial data returned by the household-scoped tools.
"""


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
        backend = getattr(settings, "BILL_EXTRACTION_BACKEND", "mock")

        # 2. Check if live Google Gemini client should be used
        if api_key and backend != "mock":
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
                        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
                    ),
                    types.FunctionDeclaration(
                        name="get_unpaid_bills",
                        description="Fetches all unpaid or partially paid bills for the household.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "only_assigned_to_me": types.Schema(type=types.Type.BOOLEAN, description="Filter only bills assigned to the calling user"),
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
            logger.exception(f"Gemini assistant error: {exc}. Falling back to deterministic assistant.")
            return cls._call_mock_assistant(conversation, user, user_msg, prompt)

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
        prompt_lower = prompt.lower()
        tool_executions = []
        assistant_content = ""

        # Intent 1: Bill explanation
        bill_id_match = re.search(r"bill\s*#?(\d+)", prompt_lower)
        if "explain" in prompt_lower and bill_id_match:
            bill_id = int(bill_id_match.group(1))
            res = execute_tool_call("explain_bill", {"bill_id": bill_id}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="explain_bill",
                tool_arguments={"bill_id": bill_id},
                execution_result=res,
                is_success="error" not in res,
                error_message=res.get("error", ""),
            )
            tool_executions.append(exec_rec)
            if "error" in res:
                assistant_content = f"I could not retrieve Bill #{bill_id}: {res['error']}"
            else:
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

        # Intent 2: Overdue bills
        elif any(k in prompt_lower for k in ["overdue", "past due", "late bills"]):
            only_me = any(k in prompt_lower for k in ["my", "assigned to me", "for me"])
            res = execute_tool_call("get_overdue_bills", {"only_assigned_to_me": only_me}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_overdue_bills",
                tool_arguments={"only_assigned_to_me": only_me},
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            count = res["overdue_count"]
            total = res["total_overdue_amount"]
            scope = "assigned to you" if only_me else "in the household"
            if count == 0:
                assistant_content = f"Great news! There are currently no overdue bills {scope}."
            else:
                items = [f"- **{b['provider_name']}** ({b['category_display']}): **{b['outstanding_balance']} INR** (overdue by {b['days_overdue']} days, due {b['due_date']})" for b in res["bills"]]
                items_str = "\n".join(items)
                assistant_content = (
                    f"There {'is' if count == 1 else 'are'} **{count} overdue bill{'s' if count != 1 else ''}** {scope} "
                    f"totaling **{total} INR**:\n\n{items_str}"
                )

        # Intent 3: Upcoming bills
        elif any(k in prompt_lower for k in ["upcoming", "due next", "due soon"]):
            only_me = any(k in prompt_lower for k in ["my", "assigned to me", "for me"])
            days = 7 if "week" in prompt_lower else 30
            res = execute_tool_call("get_upcoming_bills", {"days": days, "only_assigned_to_me": only_me}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_upcoming_bills",
                tool_arguments={"days": days, "only_assigned_to_me": only_me},
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            count = res["upcoming_count"]
            total = res["total_upcoming_amount"]
            scope = "assigned to you" if only_me else "for household"
            if count == 0:
                assistant_content = f"You have no upcoming bills due in the next {days} days."
            else:
                items = [f"- **{b['provider_name']}** ({b['category_display']}): **{b['outstanding_balance']} INR** (due {b['due_date']}, {b['days_until_due']} days remaining)" for b in res["bills"]]
                items_str = "\n".join(items)
                assistant_content = (
                    f"You have **{count} upcoming bill{'s' if count != 1 else ''}** {scope} due in the next {days} days "
                    f"totaling **{total} INR**:\n\n{items_str}"
                )

        # Intent 4: Category spending
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

        # Intent 5: Recurring bills
        elif any(k in prompt_lower for k in ["recurring", "subscription", "subscriptions", "autopay"]):
            res = execute_tool_call("get_recurring_bills", {}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_recurring_bills",
                tool_arguments={},
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            count = res["active_schedules_count"]
            monthly_total = res["estimated_monthly_recurring_total"]
            if count == 0:
                assistant_content = "There are currently no active recurring schedules configured for this household."
            else:
                items = [f"- **{s['provider_name']}** ({s['frequency_display']}): {s['expected_amount']} INR (~{s['monthly_equivalent']} INR/mo, next due: {s['next_due_date']})" for s in res["schedules"]]
                items_str = "\n".join(items)
                assistant_content = (
                    f"Household has **{count} active recurring schedule{'s' if count != 1 else ''}** "
                    f"totaling **{monthly_total} INR/month**:\n\n{items_str}"
                )

        # Intent 6: Payment summary
        elif any(k in prompt_lower for k in ["payment", "payments", "receipt", "receipts", "settled"]):
            res = execute_tool_call("get_payment_summary", {"limit": 5}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_payment_summary",
                tool_arguments={"limit": 5},
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

        # Intent 7: Provider spending
        elif "provider" in prompt_lower or any(p in prompt_lower for p in ["tata power", "airtel", "comcast", "netflix", "spotify"]):
            # Extract provider name
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

        # Intent 8: Household spending / monthly expenses summary
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

        # Default: Unpaid bills
        else:
            only_me = any(k in prompt_lower for k in ["my", "assigned to me", "for me"])
            res = execute_tool_call("get_unpaid_bills", {"only_assigned_to_me": only_me}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_unpaid_bills",
                tool_arguments={"only_assigned_to_me": only_me},
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)

            count = res["unpaid_bills_count"]
            total = res["total_outstanding_amount"]
            scope = "assigned to you" if only_me else "in the household"
            if count == 0:
                assistant_content = f"There are currently no unpaid bills {scope}."
            else:
                items = [f"- **{b['provider_name']}** ({b['category_display']}): **{b['outstanding_balance']} INR** (status: {b['status']}, due: {b['due_date']})" for b in res["bills"]]
                items_str = "\n".join(items)
                assistant_content = (
                    f"Found **{count} unpaid bill{'s' if count != 1 else ''}** {scope} "
                    f"totaling **{total} INR**:\n\n{items_str}"
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
