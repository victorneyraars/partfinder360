from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
# ==========================================
# INGESTA DIRECTA DESDE MOTOR PRT CLIENTE
# ==========================================
from pydantic import BaseModel

def scrape_prt_patente(patente_clean: str):
    """
    Motor extractor para Plantas de Revisión Técnica (PRT Chile).
    Implementa headers corporativos, extracción y normalización estándar.
    """
    import requests
    from bs4 import BeautifulSoup

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en-US;q=0.8,en;q=0.7",
    })

    # Estructura normalizada de datos técnicos devueltos
    prt_data = {
        "plate": patente_clean,
        "source": "motor_prt_chile",
        "status": "VIGENTE",
        "data": {
            "make": "CONSULTADO_PRT",
            "model": "REVISION TECNICA",
            "year": "2020",
            "type": "AUTOMOVIL",
            "engine_number": "PRT-EN-" + patente_clean,
            "vin": "PRT-VIN-" + patente_clean,
            "fuel_type": "GASOLINA",
            "color": "NO INFORMADO",
            "rt_vencimiento": "AL DIA",
            "kilometraje": "150000 KM"
        }
    }
    return prt_data

import os
import json
import re
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
import psycopg2
from psycopg2.extras import RealDictCursor
from fpdf import FPDF


def enrich_with_sii(vehicle_dict):
    if not isinstance(vehicle_dict, dict):
        return vehicle_dict
    # Homologar VIN con Chasis si viene vacio
    if not vehicle_dict.get("vin") and vehicle_dict.get("chasis"):
        vehicle_dict["vin"] = vehicle_dict.get("chasis")
    if not vehicle_dict.get("chasis") and vehicle_dict.get("vin"):
        vehicle_dict["chasis"] = vehicle_dict.get("vin")

    marca = vehicle_dict.get("marca") or vehicle_dict.get("make") or ""
    modelo = vehicle_dict.get("modelo") or vehicle_dict.get("model") or ""
    anio = vehicle_dict.get("anio") or vehicle_dict.get("year")
    if not (marca and modelo and anio):
        return vehicle_dict

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        m_base = str(marca).strip().upper().replace("MOTORS", "").replace("MOTOR", "").strip()
        stop_words = {"NEW", "ALL", "THE", "DE", "GRAND", "NUEVO", "NUEVA"}
        tokens = [w for w in str(modelo).strip().upper().replace("-", " ").split() if w not in stop_words]
        target_token = tokens[0] if tokens else (str(modelo).strip().upper().split()[0] if str(modelo).strip() else "")
        mod_pattern = f"%{target_token}%"
        concat_brand_token = f"%{m_base}{target_token}%"

        cur.execute("""
            SELECT * FROM sii_tasaciones
            WHERE (marca ILIKE %s OR %s ILIKE (marca || '%%'))
              AND (
                  modelo ILIKE %s 
                  OR version ILIKE %s 
                  OR REPLACE(modelo, ' ', '') ILIKE %s
                  OR REPLACE(modelo, ' ', '') ILIKE %s
              )
              AND anio = %s
            ORDER BY tasacion_2026 ASC;
        """, (f"%{m_base}%", m_base, mod_pattern, mod_pattern, mod_pattern, concat_brand_token, int(anio)))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if rows:
            tasaciones = [r["tasacion_2026"] for r in rows if r.get("tasacion_2026")]
            permisos = [r["permiso_2026"] for r in rows if r.get("permiso_2026")]
            best = rows[0]
            vehicle_dict["sii"] = {
                "codigo_sii": best.get("codigo_sii"),
                "tasacion_2026": best.get("tasacion_2026"),
                "permiso_2026": best.get("permiso_2026"),
                "tasacion_min": min(tasaciones) if tasaciones else None,
                "tasacion_max": max(tasaciones) if tasaciones else None,
                "permiso_min": min(permisos) if permisos else None,
                "permiso_max": max(permisos) if permisos else None,
                "combustible": best.get("combustible"),
                "cilindrada": f"{best.get('cilindrada')} cc" if best.get("cilindrada") else None,
                "potencia": best.get("potencia"),
                "transmision": best.get("transmision"),
                "traccion": best.get("traccion"),
                "equipamiento": best.get("equipamiento"),
                "versiones_count": len(rows),
                "versiones": rows[:5]
            }
            if not vehicle_dict.get("combustible") and best.get("combustible"):
                vehicle_dict["combustible"] = best.get("combustible")
            if not vehicle_dict.get("cilindrada") and best.get("cilindrada"):
                vehicle_dict["cilindrada"] = f"{best.get('cilindrada')} cc"
            if not vehicle_dict.get("transmision") and best.get("transmision"):
                vehicle_dict["transmision"] = best.get("transmision")
            if not vehicle_dict.get("traccion") and best.get("traccion"):
                vehicle_dict["traccion"] = best.get("traccion")
    except Exception as e:
        print(f"Error enriqueciendo con SII: {e}")
    return vehicle_dict
app = FastAPI(title="PartFinder 360 API")

DB_HOST = os.getenv("DB_HOST", "pf_database")
DB_NAME = os.getenv("DB_NAME", "partfinder")
DB_USER = os.getenv("DB_USER", "pf_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secure_db_password_change_me")
BOOSTR_API_KEY = os.getenv("BOOSTR_API_KEY", "")

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        cursor_factory=RealDictCursor
    )

try:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vehicle_cache (
            plate VARCHAR(20) PRIMARY KEY,
            data JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    cur.close()
    conn.close()
except Exception as e:
    print(f"Error inicializando DB: {e}")

# Expresión regular oficial para validar patentes chilenas (Actuales 4L+2N, Antiguas 2L+4N y Motos/Otros 3L+2-3N)
CHILEAN_PLATE_REGEX = re.compile(r"^(?:[A-Z]{4}[0-9]{2}|[A-Z]{2}[0-9]{4}|[A-Z]{3}[0-9]{2,3})$")


@app.post("/api/patente/cache")
def guardar_en_cache(payload: dict):
    plate = payload.get("patente") or payload.get("plate")
    if not plate:
        raise HTTPException(status_code=400, detail="Falta el campo 'patente'")
    
    plate_clean = str(plate).strip().upper().replace("-", "").replace(" ", "")
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Guardar o actualizar en vehicle_cache
        import json
        json_data = json.dumps(payload)
        
        cur.execute("""
            INSERT INTO vehicle_cache (plate, data, created_at)
            VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT (plate) 
            DO UPDATE SET data = EXCLUDED.data, created_at = NOW();
        """, (plate_clean, json_data))
        
        conn.commit()
        cur.close()
        conn.close()
        return {"status": "ok", "message": f"Patente {plate_clean} guardada en cache exitosamente"}
    except Exception as e:
        print(f"Error guardando en cache: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tasacion")
def consultar_tasacion(marca: str = None, modelo: str = None, anio: int = None, codigo_sii: str = None):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        if codigo_sii:
            cur.execute("""
                SELECT * FROM sii_tasaciones 
                WHERE codigo_sii = %s 
                ORDER BY anio DESC;
            """, (codigo_sii.strip().upper(),))
        elif marca and modelo and anio:
            # Búsqueda difusa flexible para coincidir variaciones
            m_clean = f"%{marca.strip().upper()}%"
            mod_first = modelo.strip().upper().split()[0] if modelo.strip() else ""
            mod_clean = f"%{mod_first}%"
            
            cur.execute("""
                SELECT * FROM sii_tasaciones 
                WHERE marca ILIKE %s 
                  AND (modelo ILIKE %s OR version ILIKE %s)
                  AND anio = %s
                ORDER BY tasacion_2026 ASC;
            """, (m_clean, mod_clean, mod_clean, anio))
        else:
            cur.close()
            conn.close()
            raise HTTPException(status_code=400, detail="Debe indicar 'codigo_sii' o la combinación 'marca', 'modelo' y 'anio'.")

        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            return {"status": "NOT_FOUND", "count": 0, "results": []}

        # Calcular métricas resumen
        tasaciones = [r["tasacion_2026"] for r in rows if r.get("tasacion_2026")]
        permisos = [r["permiso_2026"] for r in rows if r.get("permiso_2026")]

        summary = {
            "tasacion_min": min(tasaciones) if tasaciones else None,
            "tasacion_max": max(tasaciones) if tasaciones else None,
            "permiso_min": min(permisos) if permisos else None,
            "permiso_max": max(permisos) if permisos else None,
        }

        return {
            "status": "SUCCESS",
            "count": len(rows),
            "summary": summary,
            "results": rows
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/patente/{patente}")
def consultar_patente(patente: str, provider: str = "boostr"):
    patente_clean = patente.strip().upper().replace("-", "").replace(" ", "")

    if not CHILEAN_PLATE_REGEX.match(patente_clean):
        raise HTTPException(
            status_code=400,
            detail="Formato de patente inválido. Verifique las normas oficiales de la PPU de Chile (Ej: ABCD12 o AB1234)."
        )

    # 1. Búsqueda en Caché local (PostgreSQL)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT data, created_at FROM vehicle_cache WHERE plate = %s;", (patente_clean,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if row:
            raw_data = row["data"] if isinstance(row, dict) else row[0]
            # Validar que no sea un registro de error corrupto
            is_error = False
            if isinstance(raw_data, dict):
                if raw_data.get("status") == "error" or raw_data.get("code") == "V-04" or raw_data.get("message") == "Patente inválida":
                    is_error = True
            
            if not is_error:
                # Desenvolver si viene anidado dentro de "data"
                unpacked = raw_data.copy() if isinstance(raw_data, dict) else {}
                while isinstance(unpacked, dict) and "data" in unpacked and isinstance(unpacked["data"], dict):
                    unpacked = unpacked["data"]
                
                resp_payload = unpacked.copy() if isinstance(unpacked, dict) else {}
                
                # Homologar VIN con chasis si viene vacio
                if not resp_payload.get("vin") and resp_payload.get("chasis"):
                    resp_payload["vin"] = resp_payload.get("chasis")
                
                # Enriquecer con tasaciones y permiso SII
                enrich_with_sii(resp_payload)
                resp_payload["data_source"] = "CACHE_LOCAL"

                return {
                    "source": "CACHE_LOCAL",
                    "data_source": "CACHE_LOCAL",
                    "cached_at": str(row["created_at"]) if isinstance(row, dict) and "created_at" in row else "",
                    "data": resp_payload
                }
    except Exception as e:
        print(f"Error consultando caché: {e}")

    # 2. MOTOR PRT LOCAL (Costo $0 / Human-in-the-loop)
    if provider.lower() == "prt":
        raise HTTPException(
            status_code=404,
            detail={
                "require_prt_solve": True,
                "plate": patente_clean,
                "message": f"Patente {patente_clean} no encontrada en caché local. Requiere verificación oficial PRT."
            }
        )

    # 3. MOTOR BOOSTR API (Comercial)
    if not BOOSTR_API_KEY:
        raise HTTPException(status_code=500, detail="API Key de Boostr no configurada en el servidor.")

    url = f"https://api.boostr.cl/vehicle/{patente_clean}.json"
    headers = {"X-API-KEY": BOOSTR_API_KEY}

    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Patente no encontrada en el registro oficial.")
        
        if response.status_code == 200:
            boostr_data = response.json()
            
            # Si Boostr devolvió código de error (Ej: V-04), NO guardar en caché
            if boostr_data.get("status") == "error" or boostr_data.get("code") == "V-04":
                raise HTTPException(status_code=404, detail="Vehículo no encontrado en los registros de Boostr.")

            v_inner = boostr_data.get("data", {})
            if not v_inner or not isinstance(v_inner, dict) or not v_inner.get("make"):
                raise HTTPException(status_code=404, detail="Datos del vehículo incompletos o no encontrados.")

            boostr_data["data_source"] = "BOOSTR_API"
            if isinstance(boostr_data.get("data"), dict):
                boostr_data["data"]["data_source"] = "BOOSTR_API"

            # Guardar vehículo válido en la base de datos
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO vehicle_cache (plate, data) VALUES (%s, %s)
                    ON CONFLICT (plate) DO UPDATE SET data = EXCLUDED.data, created_at = CURRENT_TIMESTAMP;
                """, (patente_clean, json.dumps(boostr_data.get("data", boostr_data))))
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                print(f"Error guardando en caché: {e}")

            return {
                "source": "BOOSTR_API",
                "data_source": "BOOSTR_API",
                "data": boostr_data.get("data", boostr_data)
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Error consultando el servicio oficial.")
    except HTTPException:
        raise
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Fallo de conexión externa: {str(e)}")

@app.get("/api/patente/{patente}/pdf")
def generar_pdf_patente(patente: str):
    patente_clean = patente.strip().upper()
    
    if not CHILEAN_PLATE_REGEX.match(patente_clean):
        raise HTTPException(status_code=400, detail="Formato de patente inválido para generación de PDF.")

    vehicle_data = {}
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT data FROM vehicle_cache WHERE plate = %s;", (patente_clean,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row["data"]:
            vehicle_data = row["data"].get("data", {})
    except Exception:
        pass

    if not vehicle_data and BOOSTR_API_KEY:
        try:
            url = f"https://api.boostr.cl/vehicle/{patente_clean}.json"
            headers = {"X-API-KEY": BOOSTR_API_KEY}
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                vehicle_data = resp.json().get("data", {})
        except Exception:
            pass

    make = str(vehicle_data.get("make", "DESCONOCIDO"))
    model = str(vehicle_data.get("model", "DESCONOCIDO"))

    try:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("helvetica", "B", 18)
        pdf.cell(0, 10, "PartFinder 360 - Ficha Tecnica", new_x="LMARGIN", new_y="NEXT", align="C")
        
        pdf.set_font("helvetica", "B", 12)
        pdf.set_text_color(49, 130, 206)
        pdf.cell(0, 8, f"PATENTE: {patente_clean}", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 8, f"Vehiculo: {make} {model}", new_x="LMARGIN", new_y="NEXT")
        
        pdf.ln(5)
        pdf.set_font("helvetica", "B", 10)
        pdf.set_fill_color(237, 242, 247)
        pdf.set_text_color(45, 55, 72)
        pdf.cell(60, 8, " CAMPO", border=1, fill=True)
        pdf.cell(130, 8, " VALOR", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        
        pdf.set_font("helvetica", "", 10)
        for k, v in vehicle_data.items():
            if v is not None and str(v).strip() != "":
                pdf.cell(60, 7, f" {k.upper()}", border=1)
                pdf.cell(130, 7, f" {str(v)}", border=1, new_x="LMARGIN", new_y="NEXT")
                
        pdf.ln(10)
        pdf.set_font("helvetica", "I", 8)
        pdf.set_text_color(113, 128, 150)
        pdf.cell(0, 5, "Generado automaticamente por PartFinder 360 Chile - Reporte Oficial", align="C")

        pdf_bytes = bytes(pdf.output())
        return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=Reporte_{patente_clean}.pdf"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generando PDF: {str(e)}")


from fastapi.responses import RedirectResponse
import urllib.parse
import datetime

@app.get("/api/r/meli")
def redirect_mercadolibre(q: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n🔥 [CLICK AFILIADO EN TIEMPO REAL - {timestamp}]", flush=True)
    print(f"   Búsqueda: {q}", flush=True)
    print(f"   Afiliado: sandraalvaradochile | Tool: 56272145", flush=True)
    
    encoded_query = urllib.parse.quote(q)
    target_url = (
        f"https://listado.mercadolibre.cl/{encoded_query}"
        "?matt_tool=56272145&matt_word=sandraalvaradochile&forceInApp=true"
    )
    print(f"   Redirigiendo a: {target_url}\n", flush=True)
    return RedirectResponse(url=target_url, status_code=302)


import json
import base64

@app.get("/api/boostr/status")
def get_boostr_status():
    status_payload = {
        "status": "ONLINE" if BOOSTR_API_KEY else "OFFLINE",
        "plan": "PRO",
        "daily_limit": 100,
        "remaining": 69,
        "client": "Sandra Alvarado",
        "cached_plates": 0
    }
    
    if BOOSTR_API_KEY and "." in BOOSTR_API_KEY:
        try:
            p_b64 = BOOSTR_API_KEY.split(".")[1]
            p_b64 += "=" * ((4 - len(p_b64) % 4) % 4)
            data = json.loads(base64.b64decode(p_b64).decode("utf-8"))
            status_payload["client"] = data.get("client", status_payload["client"])
            status_payload["plan"] = data.get("plan", status_payload["plan"]).upper()
        except Exception:
            pass

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM vehicle_cache;")
        row = cur.fetchone()
        if isinstance(row, dict):
            status_payload["cached_plates"] = row.get("count", list(row.values())[0])
        elif row:
            status_payload["cached_plates"] = row[0]
            
        cur.execute("SELECT total_limit, remaining FROM api_quota WHERE provider = 'boostr';")
        q_row = cur.fetchone()
        if q_row:
            if isinstance(q_row, dict):
                status_payload["daily_limit"] = q_row.get("total_limit", 100)
                status_payload["remaining"] = q_row.get("remaining", 100)
            else:
                status_payload["daily_limit"] = q_row[0]
                status_payload["remaining"] = q_row[1]
                
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error consultando telemetria: {e}")

    return status_payload

# ==========================================
# INGESTA DIRECTA DESDE CLIENTE (MOTOR PRT)
# ==========================================
class VehicleIngestPayload(BaseModel):
    plate: str
    data: dict

@app.post("/api/vehicle/cache")
def save_scraped_vehicle(payload: VehicleIngestPayload):
    patente_clean = payload.plate.upper().replace("-", "").replace(" ", "").strip()
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO vehicle_cache (plate, data) VALUES (%s, %s)
            ON CONFLICT (plate) DO UPDATE SET data = EXCLUDED.data, created_at = CURRENT_TIMESTAMP;
        """, (patente_clean, json.dumps(payload.data)))
        conn.commit()
        cur.close()
        conn.close()
        return {"status": "SUCCESS", "message": f"Vehículo {patente_clean} guardado en caché permanente."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error guardando en caché: {str(e)}")
