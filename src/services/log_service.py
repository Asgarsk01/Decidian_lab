from __future__ import annotations

import orjson
from sqlalchemy.orm import Session

from src.models import ProcessingLog


def log_event(
    session: Session,
    document_id: str | None,
    stage: str,
    message: str,
    level: str = "info",
    details: dict | None = None,
) -> ProcessingLog:
    row = ProcessingLog(
        document_id=document_id,
        level=level,
        stage=stage,
        message=message,
        details_json=orjson.dumps(details or {}).decode(),
    )
    session.add(row)
    session.flush()
    return row

