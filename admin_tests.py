"""
Admin Tests - Endpoints para probar los servicios del ecosistema.
Cada test devuelve: status, latencia, checks, warnings, errors.

Cargado desde main.py como un router FastAPI adicional.
"""

import os
import time
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends

# Config
DB_HOST = os.getenv("DB_HOST", "pf_database")
DB_NAME = os.getenv("DB_NAME", "partfinder")
DB_USER = os.getenv("DB_USER", "pf_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secure_db_password_change_me")
MTT_SERVICE_URL = os.getenv("MTT_SERVICE_URL", "http://mtt-service:3091")
PRT_SERVICE_URL = os.getenv("PRT_SERVICE_URL", "http://host.docker.internal:3090")
BOOSTR_API_KEY = os.getenv("BOOSTR_API_KEY", "")


def _db():
    return psycopg2.connect(
        host=DB_HOST, database=DB_NAME, user=DB_USER,
        password=DB_PASSWORD, cursor_factory=RealDictCursor,
    )


def _empty(service: str, endpoint: str = ""):
    return {
        "service": service,
        "endpoint": endpoint,
        "status": "error",
        "latency_ms": 0,
        "checks": [],
        "warnings": [],
        "errors": [],
        "response": None,
    }

# ============================================================
# Test 1: pf_database (PostgreSQL)
# ============================================================
def _test_pf_database():
    """Prueba la conexion a Postgres + verifica tablas criticas."""
    result = _empty("pf_database", "internal")
    t0 = time.time()
    try:
        conn = _db()
        cur = conn.cursor()

        # Check 1: SELECT 1
        cur.execute("SELECT 1 AS ok")
        row = cur.fetchone()
        if row and row.get("ok") == 1:
            result["checks"].append({"name": "select_1", "status": "ok"})

        # Check 2: contar tablas clave
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        tablas = [r["table_name"] for r in cur.fetchall()]
        result["checks"].append({
            "name": "tablas",
            "status": "ok",
            "detail": f"{len(tablas)} tablas: {', '.join(tablas)}",
        })

        # Check 3: contar filas de cada tabla
        counts = {}
        for t in ["vehicle_cache", "usage_events", "api_quota", "sii_tasaciones", "boostr_pending_queue"]:
            try:
                cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
                counts[t] = cur.fetchone()["n"]
            except Exception:
                counts[t] = "error"
        result["checks"].append({
            "name": "row_counts",
            "status": "ok",
            "detail": counts,
        })

        # Check 4: ultima consulta en vehicle_cache
        cur.execute("SELECT plate, source, created_at FROM vehicle_cache ORDER BY created_at DESC LIMIT 1")
        ultima = cur.fetchone()
        if ultima:
            result["checks"].append({
                "name": "ultima_consulta_cache",
                "status": "ok",
                "detail": {
                    "plate": ultima["plate"],
                    "source": ultima["source"],
                    "created_at": str(ultima["created_at"]),
                },
            })

        # Check 5: cuota Boostr actual
        cur.execute("SELECT provider, remaining, total_limit, updated_at FROM api_quota")
        cuotas = [dict(r) for r in cur.fetchall()]
        result["checks"].append({
            "name": "api_quota",
            "status": "ok",
            "detail": cuotas,
        })

        cur.close()
        conn.close()
        result["status"] = "ok"
        result["response"] = {"tables": tablas, "counts": counts, "cuotas": cuotas}
    except Exception as e:
        result["errors"].append(str(e)[:200])
    result["latency_ms"] = int((time.time() - t0) * 1000)
    return result


# ============================================================
# Test 2: mtt-service (scraper MTT con cache)
# ============================================================
def _test_mtt_service(plate: str = "KHFF35"):
    """Prueba el microservicio MTT. Devuelve TODO el payload y verifica
    que el cache del servicio tenga la patente para futuros usos."""
    endpoint = f"{MTT_SERVICE_URL}/api/v1/mtt/{plate}"
    result = _empty("mtt-service", endpoint)
    t0 = time.time()
    try:
        # Check 1: health
        h = requests.get(f"{MTT_SERVICE_URL}/health", timeout=5)
        if h.status_code == 200:
            health = h.json()
            result["checks"].append({
                "name": "health",
                "status": "ok",
                "detail": health,
            })
        else:
            result["checks"].append({
                "name": "health",
                "status": "error",
                "detail": f"HTTP {h.status_code}",
            })

        # Check 2: consulta real
        r = requests.get(endpoint, timeout=30)
        if r.status_code != 200:
            result["errors"].append(f"Consulta HTTP {r.status_code}")
            result["latency_ms"] = int((time.time() - t0) * 1000)
            return result

        payload = r.json()
        source = payload.get("source", "unknown")
        data = payload.get("data", {})

        result["checks"].append({
            "name": "consulta",
            "status": "ok",
            "detail": f"source={source}, plate={data.get('patente', plate)}",
        })

        # Check 3: estructura de datos
        checks_data = []
        if "es_transporte_publico" in data:
            checks_data.append({
                "name": "es_transporte_publico",
                "value": data["es_transporte_publico"],
            })
        if "secciones" in data:
            checks_data.append({
                "name": "secciones",
                "value": f"{len(data.get('secciones') or [])} secciones",
            })
        if checks_data:
            result["checks"].append({
                "name": "estructura_datos",
                "status": "ok",
                "detail": checks_data,
            })

        # Warning si es cache miss (recien scrapeado)
        if source == "scraper":
            result["warnings"].append(
                f"Cache miss: la consulta tardo {payload.get('duration_ms', '?')}ms. "
                f"Proximas consultas de {plate} seran desde cache (<200ms)."
            )

        # Warning si el payload esta vacio
        if not data or not data.get("secciones"):
            result["warnings"].append(
                "El payload no contiene secciones. Verificar si el portal MTT cambio su estructura."
            )

        result["status"] = "ok"
        result["response"] = payload
    except Exception as e:
        result["errors"].append(str(e)[:200])
    result["latency_ms"] = int((time.time() - t0) * 1000)
    return result

# ============================================================
# Router y endpoints
# ============================================================
router = APIRouter(prefix="/api/admin/test", tags=["admin-tests"])

# Importar la dependencia de auth del admin
try:
    from admin import verify_admin_token
except ImportError:
    verify_admin_token = None


def _auth_dep():
    """Dependencia de auth opcional (reusa verify_admin_token)."""
    if verify_admin_token is None:
        raise HTTPException(status_code=500, detail="Auth no disponible")
    return verify_admin_token


@router.get("/mtt-service")
def admin_test_mtt_service(
    plate: str = "KHFF35",
    user: str = Depends(_auth_dep()),
):
    """Prueba el microservicio MTT. Devuelve:
    - Estado del health
    - Resultado de la consulta completa (todos los datos)
    - Verifica que se haya cacheado
    - Warnings si algo no esta OK
    """
    result = _test_mtt_service(plate)

    # Check extra: verificar que la patente quedo en el cache
    try:
        import sqlite3
        cache_info = requests.get(
            f"{MTT_SERVICE_URL}/api/v1/mtt/{plate}/cache",
            timeout=5,
        ).json()
        result["checks"].append({
            "name": "cache_persistencia",
            "status": "ok" if cache_info.get("status") == "cached" else "warning",
            "detail": cache_info,
        })
    except Exception as e:
        result["warnings"].append(f"No se pudo verificar cache: {str(e)[:100]}")

    return result
