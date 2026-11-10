import logging
from celery import shared_task
from apps.households.models import Household
from apps.ai.services.anomaly_detector import SpendingAnomalyDetector

logger = logging.getLogger(__name__)


@shared_task
def generate_spending_insights_task():
    """
    Celery periodic background task (e.g. weekly or daily) that inspects
    all households for spending spikes, anomalies, and upcoming forecasts.
    """
    households = Household.objects.all()
    total_insights = 0

    for hh in households:
        try:
            insights = SpendingAnomalyDetector.analyze_household(hh)
            total_insights += len(insights)
        except Exception as exc:
            logger.exception(f"Error generating spending insights for household #{hh.id}: {exc}")

    logger.info(f"Spending insights analysis complete: Generated {total_insights} new insights.")
    return total_insights
