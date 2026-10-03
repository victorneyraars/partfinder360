"""
PartFinder360 — Cola Asíncrona de Cuota Boostr (Backlog Queue).

Cuando la API de Boostr responde con cuota agotada (429 /
PLAN_LIMIT_EXCEEDED), la patente se encola en `boostr_pending_queue`.
Este worker:

  - process_queue(): verifica cuota disponible y consume la cola
    consultando https://api.boostr.cl/vehicle/{plate}.json?include=owner,
    guarda el payload en `vehicle_cache` y marca la entrada RESOLVED.
  - enqueue_plate(plate): encola (idempotente) una patente pendiente.

Se invoca desde POST /api/boostr/process-queue (o un cron externo).
Importa main.py SOLO dentro de las funciones (evita import circular).
"""
import os
import json
from datetime import datetime

BOOSTR_API_URL = "https://api.boostr.cl/vehicle/{plate}.json?include=owner"


def _db():
    from main import get_db_connection
    return get_db_connection()


def _boostr_key():
    from main import BOOSTR_API_KEY
    return BOOSTR_API_KEY


def enqueue_plate(plate: str, status: str = "QUEUED") -> bool:
    """Encola una patente para sincronización automática (idempotente)."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO boostr_pending_queue (plate, status, created_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (plate) DO UPDATE SET status = EXCLUDED.status
            """,
            (plate, status),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"boostr_queue: error encolando {plate}: {e}")
        return False


def quota_available() -> bool:
    """True si hay cuota Boostr disponible según la tabla api_quota."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "SELECT remaining FROM api_quota WHERE provider = 'boostr';"
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            remaining = row.get("remaining") if isinstance(row, dict) else row[0]
            try:
                return int(remaining) > 0
            except (TypeError, ValueError):
                return False
        return True  # sin tabla de cuota → asumir disponible
    except Exception as e:
        print(f"boostr_queue: error leyendo cuota: {e}")
        return True


def process_queue(limit: int = 10) -> dict:
    """
    Consume la cola: para cada patente pendiente consulta Boostr con
    ?include=owner. Si la cuota se agota a mitad de camino, se detiene y
    deja el resto en cola.
    """
    import requests

    from main import _boostr_enrich, get_db_connection

    processed = 0
    resolved = 0
    errors = 0
    stayed_queued = 0

    key = _boostr_key()
    if not key:
        return {"processed": 0, "resolved": 0, "errors": 0, "queued": 0,
                "message": "BOOSTR_API_KEY no configurada"}
    if not quota_available():
        return {"processed": 0, "resolved": 0, "errors": 0, "queued": 0,
                "message": "Sin cuota disponible"}

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT plate FROM boostr_pending_queue
            WHERE status IN ('QUEUED', 'PENDING')
            ORDER BY created_at ASC
            LIMIT %s;
            """,
            (limit,),
        )
        rows = [r["plate"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        print(f"boostr_queue: error leyendo cola: {e}")
        return {"processed": 0, "resolved": 0, "errors": 0, "queued": 0,
                "message": str(e)}

    for plate in rows:
        plate = str(plate).strip().upper().replace("-", "").replace(" ", "")
        if not plate:
            continue
        processed += 1
        try:
            r = requests.get(
                BOOSTR_API_URL.format(plate=plate),
                headers={"X-API-KEY": key},
                timeout=15,
            )
            if r.status_code == 429 or "PLAN_LIMIT_EXCEEDED" in (r.text or "").upper():
                # Cuota agotada: detener el consumo y dejar la cola intacta.
                stayed_queued += 1
                _set_status(plate, "QUEUED")
                print(f"boostr_queue: cuota agotada, deteniendo ({plate})")
                break
            if r.status_code != 200:
                _set_status(plate, "ERROR")
                errors += 1
                continue
            data = r.json()
            v_inner = data.get("data", {})
            if not v_inner or not isinstance(v_inner, dict) or not v_inner.get("make"):
                _set_status(plate, "ERROR")
                errors += 1
                continue
            enriched = _boostr_enrich(v_inner)
            enriched.setdefault("patente", plate)
            # Guardar en vehicle_cache (merge: no pisar ficha existente).
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT data FROM vehicle_cache WHERE plate = %s;", (plate,))
                row = cur.fetchone()
                existing = {}
                if row:
                    d = row["data"] if isinstance(row, dict) else row[0]
                    if isinstance(d, dict):
                        existing = d
                merged = {**existing, **enriched}
                cur.execute(
                    """
                    INSERT INTO vehicle_cache (plate, data) VALUES (%s, %s)
                    ON CONFLICT (plate) DO UPDATE SET data = EXCLUDED.data, created_at = CURRENT_TIMESTAMP;
                    """,
                    (plate, json.dumps(merged)),
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                print(f"boostr_queue: error guardando {plate}: {e}")
            _set_status(plate, "RESOLVED")
            resolved += 1
        except requests.exceptions.RequestException as e:
            print(f"boostr_queue: error red para {plate}: {e}")
            _set_status(plate, "ERROR")
            errors += 1
        except Exception as e:
            print(f"boostr_queue: error procesando {plate}: {e}")
            _set_status(plate, "ERROR")
            errors += 1

    return {
        "processed": processed,
        "resolved": resolved,
        "errors": errors,
        "queued": stayed_queued,
        "message": "OK",
    }


def _set_status(plate: str, status: str):
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE boostr_pending_queue
            SET status = %s, processed_at = CURRENT_TIMESTAMP
            WHERE plate = %s;
            """,
            (status, plate),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"boostr_queue: error actualizando estado de {plate}: {e}")
