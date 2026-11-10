import logging
from typing import Optional, Dict, Any
from apps.audit.models import AuditEvent

logger = logging.getLogger(__name__)

SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "key", "authorization", "csrfmiddlewaretoken"}


def sanitize_metadata(data: Any) -> Any:
    """Recursively removes sensitive keys such as passwords or API keys from metadata."""
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if str(k).lower() in SENSITIVE_KEYS:
                sanitized[k] = "******"
            else:
                sanitized[k] = sanitize_metadata(v)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_metadata(item) for item in data]
    return data


class AuditService:
    """
    Central service for recording compliance, business, and security audit events.
    """
    @classmethod
    def log(
        cls,
        action: str,
        actor=None,
        household=None,
        entity=None,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
    ) -> Optional[AuditEvent]:
        try:
            entity_type = ""
            entity_id = None
            if entity is not None:
                entity_type = entity.__class__.__name__
                entity_id = getattr(entity, "id", getattr(entity, "pk", None))

            # Auto-infer household from entity if not explicitly passed
            if not household and entity is not None:
                household = getattr(entity, "household", None)

            clean_meta = sanitize_metadata(metadata or {})

            event = AuditEvent.objects.create(
                actor=actor if (actor and getattr(actor, "is_authenticated", True)) else None,
                household=household,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                description=description or f"{action} performed on {entity_type} #{entity_id}",
                metadata=clean_meta,
                ip_address=ip_address,
            )
            return event
        except Exception as exc:
            logger.exception(f"Failed to record audit event for {action}: {exc}")
            return None
