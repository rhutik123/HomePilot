import logging
import re
from decimal import Decimal
from typing import Tuple, List, Dict, Any
from django.conf import settings
from django.utils import timezone

from apps.ai.models import AIConversation, AIMessage, AIToolExecution, AIUsageRecord
from apps.ai.services.tools import execute_tool_call, TOOL_REGISTRY

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are HomePilot AI, an intelligent, helpful household financial assistant.
Your job is to help families and household members track, explain, and manage their utility and living expenses.

CRITICAL INSTRUCTIONS:
1. All financial numbers, totals, and balances MUST be derived from tool results. NEVER fabricate or guess monetary figures.
2. If the user asks about bills, spending, due dates, or payments, ALWAYS use the provided tools to fetch real data before answering.
3. Be clear, encouraging, and actionable. Alert the user to overdue items and upcoming payment milestones.
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
                        name="get_unpaid_bills",
                        description="Fetches all unpaid, partially paid, or overdue bills for the household.",
                        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
                    ),
                    types.FunctionDeclaration(
                        name="get_upcoming_bills",
                        description="Fetches upcoming bills due within the specified number of days.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={"days": types.Schema(type=types.Type.INTEGER, description="Days ahead to check")},
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="get_monthly_spending",
                        description="Calculates total billed, paid, and outstanding for a given month and year.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "year": types.Schema(type=types.Type.INTEGER, description="Year (e.g. 2026)"),
                                "month": types.Schema(type=types.Type.INTEGER, description="Month (1-12)"),
                            },
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="compare_category_spending",
                        description="Compares household spending breakdown across categories over recent months.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={"months": types.Schema(type=types.Type.INTEGER, description="Number of months to analyze")},
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
            # Check for function calls
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

                # Second turn: provide tool output back to model
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
        Deterministic, offline assistant handler for tests and local development.
        Directly executes the corresponding household financial tool and returns
        truthful facts calculated by Django ORM / Decimal arithmetic.
        """
        household = conversation.household
        prompt_lower = prompt.lower()
        tool_executions = []
        assistant_content = ""

        # Intent detection
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
                    f"Bill #{res['bill_id']} from {res['provider_name']} has a total amount of {res['amount']} {res['currency']} "
                    f"with {res['outstanding_balance']} {res['currency']} outstanding. "
                    f"Status: {res['status']}. Payments recorded: {res['payments_count']}. "
                    f"Attached documents: {res['attached_documents_count']}."
                )

        elif any(k in prompt_lower for k in ["compare", "breakdown", "category", "categories"]):
            res = execute_tool_call("compare_category_spending", {"months": 3}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="compare_category_spending",
                tool_arguments={"months": 3},
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)
            cats_summary = ", ".join([f"{c['category']}: {c['total']} ({c['percentage_of_total']})" for c in res.get("categories", [])])
            assistant_content = (
                f"Household '{household.name}' spending over the last 3 months totals {res['grand_total']} INR. "
                f"Category breakdown: {cats_summary if cats_summary else 'No expense records found'}."
            )

        elif any(k in prompt_lower for k in ["month", "spent", "spending", "total"]):
            res = execute_tool_call("get_monthly_spending", {}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_monthly_spending",
                tool_arguments={},
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)
            assistant_content = (
                f"For {res['year']}-{res['month']:02d}, household '{household.name}' has total billed of {res['total_billed']} INR. "
                f"Total paid: {res['total_paid']} INR, with {res['total_outstanding']} INR outstanding."
            )

        elif any(k in prompt_lower for k in ["upcoming", "next"]):
            res = execute_tool_call("get_upcoming_bills", {"days": 30}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_upcoming_bills",
                tool_arguments={"days": 30},
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)
            assistant_content = (
                f"You have {res['upcoming_count']} upcoming bills in the next 30 days totaling {res['total_upcoming_amount']} INR."
            )

        else:
            # Default: retrieve unpaid bills
            res = execute_tool_call("get_unpaid_bills", {}, household, user)
            exec_rec = AIToolExecution.objects.create(
                conversation=conversation,
                message=user_msg,
                tool_name="get_unpaid_bills",
                tool_arguments={},
                execution_result=res,
                is_success="error" not in res,
            )
            tool_executions.append(exec_rec)
            assistant_content = (
                f"Household '{household.name}' currently has {res['unpaid_bills_count']} unpaid bills "
                f"totaling {res['total_outstanding_amount']} {res['currency']}."
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
        return assistant_msg, tool_executions
