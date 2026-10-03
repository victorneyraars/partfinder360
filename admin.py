# ============================================================
# PartFinder Admin API - endpoints protegidos con JWT
# ============================================================
# Modulo separado para aislar la logica de administracion del
# endpoint publico. Se importa desde main.py.
# ============================================================

import os
import jwt
import bcrypt
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel
from typing import Optional

# ============================================================
# Configuracion
# ============================================================
ADMIN_JWT_SECRET = os.getenv("ADMIN_JWT_SECRET", "")
ADMIN_JWT_ALGORITHM = "HS256"
ADMIN_JWT_EXPIRES_HOURS = 24

# Formato en .env: ADMIN_USERS=email1:hash1,email2:hash2
_raw_users = os.getenv("ADMIN_USERS", "")
ADMIN_USERS = {}
for entry in _raw_users.split(","):
    entry = entry.strip()
    if ":" in entry:
        email, pwd_hash = entry.split(":", 1)
        ADMIN_USERS[email.strip().lower()] = pwd_hash.strip()

# Mismas credenciales que main.py
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

# ============================================================
# Router
# ============================================================
router = APIRouter(prefix="/api/admin", tags=["admin"])

# ============================================================
# Modelos
# ============================================================
class LoginPayload(BaseModel):
    email: str
    password: str


# ============================================================
# Auth helpers
# ============================================================
def _emit_jwt(email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=ADMIN_JWT_EXPIRES_HOURS)).timestamp()),
        "iss": "partfinder-admin",
    }
    return jwt.encode(payload, ADMIN_JWT_SECRET, algorithm=ADMIN_JWT_ALGORITHM)


def verify_admin_token(authorization: Optional[str] = Header(None)) -> str:
    """Dependency de FastAPI. Valida el JWT y devuelve el email del admin."""
    if not ADMIN_JWT_SECRET:
        raise HTTPException(status_code=500, detail="ADMIN_JWT_SECRET no configurado en el servidor")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Falta token de autorizacion")
    token = authorization[len("Bearer "):].strip()
    try:
        payload = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=[ADMIN_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token invalido")
    email = payload.get("sub", "")
    if email.lower() not in ADMIN_USERS:
        raise HTTPException(status_code=403, detail="Usuario no autorizado")
    return email


# ============================================================
# POST /api/admin/auth/login
# ============================================================
@router.post("/auth/login")
def admin_login(payload: LoginPayload):
    # Chequeo de configuracion: falla ruidosamente si el servidor no esta listo
    if not ADMIN_JWT_SECRET:
        raise HTTPException(status_code=500, detail="Admin API sin ADMIN_JWT_SECRET")
    if not ADMIN_USERS:
        raise HTTPException(status_code=500, detail="Admin API sin usuarios configurados")

    email = payload.email.strip().lower()
    pwd_hash = ADMIN_USERS.get(email)
    if not pwd_hash:
        # Mensaje generico para no filtrar si el email existe
        raise HTTPException(status_code=401, detail="Credenciales invalidas")
    try:
        valid = bcrypt.checkpw(payload.password.encode(), pwd_hash.encode())
    except Exception:
        valid = False
    if not valid:
        raise HTTPException(status_code=401, detail="Credenciales invalidas")
    token = _emit_jwt(email)
    return {
        "status": "ok",
        "token": token,
        "expires_in": ADMIN_JWT_EXPIRES_HOURS * 3600,
        "email": email,
    }


# ============================================================
# GET /api/admin/me
# ============================================================
@router.get("/me")
def admin_me(user: str = Header(None, alias="X-Admin-User")):
    # Nota: solo para debugging rapido. El endpoint real usa verify_admin_token.
    return {"status": "ok"}

# ============================================================
# GET /api/admin/health
# ============================================================
@router.get("/health")
def admin_health(user: str = Depends(verify_admin_token)):
    # Nota: se valida con verify_admin_token en cada request real.
    # Uso Header real: se debe enviar Authorization: Bearer <token>
    # y ademas X-Admin-User: <email> (o usar Depends en una version futura)
    result = {"status": "ok", "services": {}}

    # 1. DB
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
        cur.close()
        conn.close()
        result["services"]["pf_database"] = {"status": "ok"}
    except Exception as e:
        result["services"]["pf_database"] = {"status": "error", "detail": str(e)[:120]}

    # 2. Boostr (via tabla api_quota)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT total_limit, remaining, updated_at FROM api_quota WHERE provider = 'boostr'")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            result["services"]["boostr"] = {
                "status": "ok",
                "total_limit": row["total_limit"],
                "remaining": row["remaining"],
                "updated_at": str(row["updated_at"]),
            }
        else:
            result["services"]["boostr"] = {"status": "unknown"}
    except Exception as e:
        result["services"]["boostr"] = {"status": "error", "detail": str(e)[:120]}

    # 3. vehicle_cache count
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM vehicle_cache")
        row = cur.fetchone()
        cur.close()
        conn.close()
        result["services"]["vehicle_cache"] = {"status": "ok", "count": row["n"] if row else 0}
    except Exception as e:
        result["services"]["vehicle_cache"] = {"status": "error", "detail": str(e)[:120]}

    return result


# ============================================================
# GET /api/admin/quota
# ============================================================
@router.get("/quota")
def admin_quota(user: str = Depends(verify_admin_token)):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT provider, total_limit, remaining, updated_at FROM api_quota ORDER BY provider")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {
            "status": "ok",
            "quotas": [dict(r) for r in rows],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando cuotas: {str(e)[:120]}")


# ============================================================
# GET /api/admin/plates/stats?days=7
# ============================================================
@router.get("/plates/stats")
def admin_plates_stats(days: int = 7, user: str = Depends(verify_admin_token)):
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days debe estar entre 1 y 365")
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Total de patentes en cache
        cur.execute("SELECT COUNT(*) AS n FROM vehicle_cache")
        total_row = cur.fetchone()
        total = total_row["n"] if total_row else 0

        # Consultas por dia
        cur.execute("""
            SELECT DATE(created_at) AS dia, COUNT(*) AS consultas
            FROM vehicle_cache
            WHERE created_at > NOW() - (%s || ' days')::interval
            GROUP BY DATE(created_at)
            ORDER BY dia DESC
        """, (days,))
        by_day = [dict(r) for r in cur.fetchall()]

        # Por source
        cur.execute("""
            SELECT COALESCE(source, 'unknown') AS source, COUNT(*) AS n
            FROM vehicle_cache
            GROUP BY source
            ORDER BY n DESC
        """)
        by_source = [dict(r) for r in cur.fetchall()]

        cur.close()
        conn.close()
        return {
            "status": "ok",
            "total_cached": total,
            "days": days,
            "by_day": by_day,
            "by_source": by_source,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando stats: {str(e)[:120]}")


# ============================================================
# GET /api/admin/plates/recent?limit=50
# ============================================================
@router.get("/plates/recent")
def admin_plates_recent(limit: int = 50, user: str = Depends(verify_admin_token)):
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit debe estar entre 1 y 500")
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT plate, COALESCE(source, 'unknown') AS source, created_at
            FROM vehicle_cache
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {
            "status": "ok",
            "count": len(rows),
            "plates": [
                {
                    "plate": r["plate"],
                    "source": r["source"],
                    "created_at": str(r["created_at"]),
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando patentes: {str(e)[:120]}")
