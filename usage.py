# ============================================================
# Usage Tracking — eventos de uso anonimos por device_id
# ============================================================
import os
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any

DB_HOST = os.getenv("DB_HOST", "pf_database")
DB_NAME = os.getenv("DB_NAME", "partfinder")
DB_USER = os.getenv("DB_USER", "pf_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secure_db_password_change_me")


def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        cursor_factory=RealDictCursor,
    )


router = APIRouter(prefix="/api/usage", tags=["usage"])


class UsageEventPayload(BaseModel):
    device_id: str
    event_type: str
    plate: Optional[str] = None
    platform: Optional[str] = None
    app_version: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@router.post("/track")
def track_event(payload: UsageEventPayload):
    # Validaciones basicas
    device_id = (payload.device_id or "").strip()
    event_type = (payload.event_type or "").strip()
    if not device_id or len(device_id) > 64:
        raise HTTPException(status_code=400, detail="device_id invalido")
    if not event_type or len(event_type) > 32:
        raise HTTPException(status_code=400, detail="event_type invalido")

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO usage_events
              (device_id, event_type, plate, platform, app_version, metadata)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                device_id,
                event_type,
                (payload.plate or "")[:10] or None,
                (payload.platform or "")[:20] or None,
                (payload.app_version or "")[:20] or None,
                Json(payload.metadata) if payload.metadata is not None else None,
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error guardando evento: {str(e)[:120]}")
