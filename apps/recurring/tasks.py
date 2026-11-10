import logging
from celery import shared_task
from .services.schedule_service import RecurringScheduleService

logger = logging.getLogger(__name__)


@shared_task
def generate_recurring_bills_task(generate_ahead_days: int = 15):
    """
    Periodic task run daily by Celery Beat (e.g. 06:00 AM).
    Finds active recurring schedules due within generate_ahead_days and
    idempotently creates upcoming bills.
    """
    logger.info("Starting automatic recurring bills generation task.")
    generated_pairs = RecurringScheduleService.generate_due_occurrences(generate_ahead_days=generate_ahead_days)
    count = len(generated_pairs)
    logger.info(f"Recurring bills check complete: Generated {count} upcoming bills.")
    return count
