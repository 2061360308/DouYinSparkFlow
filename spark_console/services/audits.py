from sqlalchemy.orm import Session

from spark_console.models import AuditEvent


class AuditService:
    def __init__(self, session: Session):
        self.session = session

    def write(
        self,
        actor_user_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        detail: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            actor_user_id=actor_user_id,
            action=action[:64],
            resource_type=resource_type[:32],
            resource_id=resource_id,
            detail=detail[:240] if detail else None,
        )
        self.session.add(event)
        return event
