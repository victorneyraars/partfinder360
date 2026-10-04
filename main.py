from pydantic import BaseModel
from typing import Optional

import os
import json
import re
import requests
import asyncio
import hashlib
import time as _time
import unicodedata
from datetime import datetime, date
from html import unescape as html_unescape
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from fpdf import FPDF

# Admin API (protegido con JWT)
from admin import router as admin_router  # noqa: E402
from usage import router as usage_router  # noqa: E402
from admin_tests import router as admin_tests_router  # noqa: E402

# Cliente HTTP dedicado para el microservicio PRT.
import prt_client


def _parse_fecha_es(s: str):
    """Parsea dd/mm/yyyy o dd-mm-yyyy → date; None si es inválida."""
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _eval_prt_vigencia(data: dict):
    """
    Política de vigencia autónoma del backend.
    Devuelve (vigente: bool, motivo: str).

    VIGENTE  = rt_estado contiene 'aprobad'/'vigente' Y rt_vencimiento >= hoy.
    VENCIDA/RECHAZADA/sin datos = no vigente (requiere verificación fresca).
    Un registro no vigente NUNCA bloquea consultas futuras: se re-evalúa en
    cada GET y solo pasa a hit cuando la revisión quede VIGENTE.
    """
    if not isinstance(data, dict):
        return False, "sin_datos"
    estado = str(data.get("rt_estado") or "").strip().lower()
    aprobado = ("aprobad" in estado) or ("vigente" in estado)
    if not aprobado:
        return False, "rt_no_aprobada" if estado else "rt_sin_estado"
    venc = str(data.get("rt_vencimiento") or "").strip()
    d = _parse_fecha_es(venc)
    if d is None:
        return False, "rt_sin_vencimiento"
    hoy = date.today()
    if d < hoy:
        return False, "rt_vencida"
    return True, "rt_vigente"


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
              AND anio_fabricacion = %s
            ORDER BY tasacion_fiscal ASC;
        """, (f"%{m_base}%", m_base, mod_pattern, mod_pattern, mod_pattern, concat_brand_token, int(anio)))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if rows:
            tasaciones = [r["tasacion_fiscal"] for r in rows if r.get("tasacion_fiscal")]
            permisos = [r["permiso_circulacion"] for r in rows if r.get("permiso_circulacion")]
            best = rows[0]
            vehicle_dict["sii"] = {
                "codigo_sii": best.get("codigo_sii"),
                "tasacion_fiscal": best.get("tasacion_fiscal"),
                "permiso_circulacion": best.get("permiso_circulacion"),
                "tasacion_min": min(tasaciones) if tasaciones else None,
                "tasacion_max": max(tasaciones) if tasaciones else None,
                "permiso_min": min(permisos) if permisos else None,
                "permiso_max": max(permisos) if permisos else None,
                "combustible": best.get("combustible"),
                "cilindrada": f"{best.get('cilindrada')} cc" if best.get("cilindrada") else None,
                "transmision": best.get("transmision"),
                "versiones_count": len(rows),
                "versiones": rows[:5]
            }
            if not vehicle_dict.get("combustible") and best.get("combustible"):
                vehicle_dict["combustible"] = best.get("combustible")
            if not vehicle_dict.get("cilindrada") and best.get("cilindrada"):
                vehicle_dict["cilindrada"] = f"{best.get('cilindrada')} cc"
            if not vehicle_dict.get("transmision") and best.get("transmision"):
                vehicle_dict["transmision"] = best.get("transmision")
    except Exception as e:
        print(f"Error enriqueciendo con SII: {e}")
    return vehicle_dict
app = FastAPI(title="PartFinder 360 API")

# Admin API
app.include_router(admin_router)
app.include_router(usage_router)
app.include_router(admin_tests_router)

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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS boostr_pending_queue (
            plate VARCHAR(20) PRIMARY KEY,
            status VARCHAR(20) DEFAULT 'QUEUED',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP
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


@app.get("/privacidad.html", include_in_schema=False)
@app.get("/privacidad", include_in_schema=False)
def pagina_privacidad():
    """Página legal y de descargo para Google Play / publicación."""
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "privacidad.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/html")
    except Exception as e:
        return Response(content=f"<h1>Política de Privacidad</h1><p>PartFinder 360 - Studio Digital 360. Contacto: privacidad@studiodigital360.com</p><p>Error: {e}</p>", media_type="text/html")


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



MTT_CONSULTA_URL = "https://apps.mtt.cl/consultaweb/default.aspx"
MTT_SERVICE_URL = os.getenv("MTT_SERVICE_URL", "http://mtt-service:3091")


def _mtt_field(name, html_text):
    m = re.search(
        r'name="%s"\s+id="[^"]*"\s+value="([^"]*)"' % re.escape(name), html_text
    ) or re.search(
        r'id="[^"]*"\s+name="%s"\s+value="([^"]*)"' % re.escape(name), html_text
    )
    return m.group(1) if m else ""


def _mtt_strip(v):
    return re.sub(r"\s+", " ", html_unescape(re.sub(r"<[^>]+>", " ", v or ""))).strip()


def _mtt_decode_response(resp):
    """Decodificación definitiva anti-mojibake de la respuesta MTT.

    apps.mtt.cl es ASP.NET clásico: los bytes pueden llegar en ISO-8859-1 /
    Windows-1252 aunque el header/meta declaren UTF-8 (o viceversa). Estrategia:

    1) Si el charset declarado es de la familia latin-1/Windows-1252, se
       decodifican los bytes crudos explícitamente con ese charset.
    2) En caso contrario (utf-8 declarado o sin declaración), se prueban
       utf-8, latin-1 y windows-1252 y se elige la decodificación "limpia"
       penalizando U+FFFD, pares mojibake (Ã + 0x80-0xBF) y caracteres de
       control. Así 'VEHÍCULO', 'PÚBLICO', 'BÁSICO', 'Región', 'Año' y
       'ACOMPAÑANTES' viajan siempre limpios al JSON.
    3) Reparación final de doble codificación (utf-8 leído como latin-1 y
       recodificado): 'PÃšBLICO' → 'PÚBLICO'.
    """
    raw = resp.content
    declared = None
    m = re.search(
        rb'charset\s*=\s*["\']?([\w-]+)',
        (resp.headers.get("content-type") or "").encode("latin-1", "ignore"),
        re.I,
    )
    if not m:
        m = re.search(rb'charset\s*=\s*["\']?([\w-]+)', raw[:8192], re.I)
    if m:
        declared = m.group(1).decode("ascii", "ignore").lower()

    latin_family = ("iso-8859-1", "latin-1", "latin1", "windows-1252", "cp1252", "iso8859-1")
    if declared in latin_family:
        enc = "latin-1" if declared in ("latin-1", "latin1") else declared
        try:
            return _mtt_repair_mojibake(raw.decode(enc))
        except (UnicodeDecodeError, LookupError):
            return _mtt_repair_mojibake(raw.decode("latin-1", errors="replace"))

    def score(t):
        return (
            t.count("\ufffd") * 4
            + len(_MOJIBAKE_PAIR_RE.findall(t)) * 2
            + len(re.findall(r"[\x80-\x9f]", t))
        )

    variants = []
    for enc in ("utf-8", "windows-1252", "latin-1"):
        try:
            variants.append((score(t := raw.decode(enc)), enc, t))
        except UnicodeDecodeError:
            continue
    variants.sort(key=lambda x: x[0])
    return _mtt_repair_mojibake(variants[0][2])


_MOJIBAKE_PAIR_RE = re.compile(r"Ã[\x80-\xbf]")


def _mtt_repair_mojibake(text):
    """Repara doble codificación: 'PÃšBLICO' → 'PÚBLICO', 'ÑuÃ±oa' → 'Ñuñoa'."""
    if not _MOJIBAKE_PAIR_RE.search(text or ""):
        return text
    for enc in ("latin-1", "cp1252"):
        try:
            candidate = text.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if len(_MOJIBAKE_PAIR_RE.findall(candidate)) < len(_MOJIBAKE_PAIR_RE.findall(text)):
            return candidate
    return text


# ===== CAPA DE SANITIZACIÓN PROFUNDA (anti-mojibake forzado) =====
# Independientemente de cómo responda el portal, TODO texto (etiquetas y
# valores) pasa por este mapa de reemplazo explícito antes de serializar el
# JSON. Cubre las corrupciones clásicas de UTF-8 leído como latin-1/cp1252:
#   VEHÃ<SHY>CULO → VEHÍCULO, PÃšBLICO → PÚBLICO, BÃ<0x81>SICO → BÁSICO,
#   RegiÃ³n → Región, AÃ±o → Año, ACOMPAÃ'ANTES → ACOMPAÑANTES.
_MTT_MOJIBAKE_FIXES = {
    "\u00c3\u008d": "Í",  # Ã + 0x8D (SHY invisible)
    "\u00c3\u00cd": "Í",
    "\u00c3\u00ed": "í",
    "\u00c3\u009a": "Ú",  # Ã + 0x9A
    "\u00c3\u0161": "Ú",  # Ã + š (cp1252)
    "\u00c3\u00da": "Ú",
    "\u00c3\u00fa": "ú",
    "\u00c3\u0081": "Á",  # Ã + 0x81
    "\u00c3\u00a1": "á",
    "\u00c3\u2030": "É",  # Ã + ‰ (cp1252)
    "\u00c3\u00c9": "É",
    "\u00c3\u00e9": "é",
    "\u00c3\u201c": "Ó",  # Ã + “ (cp1252)
    "\u00c3\u00d3": "Ó",
    "\u00c3\u00f3": "ó",
    "\u00c3\u2018": "Ñ",  # Ã + ‘ (cp1252)
    "\u00c3\u00d1": "Ñ",
    "\u00c3\u00f1": "ñ",
    "P\u00c3\u0161BLICO": "PÚBLICO",
    "P\u00c3\u009aBLICO": "PÚBLICO",
    "B\u00c3\u0081SICO": "BÁSICO",
    "B\u00c3\u00a1SICO": "BÁSICO",
    "VEH\u00c3\u008dCULO": "VEHÍCULO",
    "VEH\u00c3\u00cdCULO": "VEHÍCULO",
    "ACOMPA\u00c3\u2018ANTES": "ACOMPAÑANTES",
    "ACOMPA\u00c3\u0091ANTES": "ACOMPAÑANTES",
}


def _mtt_fix_mojibake_pairs(text):
    """Barrido genérico: cualquier par 'Ã' + byte-latino → carácter correcto
    vía round-trip cp1252/latin-1 → utf-8."""
    def repl(m):
        pair = m.group(0)
        for enc in ("cp1252", "latin-1"):
            try:
                return pair.encode(enc).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        return pair

    return re.sub(
        r"Ã[\x80-\xbf\u0161\u2030\u201c\u201d\u2018\u2019\u2122\u017d\u017e\u0152\u0153\u009a]",
        repl,
        text,
    )


def _mtt_deep_sanitize(obj):
    """Recorre TODA la estructura (etiquetas, valores, títulos, items) y
    fuerza la limpieza de mojibake + trim + colapso de espacios."""
    if isinstance(obj, str):
        t = obj
        for k, v in _MTT_MOJIBAKE_FIXES.items():
            t = t.replace(k, v)
        t = _mtt_fix_mojibake_pairs(t)
        t = _mtt_repair_mojibake(t)
        return re.sub(r"\s+", " ", t).strip()
    if isinstance(obj, list):
        return [_mtt_deep_sanitize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _mtt_deep_sanitize(v) for k, v in obj.items()}
    return obj


# Títulos legibles para los bloques directos conocidos del formulario.
# Todo id MainContent_* desconocido se humaniza genéricamente (ver
# _mtt_humanize_id), de modo que campos imprevistos que MTT agregue a futuro
# se capturen sin tocar código.
_MTT_SPAN_TITLES = {
    "conductores": "CONDUCTORES",
    "adultoacompanates": "ADULTOS ACOMPAÑANTES",
    "notas": "NOTAS",
    "lblley20378": "LEY 20.378",
    "reemplazadopor": "REEMPLAZADO POR",
}


def _mtt_humanize_id(span_id):
    base = (span_id or "").strip()
    if base.lower().startswith("maincontent_"):
        base = base[len("MainContent_"):]
    base = re.sub(r"^lbl", "", base, flags=re.I)
    base = re.sub(r"(?<!^)(?=[A-ZÁÉÍÓÚÑ])", " ", base)
    return _mtt_strip(base).upper() or "INFORMACIÓN"


def _mtt_list_items(cell_html):
    """Convierte una celda de texto directo en items. Los separadores
    estructurales (<br>, <li>, filas de tablas anidadas, </div>, </h#>) se
    convierten en saltos ANTES de colapsar espacios, para no pegar entradas
    (p. ej. dos conductores en divs consecutivos)."""
    txt = re.sub(r"<br\s*/?>", "\n", cell_html or "", flags=re.I)
    txt = re.sub(r"</li>", "\n", txt, flags=re.I)
    txt = re.sub(r"</tr>", "\n", txt, flags=re.I)
    txt = re.sub(r"</t[dh]>", "\t", txt, flags=re.I)
    txt = re.sub(r"</div>", "\n", txt, flags=re.I)
    txt = re.sub(r"</h[1-6]>", "\n", txt, flags=re.I)
    items = []
    for line in txt.split("\n"):
        for part in line.split("\t"):
            part = _mtt_strip(part)
            if part:
                items.append(part)
    return items


def _mtt_sanitize_list_items(items, section_title):
    """Limpieza de entradas de lista:

    - trim + colapso de espacios múltiples (\\s+) en todos los textos.
    - Elimina la repetición del nombre de la sección dentro del valor
      (p. ej. 'CONDUCTORES ALEJANDRO ENRIQUE AGUILAR RIFFO' →
      'ALEJANDRO ENRIQUE AGUILAR RIFFO').
    - Separa conceptos pegados cuando ya son el encabezado (p. ej.
      'RENOVACION POR CANCELACION ESTE VEHICULO REEMPLAZA A DXBL16' →
      'ESTE VEHICULO REEMPLAZA A DXBL16').
    """
    title = re.sub(r"\s+", " ", _mtt_strip(section_title)).upper()
    clean = []
    for it in items or []:
        t = re.sub(r"\s+", " ", _mtt_strip(it)).strip()
        if not t:
            continue
        # 1) Quitar repetición del encabezado de la sección como prefijo.
        t = re.sub(
            r"^" + re.escape(title) + r"\s*(?:[:.\-–—]\s*)?",
            "",
            t,
            flags=re.I,
        ).strip()
        # 2) Quitar conceptos que ya representa el encabezado, aunque vengan
        #    pegados sin separador.
        t = re.sub(
            r"^(?:RENOVACI[OÓ]N\s*POR\s*CANCELACI[OÓ]N)\s*(?:[:.\-–—]\s*)?",
            "",
            t,
            flags=re.I,
        ).strip()
        t = re.sub(r"\s+", " ", t).strip()
        if t and t.upper() != title:
            clean.append(t)
    return clean


def _mtt_parse_result_table(page_html):
    """Parser 100% dinámico de la tabla de resultados del RNSTP.

    - <th> (o fila de cabecera gris) inicia una `seccion`.
    - Filas de 2 celdas → items tipo par {"etiqueta", "valor"}; sin valor
      se registra igualmente "No registra".
    - Filas de una celda (colspan, bloques tipo CONDUCTORES/RENOVACION) →
      sección tipo "lista" con entradas directas.
    Cualquier campo nuevo que MTT publique cae automáticamente en `secciones`
    sin cambios de código.
    """
    secciones = []
    m = re.search(
        r'<table[^>]*id="MainContent_tablaDatos"[^>]*>(.*?)</table>',
        page_html,
        re.S | re.I,
    )
    if not m:
        return secciones
    current = None
    for trow in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S | re.I):
        th = re.search(r"<th[^>]*>(.*?)</th>", trow, re.S | re.I)
        if th:
            title = _mtt_strip(th.group(1))
            if title:
                current = {"titulo": title, "tipo": "pares", "items": []}
                secciones.append(current)
            continue

        cells = re.findall(r"<td[^>]*>(.*?)</td>", trow, re.S | re.I)
        if len(cells) >= 2:
            label = _mtt_strip(cells[0])
            value = _mtt_strip(cells[1])
            if not label and not value:
                continue
            if current is None:
                current = {"titulo": "INFORMACIÓN DEL VEHÍCULO", "tipo": "pares", "items": []}
                secciones.append(current)
            current["items"].append(
                {"etiqueta": label or "Dato", "valor": value or "No registra"}
            )
        elif len(cells) == 1:
            sid = re.search(r'id="MainContent_([A-Za-z0-9]+)"', cells[0], re.I)
            if sid:
                key = sid.group(1).lower()
                # El portal repite el título dentro de la celda (p. ej.
                # <h3>CONDUCTORES</h3>): se usa para confirmar el título y se
                # retira del contenido para no duplicarlo en los items.
                h3 = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", cells[0], re.S | re.I)
                titulo = _MTT_SPAN_TITLES.get(key) or (
                    _mtt_strip(h3.group(1)) if h3 else _mtt_humanize_id(sid.group(1))
                )
                cell_sin_h3 = re.sub(
                    r"<h[1-6][^>]*>.*?</h[1-6]>", "", cells[0], flags=re.S | re.I
                )
                items = _mtt_sanitize_list_items(_mtt_list_items(cell_sin_h3), titulo)
                # Bloques condicionales (reemplazo / ley 20.378) solo se
                # emiten si traen contenido; conductores/acompañantes/notas
                # se emiten siempre (la app muestra "No registra datos").
                if not items and key in ("reemplazadopor", "lblley20378"):
                    continue
                secciones.append({
                    "titulo": titulo,
                    "tipo": "lista",
                    "items": items,
                })
            else:
                items = _mtt_list_items(cells[0])
                if items:
                    if current is None or current.get("tipo") != "lista":
                        current = {"titulo": "INFORMACIÓN ADICIONAL", "tipo": "lista", "items": []}
                        secciones.append(current)
                    current["items"].extend(
                        _mtt_sanitize_list_items(items, current["titulo"])
                    )
    return secciones


def _mtt_derive_flat(secciones):
    """Deriva los campos planos legacy (para compatibilidad y TTL) desde las
    secciones dinámicas, priorizando la sección de servicio."""
    flat = {
        "tipo_servicio": "",
        "estado_servicio": "",
        "region": "",
        "folio_flota": "",
        "fecha_vencimiento_permiso": "",
    }

    def scan(require_servicio_section=False):
        for sec in secciones or []:
            titulo = (sec.get("titulo") or "").lower()
            es_servicio = "servicio" in titulo
            for it in sec.get("items") or []:
                if not isinstance(it, dict):
                    continue
                label = (it.get("etiqueta") or "").lower()
                valor = (it.get("valor") or "").strip()
                if require_servicio_section and not es_servicio:
                    continue
                if ("tipo de servicio" in label) and not flat["tipo_servicio"]:
                    flat["tipo_servicio"] = valor
                elif ("estado" in label and "servicio" in label) and not flat["estado_servicio"]:
                    flat["estado_servicio"] = valor
                elif "regi" in label and not flat["region"]:
                    flat["region"] = valor
                elif ("folio" in label or "flota" in label) and not flat["folio_flota"]:
                    flat["folio_flota"] = valor
                elif ("vencimiento" in label and "servicio" in label) and not flat["fecha_vencimiento_permiso"]:
                    flat["fecha_vencimiento_permiso"] = valor

    # Pasada 1: coincidencias específicas.
    scan()
    # Pasada 2 (fallback): primer 'estado'/'vencimiento' donde sea.
    for sec in secciones or []:
        for it in sec.get("items") or []:
            if not isinstance(it, dict):
                continue
            label = (it.get("etiqueta") or "").lower()
            valor = (it.get("valor") or "").strip()
            if not flat["estado_servicio"] and "estado" in label:
                flat["estado_servicio"] = valor
            if not flat["fecha_vencimiento_permiso"] and "vencimiento" in label:
                flat["fecha_vencimiento_permiso"] = valor
    return flat


def _scrape_mtt(plate):
    """Consulta el Registro Nacional de Servicios de Transporte de Pasajeros
    y Escolar (RNSTP) del MTT vía ASP.NET WebForms.

    Devuelve la estructura dinámica `secciones` (titulo/tipo/items) extraída
    del contenedor de resultados sin hardcodear columnas, más los campos
    planos legacy derivados de las mismas secciones.
    """
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                         "Accept": "text/html,application/xhtml+xml"})

    r1 = sess.get(MTT_CONSULTA_URL, timeout=5)
    if r1.status_code != 200:
        raise RuntimeError(f"MTT GET fallo con {r1.status_code}")
    page = _mtt_decode_response(r1)

    payload = {
        "ctl00$MainContent$ppu": plate,
        "ctl00$MainContent$btn_buscar": "Buscar",
        "__VIEWSTATE": _mtt_field("__VIEWSTATE", page),
        "__VIEWSTATEGENERATOR": _mtt_field("__VIEWSTATEGENERATOR", page),
        "__EVENTVALIDATION": _mtt_field("__EVENTVALIDATION", page),
    }
    r2 = sess.post(MTT_CONSULTA_URL, data=payload, timeout=5)
    if r2.status_code != 200:
        raise RuntimeError(f"MTT POST fallo con {r2.status_code}")
    page = _mtt_decode_response(r2)
    text = re.sub(r"\s+", " ", html_unescape(re.sub(r"<[^>]+>", " ", page)))

    out = {
        "es_transporte_publico": True,
        "tipo_servicio": "",
        "estado_servicio": "",
        "region": "",
        "folio_flota": "",
        "fecha_vencimiento_permiso": "",
        "secciones": [],
    }
    if re.search(r"no pertenece al Registro Nacional|no se encuentra registrado en el Registro Nacional", text, re.I):
        out["es_transporte_publico"] = False
        msg = re.search(r'<span id="MainContent_msg"[^>]*>(.*?)</span>', page, re.S | re.I)
        mensaje = _mtt_strip(msg.group(1)) if msg else (
            "El vehículo no pertenece al Registro Nacional de Servicios de "
            "Transporte de Pasajeros ni al Registro Nacional de Servicios de "
            "Transporte Escolar."
        )
        out["secciones"] = [{
            "titulo": "RESULTADO DE CONSULTA",
            "tipo": "lista",
            "items": [mensaje] if mensaje else [],
        }]
        return _mtt_deep_sanitize(out)

    secciones = _mtt_parse_result_table(page)
    out["secciones"] = secciones
    out.update(_mtt_derive_flat(secciones))
    # Sanitización profunda final: garantiza acentos y ñ limpios en el JSON
    # aunque el portal entregue bytes mixtos o doble codificados.
    return _mtt_deep_sanitize(out)


def _sii_normalize(s):
    """Normaliza cadenas para la comparación con las tablas oficiales:
    sin tildes, mayúsculas, trim y colapso de espacios."""
    if not s:
        return ""
    s = re.sub(r"\s+", " ", str(s)).strip().upper()
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


# Sufijos/prefijos comerciales que se eliminan de la marca antes de buscar
# en las tablas SII (ej. 'KIA MOTORS' → 'KIA', 'HYUNDAI MOTOR' → 'HYUNDAI').
_SII_MARCA_SUFIJOS = {"MOTORS", "MOTOR", "CHILE", "AUTO", "AUTOMOTRIZ", "VEHICULOS"}


def _sii_clean_marca(marca):
    """Limpia la marca de sufijos/prefijos comerciales por token."""
    if not marca:
        return ""
    tokens = [t for t in _sii_normalize(marca).split() if t not in _SII_MARCA_SUFIJOS]
    return " ".join(tokens)


def _sii_parse_cc(v):
    """Normaliza cilindrada de la ficha base a cc enteros: '1.2' → 1200,
    '1197 cc' → 1197, '1600' → 1600."""
    if v in (None, ""):
        return None
    s = str(v).strip().lower().replace("cc", "").replace("c.c.", "")
    m = re.search(r"(\d+(?:[.,]\d+)?)", s)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    if num < 100:
        num *= 1000
    return int(round(num))


def _sii_clp(n):
    """Formatea un monto CLP a '$X.XXX.XXX' (estilo chileno)."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return "$0"
    s = str(n)
    out = []
    for i, ch in enumerate(s):
        rem = len(s) - i
        out.append(ch)
        if rem > 1 and (rem - 1) % 3 == 0:
            out.append(".")
    return "$" + "".join(out)


def _sii_query_rows(marca, modelo, anio):
    """Consulta las tablas oficiales con matching TOLERANTE:
    - marca: limpia de sufijos comerciales ('KIA MOTORS' → 'KIA') y match
      exacto o parcial bidireccional contra la tabla.
    - modelo: exacto, ILIKE bidireccional y comparación por RAÍZ
      alfanumérica (sin espacios ni caracteres especiales) en ambas
      direcciones: 'I10' matchea 'I 10', 'I-10' y 'GRAND I10'.
    Devuelve lista de dicts."""
    marca_limpia = _sii_clean_marca(marca)
    raiz = re.sub(r"[^A-Z0-9]", "", modelo or "")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT codigo_sii, categoria, tipo_vehiculo, marca, modelo, version,
               anio_fabricacion, tasacion_fiscal, permiso_circulacion,
               anio_tributario, cilindrada, combustible, transmision,
               puertas, equipamiento
        FROM sii_tasaciones
        WHERE (
              marca = %s OR marca ILIKE %s OR %s ILIKE (marca || '%%')
              OR marca = %s OR marca ILIKE %s OR %s ILIKE (marca || '%%')
          )
          AND anio_fabricacion = %s
          AND (
              modelo = %s
              OR modelo ILIKE %s
              OR %s ILIKE (modelo || '%%')
              OR regexp_replace(modelo, '[^A-Z0-9]', '', 'g') = %s
              OR regexp_replace(modelo, '[^A-Z0-9]', '', 'g') LIKE %s
              OR %s LIKE (regexp_replace(modelo, '[^A-Z0-9]', '', 'g') || '%%')
          )
        """,
        (
            marca, f"%{marca}%", marca,
            marca_limpia or "\x00", f"%{marca_limpia}%" if marca_limpia else "\x00",
            marca_limpia or "\x00",
            anio,
            modelo, f"%{modelo}%", modelo,
            raiz or "\x00", f"%{raiz}%" if raiz else "\x00", raiz or "\x00",
        ),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def _resolver_tasacion_sii(patente, marca_q="", modelo_q="", anio_q="", cilindrada_q=""):
    """Motor local de tasación SII (Decreto Exento): resuelve marca/modelo/
    año del vehículo y consulta la tabla `sii_tasaciones`.

    - 1 fila  → exact_match: true con montos y código exactos.
    - N filas → exact_match: false con rangos y lista de versiones
      enriquecida (cilindrada/combustible/transmisión/equipamiento...).
    - 0 filas → 404 "Vehículo no tipificado en tablas oficiales SII".
    """
    marca = _sii_normalize(marca_q)
    modelo = _sii_normalize(modelo_q)
    cilindrada_ficha = _sii_parse_cc(cilindrada_q)
    anio = None
    if anio_q:
        m = re.search(r"\d{4}", str(anio_q))
        if m:
            anio = int(m.group(0))

    def _extraer_anio(v):
        m = re.search(r"\d{4}", str(v or ""))
        return int(m.group(0)) if m else None

    # 1) Ficha base interna (vehicle_cache: PRT/Boostr/MTT ya consultados).
    if not (marca and modelo and anio):
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT data FROM vehicle_cache WHERE plate = %s;", (patente,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                d = row["data"] if isinstance(row, dict) else row[0]
                if isinstance(d, dict):
                    if not marca:
                        marca = _sii_normalize(d.get("marca") or d.get("make"))
                    if not modelo:
                        modelo = _sii_normalize(d.get("modelo") or d.get("model"))
                    if not anio:
                        anio = _extraer_anio(d.get("anio") or d.get("year"))
                    if cilindrada_ficha is None:
                        cilindrada_ficha = _sii_parse_cc(
                            d.get("cilindrada") or d.get("engine_size")
                        )
        except Exception as e:
            print(f"SII: error leyendo ficha base: {e}")

    # 2) Respaldo: ficha desde Boostr API.
    if not (marca and modelo and anio) and BOOSTR_API_KEY:
        try:
            r = requests.get(
                f"https://api.boostr.cl/vehicle/{patente}.json",
                headers={"X-API-KEY": BOOSTR_API_KEY},
                timeout=15,
            )
            _update_boostr_quota_from_response(r)
            if r.status_code == 200:
                raw = r.json()
                d = (raw.get("data") or raw) if isinstance(raw, dict) else {}
                if isinstance(d, dict):
                    marca = marca or _sii_normalize(d.get("make"))
                    modelo = modelo or _sii_normalize(d.get("model"))
                    if not anio:
                        anio = _extraer_anio(d.get("year"))
                    if cilindrada_ficha is None:
                        cilindrada_ficha = _sii_parse_cc(d.get("engine_size"))
        except Exception as e:
            print(f"SII: Boostr falló: {e}")

    if not (marca and modelo and anio):
        raise HTTPException(
            status_code=404,
            detail={
                "error": "vehiculo_no_identificado",
                "message": "No se pudo identificar marca/modelo/año del vehículo para consultar las tablas SII.",
            },
        )

    # 3) Consulta en tablas oficiales (primero modelo completo).
    rows = _sii_query_rows(marca, modelo, anio)
    if not rows:
        # Tolerancia: primer token NO genérico del modelo (p. ej.
        # 'RAV4 HYBRID...' → 'RAV4'; 'GRAND I10' → 'I10', nunca 'GRAND').
        stopwords = {"GRAND", "GRAN", "NEW", "NUEVO", "NUEVA", "ALL", "THE", "DE", "DEL"}
        tokens = [t for t in modelo.split() if t not in stopwords]
        first_token = tokens[0] if tokens else ""
        if first_token and first_token != modelo:
            rows = _sii_query_rows(marca, first_token, anio)

    if not rows:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_tipificado",
                "message": "Vehículo no tipificado en tablas oficiales SII",
            },
        )

    # 4) Priorización: match exacto de modelo primero, luego coincidencia de
    #    cilindrada de la ficha base (si aplica), luego versión. SIEMPRE se
    #    retorna la lista completa de variantes del año.
    cc_ficha = _sii_parse_cc(cilindrada_ficha)
    raiz_modelo = re.sub(r"[^A-Z0-9]", "", modelo or "")

    def _sort_key(r):
        modelo_exacto = (r["modelo"] or "") == modelo
        raiz_row = re.sub(r"[^A-Z0-9]", "", r["modelo"] or "")
        raiz_exacta = bool(raiz_modelo) and raiz_row == raiz_modelo
        cc_ok = (
            cc_ficha is not None
            and r.get("cilindrada") is not None
            and int(r["cilindrada"]) == cc_ficha
        )
        return (
            0 if modelo_exacto else (1 if raiz_exacta else 2),
            0 if cc_ok else 1,
            (r["version"] or ""),
        )

    rows = sorted(rows, key=_sort_key)

    def _version_payload(r):
        tas = int(r["tasacion_fiscal"] or 0)
        perm = int(r["permiso_circulacion"] or 0)
        trans = (r["transmision"] or "").strip().upper()
        trans_cod = "AUT" if "AUTO" in trans else ("MEC" if trans else "")
        cc = r.get("cilindrada")
        return {
            "codigo_sii": r["codigo_sii"],
            "marca": r["marca"],
            "modelo": r["modelo"],
            "version": r["version"],
            "descripcion": f"{r['modelo']} {r['version']}".strip(),
            "anio_fabricacion": r["anio_fabricacion"],
            "anio_tributario": r["anio_tributario"],
            "cilindrada": cc or 0,
            "cilindrada_match": bool(cc is not None and cc_ficha is not None and int(cc) == cc_ficha),
            "combustible": r["combustible"] or "",
            "transmision": r["transmision"] or "",
            "transmision_cod": trans_cod,
            "equipamiento": r.get("equipamiento") or "",
            "puertas": r.get("puertas") or 0,
            "tasacion": str(tas),
            "tasacion_formateada": _sii_clp(tas),
            "permiso": str(perm),
            "permiso_formateada": _sii_clp(perm),
            "categoria": r["categoria"],
        }

    versiones = [_version_payload(r) for r in rows]
    primero = rows[0]
    base = {
        "fuente": "SII",
        "patente": patente,
        "marca": primero["marca"],
        "modelo": primero["modelo"],
        "tipo_vehiculo": primero["tipo_vehiculo"],
        "categoria": primero["categoria"],
        "anio_tasacion": str(primero["anio_fabricacion"]),
        "anio_tributario": primero["anio_tributario"],
        "anio": str(primero["anio_fabricacion"]),
        "versiones": versiones,
        "datos_homologacion": {
            "marca": primero["marca"],
            "modelo": primero["modelo"],
            "version": primero["version"],
            "anio": str(primero["anio_fabricacion"]),
            "cilindrada": str(primero["cilindrada"] or ""),
            "combustible": primero["combustible"] or "",
            "transmision": primero["transmision"] or "",
        },
        # Campos ajenos al SII: vacíos explícitos.
        "rt_estado": "",
        "rt_vencimiento": "",
        "historial_rt": [],
        "es_transporte_publico": False,
        "tipo_servicio": "",
        "estado_servicio": "",
        "region": "",
        "folio_flota": "",
        "fecha_vencimiento_permiso": "",
        "secciones": [],
    }

    if len(rows) == 1:
        base.update({
            "exact_match": True,
            "codigo_sii": primero["codigo_sii"],
            "tasacion_fiscal": str(primero["tasacion_fiscal"]),
            "permiso_circulacion": str(primero["permiso_circulacion"]),
        })
    else:
        tasaciones = [r["tasacion_fiscal"] for r in rows]
        permisos = [r["permiso_circulacion"] for r in rows]
        base.update({
            "exact_match": False,
            "rango_tasacion": {"min": str(min(tasaciones)), "max": str(max(tasaciones))},
            "rango_permiso": {"min": str(min(permisos)), "max": str(max(permisos))},
        })

    # 4) Persistir en la base centralizada (merge con la ficha existente).
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT data FROM vehicle_cache WHERE plate = %s;", (patente,))
        row = cur.fetchone()
        existing = {}
        if row:
            d = row["data"] if isinstance(row, dict) else row[0]
            if isinstance(d, dict):
                existing = d
        fuente_prev = existing.get("fuente") if isinstance(existing, dict) else None
        merged = {**existing, **base}
        # No pisar la fuente original de la ficha (PRT/Boostr/MTT): la
        # tasación SII es un enriquecimiento, no un reemplazo de origen.
        if fuente_prev and base.get("fuente") == "SII":
            merged["fuente"] = fuente_prev
        # Restaurar campos de la ficha original que el payload SII deja
        # vacíos como marcador (RT, MTT...): no deben blanquearse.
        if isinstance(existing, dict):
            for k in (
                "rt_estado", "rt_vencimiento", "historial_rt", "secciones",
                "es_transporte_publico", "tipo_servicio", "estado_servicio",
                "region", "folio_flota", "fecha_vencimiento_permiso",
            ):
                ev = existing.get(k)
                bv = base.get(k)
                if ev not in (None, "", []) and bv in (None, "", []):
                    merged[k] = ev
        cur.execute(
            """
            INSERT INTO vehicle_cache (plate, data) VALUES (%s, %s)
            ON CONFLICT (plate) DO UPDATE SET data = EXCLUDED.data, created_at = CURRENT_TIMESTAMP;
            """,
            (patente, json.dumps(merged)),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"SII: error guardando en vehicle_cache: {e}")

    return {
        "source": "SII",
        "data_source": "SII",
        "data": base,
        "cache_hit": True,
        "requiere_verificacion": False,
    }


def _boostr_enrich(v_inner: dict) -> dict:
    """Enriquecimiento completo del payload Boostr (payload íntegro + aliases
    + titular a nivel raíz + RT vacío explícito). NO muta la entrada."""
    out = dict(v_inner or {})
    out["fuente"] = "Boostr"
    out["owner_consultado"] = True
    owner = out.get("owner")
    if isinstance(owner, dict):
        out["owner_name"] = owner.get("fullname") or ""
        out["owner_rut"] = owner.get("documentNumber") or ""
    else:
        out.setdefault("owner_name", "")
        out.setdefault("owner_rut", "")
    out.setdefault("tipo", out.get("body_type") or out.get("type") or "")
    out.setdefault("nro_motor", out.get("engine") or "")
    out.setdefault("cilindrada", out.get("engine_size") or "")
    out.setdefault("combustible", out.get("gas_type") or "")
    out.setdefault("transmision", out.get("transmission") or "")
    out.setdefault("chasis", out.get("chassis") or "")
    out.setdefault("vin", out.get("chassis") or "")
    out.setdefault("kilometraje", out.get("kilometers") or 0)
    out.setdefault("pais", out.get("country") or "")
    out.setdefault("fabricante", out.get("manufacturer") or "")
    out.setdefault("marca", out.get("make") or "")
    out.setdefault("modelo", out.get("model") or "")
    if out.get("year") is not None:
        out.setdefault("anio", out.get("year"))
    out.setdefault("version_modelo", out.get("version") or "")
    out["rt_estado"] = ""
    out["rt_vencimiento"] = ""
    out["historial_rt"] = []
    out["rt_disponible"] = False
    return out




# ============================================================
# Boostr: actualizar cuota en BD desde headers de respuesta
# ============================================================
def _update_boostr_quota_from_response(response):
    """Boostr expone ratelimit-remaining, ratelimit-limit, ratelimit-reset
    en cada respuesta. Actualizamos la tabla api_quota con esos valores
    reales. Silencioso: nunca rompe el flujo si falla."""
    try:
        remaining = response.headers.get("ratelimit-remaining")
        limit = response.headers.get("ratelimit-limit")
        if remaining is None or limit is None:
            return
        try:
            remaining_i = int(remaining)
            limit_i = int(limit)
        except (ValueError, TypeError):
            return
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO api_quota (provider, total_limit, remaining, updated_at)
            VALUES ('boostr', %s, %s, NOW())
            ON CONFLICT (provider) DO UPDATE
            SET total_limit = EXCLUDED.total_limit,
                remaining = EXCLUDED.remaining,
                updated_at = EXCLUDED.updated_at
        """, (limit_i, remaining_i))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        try:
            print(f"[BOOSTR-QUOTA] error actualizando cuota: {e}")
        except Exception:
            pass


def _full_boostr(plate: str) -> dict:
    """Consulta Boostr para el dashboard: {status, data}. 429/PLAN_LIMIT →
    status 'queued' + encolado automático."""
    if not BOOSTR_API_KEY:
        return {"status": "error", "data": {}}
    try:
        r = requests.get(
            f"https://api.boostr.cl/vehicle/{plate}.json?include=owner",
            headers={"X-API-KEY": BOOSTR_API_KEY},
            timeout=15,
        )
        _update_boostr_quota_from_response(r)
        if r.status_code == 429 or "PLAN_LIMIT_EXCEEDED" in (r.text or "").upper():
            try:
                import boostr_queue_worker
                boostr_queue_worker.enqueue_plate(plate, "QUEUED")
            except Exception as e:
                print(f"/full: error encolando boostr {plate}: {e}")
            return {"status": "queued", "data": {}}
        if r.status_code != 200:
            return {"status": "error", "data": {}}
        data = r.json()
        # ===== MAPEO SEMÁNTICO DE INEXISTENCIA =====
        # Boostr responde HTTP 200 para patentes sin registro, con un payload
        # de nivel raíz tipo: {"status":"error","data":"","code":"V-02",
        # "message":"No encontramos datos asociados a la patente ingresada"}.
        # Eso es "no existe en padrón" (NO un error de servicio): se mapea a
        # un estado 'not_found' distinguible por Flutter para activar el
        # Caso B (estado global "sin registros en ninguna fuente") sin
        # confundirlo con un fallo de red o servicio caído.
        code = (data.get("code") or "").upper()
        message = (data.get("message") or "")
        if code == "V-02" or "No encontramos datos" in message:
            return {"status": "not_found", "data": {}}
        v_inner = data.get("data", {})
        if not v_inner or not isinstance(v_inner, dict) or not v_inner.get("make"):
            return {"status": "error", "data": {}}
        enriched = _boostr_enrich(v_inner)
        enriched.setdefault("patente", plate)
        enriched["data_source"] = "BOOSTR_API"
        return {"status": "ok", "data": enriched}
    except requests.exceptions.RequestException as e:
        print(f"/full: boostr red {plate}: {e}")
        return {"status": "error", "data": {}}


def _full_prt(plate: str) -> dict:
    """Consulta PRT para el dashboard: {status, data}.
    ORDEN CORREGIDO:
      1) vehicle_cache (datos REALES del WebView con captcha humano)
      2) prt-service microservicio (fallback para cuando este en modo real)
    El cache tiene prioridad para no depender del microservicio (que hoy
    corre en modo mock y devuelve datos ficticios para 4 patentes fijas).
    """
    from fastapi import HTTPException as _HTTPException

    # 1) PRIMERO: buscar en vehicle_cache (datos reales del WebView P2P)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT data FROM vehicle_cache WHERE plate = %s;", (plate,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            d = row["data"] if isinstance(row, dict) else row[0]
            if isinstance(d, dict) and (d.get("marca") or d.get("rt_estado") or d.get("historial_rt")):
                d["data_source"] = "PRT"
                d["fuente"] = d.get("fuente") or "PRT Oficial"
                return {"status": "ok", "data": d}
    except Exception as e:
        print(f"/full: prt cache {plate}: {e}")

    # 2) SEGUNDO: consultar microservicio PRT (para cuando este en modo real)
    try:
        result = prt_client.consultar_revision_tecnica(plate)
        vehicle = result.get("vehicle") or {}
        revision = result.get("revision_tecnica") or {}
        historial = revision.get("historial") or []
        data = {
            "patente": vehicle.get("patente") or plate,
            "marca": vehicle.get("marca") or "",
            "modelo": vehicle.get("modelo") or "",
            "anio": vehicle.get("anio") or "",
            "make": vehicle.get("marca") or "",
            "model": vehicle.get("modelo") or "",
            "year": vehicle.get("anio") or "",
            "vin": vehicle.get("vin") or "",
            "chasis": vehicle.get("vin") or vehicle.get("chasis") or "",
            "tipo": vehicle.get("tipoVehiculo") or "",
            "tipo_vehiculo": vehicle.get("tipoVehiculo") or "",
            "rt_estado": revision.get("estado") or "",
            "rt_vencimiento": revision.get("fechaUltimaRevision") or "",
            "historial_rt": [
                {
                    "fecha": (h.get("fecha") or "") if isinstance(h, dict) else "",
                    "cod_planta": (h.get("cod_planta") or "") if isinstance(h, dict) else "",
                    "planta": (h.get("planta") or "") if isinstance(h, dict) else "",
                    "certificado": (h.get("certificado") or "") if isinstance(h, dict) else "",
                    "vencimiento": (h.get("vencimiento") or "") if isinstance(h, dict) else "",
                    "estado": (h.get("estado") or "") if isinstance(h, dict) else "",
                    "kilometraje": (h.get("kilometraje") or "") if isinstance(h, dict) else "",
                    "observaciones": (h.get("observaciones") or "") if isinstance(h, dict) else "",
                    "es_gases": bool(h.get("es_gases")) if isinstance(h, dict) else False,
                }
                for h in historial
            ],
            "fuente": "PRT Oficial",
            "data_source": "PRT",
        }
        return {"status": "ok", "data": data}
    except _HTTPException:
        pass
    except Exception as e:
        print(f"/full: prt {plate}: {e}")

    return {"status": "error", "data": {}}


def _fetch_mtt_from_service(plate: str):
    """Consulta el microservicio mtt-service (con cache) para MTT.
    Devuelve el dict de datos o None si el servicio falla (fallback al
    scraper local en ese caso).
    """
    try:
        url = f"{MTT_SERVICE_URL.rstrip('/')}/api/v1/mtt/{plate}"
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            print(f"/full: mtt-service HTTP {r.status_code}")
            return None
        payload = r.json()
        if not isinstance(payload, dict) or not payload.get("success"):
            return None
        return payload.get("data") or None
    except Exception as e:
        print(f"/full: mtt-service error {plate}: {str(e)[:120]}")
        return None


def _full_mtt(plate: str) -> dict:
    """Consulta MTT para el dashboard: {status, data}.
    Orden: microservicio mtt-service (con cache) primero, scraper local
    como fallback si el servicio no responde.
    """
    result = _fetch_mtt_from_service(plate)
    if result is None:
        # Fallback al scraper local
        try:
            result = _scrape_mtt(plate)
        except Exception as e:
            print(f"/full: mtt scraper {plate}: {e}")
            return {"status": "error", "data": {}}
    try:
        data = _mtt_deep_sanitize({
            "patente": plate,
            "fuente": "MTT",
            "es_transporte_publico": bool(result.get("es_transporte_publico", False)),
            "tipo_servicio": result.get("tipo_servicio", ""),
            "estado_servicio": result.get("estado_servicio", ""),
            "region": result.get("region", ""),
            "folio_flota": result.get("folio_flota", ""),
            "fecha_vencimiento_permiso": result.get("fecha_vencimiento_permiso", ""),
            "secciones": result.get("secciones") or [],
            "rt_estado": "",
            "rt_vencimiento": "",
            "historial_rt": [],
        })
        data["data_source"] = "MTT_REGISTRO"
        return {"status": "ok", "data": data}
    except Exception as e:
        print(f"/full: mtt {plate}: {e}")
        return {"status": "error", "data": {}}


def _full_sii(plate: str) -> dict:
    """Consulta SII para el dashboard: {status, data}.

    Semántica de estado:
      - 'ok'         → tasación homologada encontrada (1+ versiones).
      - 'not_found'  → el portal respondió correctamente pero NO hay
                       tasación homologada para ese modelo/año (versiones
                       vacías: vehículos nuevos/recientes/recién homologados).
                       NO es un fallo de servicio.
      - 'error'      → caída real (red, timeout, DB, 5xx). Único caso que
                       justifica el banner "fuente no disponible".
    """
    try:
        result = _resolver_tasacion_sii(plate)
        data = result.get("data") or {}
        versiones = data.get("versiones") or []
        data["data_source"] = "SII_TASACION"
        data["fuente"] = "SII"
        if not versiones:
            # Respuesta válida pero sin tasación homologada (lista vacía).
            return {"status": "not_found", "data": {"versiones": []}}
        return {"status": "ok", "data": data}
    except HTTPException:
        # El motor local no encontró tasación homologada (vehículo no
        # tipificado o no identificado): resultado legítimamente vacío,
        # NO una caída del portal de tasaciones.
        return {"status": "not_found", "data": {"versiones": []}}
    except Exception as e:
        print(f"/full: sii {plate}: {e}")
        return {"status": "error", "data": {"versiones": []}}


def _full_fuel_efficiency(plate: str) -> dict:
    """Consulta 3CV / Eficiencia Energética (Boostr) para el dashboard.

    Semántica de estado:
      - 'ok'         → ficha 3CV homologada (lista de registros no vacía).
      - 'not_found'  → respuesta válida sin ficha de homologación (status
                       'error', code 'V-05' o lista vacía). NO es fallo.
      - 'error'      → caída real (red / timeout / 5xx).
    """
    if not BOOSTR_API_KEY:
        return {"status": "error", "data": {"registros": []}}
    try:
        r = requests.get(
            f"https://api.boostr.cl/vehicle/fuel_efficiency/{plate}.json",
            headers={"X-API-KEY": BOOSTR_API_KEY},
            timeout=15,
        )
        _update_boostr_quota_from_response(r)
        if r.status_code != 200:
            # HTTP no-200 del portal de consumo → caída real (5xx/timeout) o
            # ausencia documentada; sin body utilizable se mapea a not_found
            # solo si es claramente "sin datos", de lo contrario error.
            return {"status": "error", "data": {"registros": []}}
        raw = r.json()
        if not isinstance(raw, dict):
            return {"status": "error", "data": {"registros": []}}
        status = (raw.get("status") or "").lower()
        code = (raw.get("code") or "").upper()
        data = raw.get("data")
        registros = data if isinstance(data, list) else []
        if status == "success" and registros:
            return {"status": "ok", "data": {"registros": registros}}
        # status 'error', code 'V-05' o data vacío → sin ficha homologada.
        if status == "error" or code == "V-05" or not registros:
            return {"status": "not_found", "data": {"registros": []}}
        return {"status": "not_found", "data": {"registros": []}}
    except requests.exceptions.RequestException as e:
        print(f"/full: fuel_efficiency red {plate}: {e}")
        return {"status": "error", "data": {"registros": []}}
    except Exception as e:
        print(f"/full: fuel_efficiency {plate}: {e}")
        return {"status": "error", "data": {"registros": []}}


def _full_auto_seguro(plate: str) -> dict:
    """Consulta "Auto Seguro" (encargo por robo) — A DEMANDA.

    Esta fuente NO se consulta en el flujo inicial (evita forzar un segundo
    captcha). Se resuelve desde el dashboard vía WebView P2P; por lo tanto el
    agregador /full la entrega SIEMPRE en estado 'idle', con la metadata
    mínima para que Flutter pinte la tarjeta y su botón.
    """
    return {
        "status": "idle",
        "data": {
            "fuente": "Auto Seguro",
            "patente": plate,
        },
    }




# ============================================================
# Usage tracking: helper para registrar eventos del backend
# ============================================================
def _generate_fingerprint(request: Request) -> str:
    """Genera un device_id estable a partir de IP + User-Agent.
    Fallback para cuando el APK no envia device_id explicito."""
    try:
        ip = request.client.host if request.client else "unknown"
        ua = request.headers.get("user-agent", "")[:200]
        raw = f"{ip}|{ua}"
        return "fp-" + hashlib.sha256(raw.encode()).hexdigest()[:24]
    except Exception:
        return "fp-unknown"


def log_usage_event(
    device_id: Optional[str],
    event_type: str,
    plate: Optional[str] = None,
    metadata: Optional[dict] = None,
    request: Optional[Request] = None,
):
    """Registra un evento de uso en usage_events. Silencioso: nunca rompe
    el flujo principal si falla."""
    try:
        # Si no hay device_id, generar fingerprint desde request
        if not device_id and request is not None:
            device_id = _generate_fingerprint(request)
        if not device_id:
            return
        platform = "unknown"
        # 1. Si el device_id es un UUID (no fingerprint), es la app movil
        #    (Flutter envia UUID via UsageTracker). Dart no incluye "android"
        #    en el User-Agent, por eso no se detecta de ahi.
        if device_id and not device_id.startswith("fp-"):
            platform = "android"
        elif request is not None:
            ua = request.headers.get("user-agent", "").lower()
            if "android" in ua:
                platform = "android"
            elif "iphone" in ua or "ipad" in ua:
                platform = "ios"
            elif "curl" in ua or "python" in ua:
                platform = "server"
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO usage_events
              (device_id, event_type, plate, platform, metadata)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                device_id[:64],
                event_type[:32],
                (plate or "")[:10] or None,
                platform[:20],
                Json(metadata) if metadata else None,
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        try:
            print(f"[USAGE-LOG] error: {e}")
        except Exception:
            pass


@app.get("/api/patente/{patente}/full")
async def consulta_full(patente: str, request: Request, device_id: Optional[str] = None):
    """Dashboard unificado: consulta SIMULTÁNEA (asyncio.gather) de las 5
    fuentes oficiales (PRT, Boostr, MTT, SII, 3CV) + Auto Seguro en idle."""
    patente_clean = patente.strip().upper().replace("-", "").replace(" ", "")
    if not CHILEAN_PLATE_REGEX.match(patente_clean):
        raise HTTPException(
            status_code=400,
            detail="Formato de patente inválido. Verifique las normas oficiales de la PPU de Chile (Ej: ABCD12 o AB1234).",
        )

    _t0 = _time.time()
    results = await asyncio.gather(
        asyncio.to_thread(_full_prt, patente_clean),
        asyncio.to_thread(_full_boostr, patente_clean),
        asyncio.to_thread(_full_mtt, patente_clean),
        asyncio.to_thread(_full_sii, patente_clean),
        asyncio.to_thread(_full_fuel_efficiency, patente_clean),
        asyncio.to_thread(_full_auto_seguro, patente_clean),
        return_exceptions=True,
    )

    def _safe(idx):
        r = results[idx]
        if isinstance(r, BaseException):
            print(f"/full: excepción en fuente {idx}: {r}")
            return {"status": "error", "data": {}}
        return r or {"status": "error", "data": {}}

    # Tracking: registrar consulta del dashboard (con o sin cache)
    _dur_ms = int((_time.time() - _t0) * 1000)
    _sources = {}
    for _i, _key in enumerate(["prt", "boostr", "mtt", "sii", "fuel_efficiency", "auto_seguro"]):
        _r = _safe(_i)
        _sources[_key] = (_r.get("status") if isinstance(_r, dict) else "error") or "unknown"
    log_usage_event(
        device_id=device_id,
        event_type="dashboard_query",
        plate=patente_clean,
        metadata={
            "duration_ms": _dur_ms,
            "sources": _sources,
        },
        request=request,
    )

    return {
        "plate": patente_clean,
        "prt": _safe(0),
        "boostr": _safe(1),
        "mtt": _safe(2),
        "sii": _safe(3),
        "fuel_efficiency": _safe(4),
        "auto_seguro": _safe(5),
    }


@app.post("/api/boostr/process-queue")
def boostr_process_queue():
    """Procesa la cola de cuota Boostr (Backlog Queue)."""
    try:
        import boostr_queue_worker
        return boostr_queue_worker.process_queue()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando cola Boostr: {str(e)}")


@app.get("/api/patente/{patente}")
def consultar_patente(patente: str, provider: str = "prt", marca: str = "", modelo: str = "", anio: str = "", cilindrada: str = ""):
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

                # ===== POLÍTICA DE VIGENCIA AUTÓNOMA =====
                # Registro con campos RT → aplicar la regla. Sin campos RT
                # (registro no-PRT, ej. Boostr) → servir sin exigir verificación.
                # La caja SII solo se sirve desde caché si ya tiene tasación;
                # si no, cae a la resolución por tablas oficiales.
                tiene_sii = bool(
                    resp_payload.get("tasacion_fiscal")
                    or resp_payload.get("rango_tasacion")
                )
                tiene_owner = bool(
                    resp_payload.get("owner")
                    or resp_payload.get("owner_name")
                    or resp_payload.get("owner_rut")
                    or resp_payload.get("owner_consultado")
                )
                # La caja SII solo se sirve desde caché si ya tiene tasación;
                # Boostr re-consulta con ?include=owner si la entrada antigua
                # no trae titular (invalidación en caliente de GKKS62 y afines).
                # Con marca/modelo/año explícitos en query, SII resuelve fresco.
                skip_cache = (
                    provider.lower() == "sii"
                    and (not tiene_sii or (bool(marca) and bool(modelo) and bool(anio)))
                ) or (provider.lower() == "boostr" and not tiene_owner)
                if skip_cache:
                    pass  # continuar hacia la rama del proveedor
                else:
                    tiene_rt = any(
                        resp_payload.get(k) not in (None, "", [])
                        for k in ("rt_estado", "rt_vencimiento", "historial_rt")
                    )
                    if tiene_rt:
                        vigente, motivo = _eval_prt_vigencia(resp_payload)
                        return {
                            "source": "CACHE_LOCAL",
                            "data_source": "CACHE_LOCAL",
                            "cached_at": str(row["created_at"]) if isinstance(row, dict) and "created_at" in row else "",
                            "data": resp_payload,
                            "cache_hit": bool(vigente),
                            "requiere_verificacion": not vigente,
                            "motivo_vigencia": motivo,
                        }

                    return {
                        "source": "CACHE_LOCAL",
                        "data_source": "CACHE_LOCAL",
                        "cached_at": str(row["created_at"]) if isinstance(row, dict) and "created_at" in row else "",
                        "data": resp_payload,
                        "cache_hit": True,
                        "requiere_verificacion": False,
                        "motivo_vigencia": "sin_datos_rt",
                    }
    except Exception as e:
        print(f"Error consultando caché: {e}")

    # 2. MOTOR PRT (P2P) — Si la patente NO está en caché, la consulta debe
    #    originarse desde el navegador del móvil (IP residencial), porque la IP
    #    del servidor está bloqueada por prt.cl. Se emite un 404 estructurado
    #    para que el cliente abra su resolver P2P (WebView) y luego ingeste el
    #    resultado vía POST /api/vehicle/cache.
    if provider.lower() == "prt":
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_cached",
                "message": "Patente no encontrada en caché local",
                "require_prt_solve": True,
                "requiere_verificacion": True,
                "motivo_vigencia": "no_existe",
            },
        )

    if provider.lower() == "mtt":
        # 1. Intentar microservicio con cache
        mtt_result = _fetch_mtt_from_service(patente_clean)
        # 2. Fallback al scraper local si el servicio no responde
        if mtt_result is None:
            try:
                mtt_result = _scrape_mtt(patente_clean)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Fallo consultando MTT: {str(e)}")

        mtt_data = _mtt_deep_sanitize({
            "patente": patente_clean,
            "fuente": "MTT",
            "es_transporte_publico": bool(mtt_result.get("es_transporte_publico", False)),
            "tipo_servicio": mtt_result.get("tipo_servicio", ""),
            "estado_servicio": mtt_result.get("estado_servicio", ""),
            "region": mtt_result.get("region", ""),
            "folio_flota": mtt_result.get("folio_flota", ""),
            "fecha_vencimiento_permiso": mtt_result.get("fecha_vencimiento_permiso", ""),
            "secciones": mtt_result.get("secciones") or [],
            "rt_estado": "",
            "rt_vencimiento": "",
            "historial_rt": [],
        })
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO vehicle_cache (plate, data) VALUES (%s, %s)
                ON CONFLICT (plate) DO UPDATE SET data = EXCLUDED.data, created_at = CURRENT_TIMESTAMP;
            """, (patente_clean, json.dumps(mtt_data)))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"Error guardando caché MTT: {e}")

        return {
            "source": "MTT",
            "data_source": "MTT",
            "data": mtt_data,
            "cache_hit": True,
            "requiere_verificacion": False,
        }


    # 2.5 CAJA SII (Tasación Fiscal Oficial — motor local por Decreto Exento).
    #     Resuelve marca/modelo/año del vehículo (query → ficha base interna →
    #     Boostr) y consulta la tabla sii_tasaciones (archivos oficiales
    #     liv2026.xlsx / pes2026.xlsx): exact_match con montos exactos, o
    #     rangos + lista de versiones cuando hay múltiples homologaciones.
    if provider.lower() == "sii":
        return _resolver_tasacion_sii(patente_clean, marca, modelo, anio, cilindrada)

    # 3. MOTOR BOOSTR API (Comercial — proveedor explícito ?provider=boostr)
    if not BOOSTR_API_KEY:
        raise HTTPException(status_code=500, detail="API Key de Boostr no configurada en el servidor.")

    # SIEMPRE con ?include=owner: trae titular (fullname + documentNumber).
    url = f"https://api.boostr.cl/vehicle/{patente_clean}.json?include=owner"
    headers = {"X-API-KEY": BOOSTR_API_KEY}

    try:
        response = requests.get(url, headers=headers, timeout=15)
        _update_boostr_quota_from_response(response)
        if response.status_code == 429 or "PLAN_LIMIT_EXCEEDED" in (response.text or "").upper():
            # Cuota agotada: encolar para sincronización automática y avisar.
            try:
                import boostr_queue_worker
                boostr_queue_worker.enqueue_plate(patente_clean, "QUEUED")
            except Exception as e:
                print(f"Error encolando {patente_clean}: {e}")
            raise HTTPException(
                status_code=429,
                detail="Cuota mensual de Boostr agotada. Patente agendada para sincronización automática.",
            )
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

            # ===== Payload ÍNTEGRO + enriquecimiento compartido =====
            v_inner = _boostr_enrich(v_inner)
            v_inner["data_source"] = "BOOSTR_API"

            # Guardar vehículo válido en la base de datos (payload completo,
            # campos vacíos incluidos).
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO vehicle_cache (plate, data) VALUES (%s, %s)
                    ON CONFLICT (plate) DO UPDATE SET data = EXCLUDED.data, created_at = CURRENT_TIMESTAMP;
                """, (patente_clean, json.dumps(v_inner)))
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                print(f"Error guardando en caché: {e}")

            return {
                "source": "BOOSTR_API",
                "data_source": "BOOSTR_API",
                "data": v_inner,
                "cache_hit": True,
                "requiere_verificacion": False,
                "fuente": "Boostr",
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Error consultando el servicio oficial.")
    except HTTPException:
        raise
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Fallo de conexión externa: {str(e)}")

@app.get("/api/prt/{patente}")
def consultar_prt_directo(patente: str):
    """
    Endpoint directo al microservicio PRT: devuelve la información pura de la
    revisión técnica (vehículo + inspección) sin pasar por caché ni Boostr.

    Respuesta: { "source": "PRT_SERVICE", "data": { "vehicle": {...}, "revision_tecnica": {...} } }
    """
    patente_clean = patente.strip().upper().replace("-", "").replace(" ", "")

    if not CHILEAN_PLATE_REGEX.match(patente_clean):
        raise HTTPException(
            status_code=400,
            detail="Formato de patente inválido. Verifique las normas oficiales de la PPU de Chile (Ej: ABCD12 o AB1234)."
        )

    # Lanza HTTPException (400/404/502/504) directamente.
    result = prt_client.consultar_revision_tecnica(patente_clean)

    return {
        "source": "PRT_SERVICE",
        "data": {
            "vehicle": result.get("vehicle", {}),
            "revision_tecnica": result.get("revision_tecnica", {}),
        },
    }

def _sane_text(v, max_len=120):
    """Sanitiza cualquier valor a texto latin-1 seguro para FPDF."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s[:max_len]


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
        if row and row.get("data"):
            raw = row["data"]
            if isinstance(raw, dict):
                if isinstance(raw.get("data"), dict):
                    raw = raw["data"]
                vehicle_data = dict(raw)
    except Exception as e:
        print(f"Error leyendo caché para PDF: {e}")

    if not vehicle_data and BOOSTR_API_KEY:
        try:
            url = f"https://api.boostr.cl/vehicle/{patente_clean}.json"
            headers = {"X-API-KEY": BOOSTR_API_KEY}
            resp = requests.get(url, headers=headers, timeout=10)
            _update_boostr_quota_from_response(resp)
            if resp.status_code == 200:
                bd = resp.json().get("data", {})
                if isinstance(bd, dict):
                    vehicle_data = dict(bd)
                    vehicle_data["fuente"] = "Boostr"
        except Exception:
            pass

    # Homologaciones mínimas para el PDF.
    make = _sane_text(vehicle_data.get("marca") or vehicle_data.get("make"), 60) or "DESCONOCIDO"
    model = _sane_text(vehicle_data.get("modelo") or vehicle_data.get("model"), 60) or "DESCONOCIDO"
    anio = _sane_text(vehicle_data.get("anio") or vehicle_data.get("year"), 12)
    sii = vehicle_data.get("sii") if isinstance(vehicle_data.get("sii"), dict) else {}
    historial = vehicle_data.get("historial_rt")
    if not isinstance(historial, list):
        historial = []

    campos = [
        ("PATENTE", patente_clean),
        ("MARCA", make),
        ("MODELO", model),
        ("AÑO", anio),
        ("TIPO", _sane_text(vehicle_data.get("tipo") or vehicle_data.get("body_type"), 60)),
        ("N° MOTOR", _sane_text(vehicle_data.get("nro_motor") or vehicle_data.get("engine"), 60)),
        ("CHASIS/VIN", _sane_text(vehicle_data.get("chasis") or vehicle_data.get("vin"), 60)),
        ("COLOR", _sane_text(vehicle_data.get("color"), 60)),
        ("COMBUSTIBLE", _sane_text(vehicle_data.get("combustible"), 60)),
        ("PBV", _sane_text(vehicle_data.get("pbv"), 60)),
        ("SELLO", _sane_text(vehicle_data.get("sello"), 60)),
        ("FUENTE", _sane_text(vehicle_data.get("fuente") or vehicle_data.get("data_source"), 60)),
    ]

    def row_pdf(pdf, label, valor, fill=False):
        pdf.set_font("helvetica", "B", 10)
        pdf.cell(55, 7, f" {_sane_text(label, 40)}", border=1, fill=fill)
        pdf.set_font("helvetica", "", 10)
        pdf.cell(135, 7, f" {_sane_text(valor, 110)}", border=1, ln=1)

    try:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("helvetica", "B", 18)
        pdf.cell(0, 10, "PartFinder 360 - Ficha Tecnica", ln=1, align="C")
        pdf.set_font("helvetica", "B", 12)
        pdf.set_text_color(49, 130, 206)
        pdf.cell(0, 8, f"PATENTE: {patente_clean}", ln=1)
        pdf.cell(0, 8, f"Vehiculo: {make} {model}  |  Anio: {anio}", ln=1)
        pdf.ln(4)

        # ===== Ficha técnica =====
        pdf.set_font("helvetica", "B", 11)
        pdf.set_text_color(45, 55, 72)
        pdf.cell(0, 8, "FICHA TECNICA", ln=1)
        for label, valor in campos:
            if valor:
                row_pdf(pdf, label, valor)
        pdf.ln(4)

        # ===== Tasación SII =====
        pdf.set_font("helvetica", "B", 11)
        pdf.cell(0, 8, "TASACION FISCAL SII", ln=1)
        if sii:
            row_pdf(pdf, "Codigo SII", sii.get("codigo_sii"))
            row_pdf(pdf, "Tasacion", f"{sii.get('tasacion_min')} - {sii.get('tasacion_max')}" if sii.get("tasacion_min") is not None else sii.get("tasacion_2026"))
            row_pdf(pdf, "Permiso", f"{sii.get('permiso_min')} - {sii.get('permiso_max')}" if sii.get("permiso_min") is not None else sii.get("permiso_2026"))
            row_pdf(pdf, "Traccion", sii.get("traccion"))
            row_pdf(pdf, "Transmision", sii.get("transmision"))
            row_pdf(pdf, "Combustible", sii.get("combustible"))
        else:
            pdf.set_font("helvetica", "I", 10)
            pdf.set_text_color(113, 128, 150)
            pdf.cell(0, 7, "Tasacion fiscal no disponible para este modelo", ln=1)
        pdf.ln(4)

        # ===== Revisión Técnica (segura ante historial vacío) =====
        pdf.set_font("helvetica", "B", 11)
        pdf.set_text_color(45, 55, 72)
        pdf.cell(0, 8, "REVISION TECNICA", ln=1)
        if historial:
            for h in historial:
                if not isinstance(h, dict):
                    continue
                linea = f"{h.get('fecha') or ''} | {h.get('planta') or h.get('cod_planta') or ''} | {h.get('estado') or ''} | vig: {h.get('vencimiento') or ''}"
                row_pdf(pdf, "Revision", linea)
        else:
            pdf.set_font("helvetica", "I", 10)
            pdf.set_text_color(113, 128, 150)
            pdf.cell(0, 7, "Revision Tecnica no disponible en fuente", ln=1)

        pdf.ln(10)
        pdf.set_font("helvetica", "I", 8)
        pdf.set_text_color(113, 128, 150)
        pdf.cell(0, 5, "Generado automaticamente por PartFinder 360 Chile - Reporte Oficial", align="C")

        out = pdf.output(dest='S')
        if isinstance(out, str):
            out = out.encode('latin-1')
        pdf_bytes = bytes(out)
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename=Reporte_{patente_clean}.pdf"})
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
        "remaining": -1,
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
# INGESTA DIRECTA DESDE CLIENTE (MOTOR PRT — P2P)
# ==========================================
class VehicleIngestPayload(BaseModel):
    plate: str
    data: dict


def _normalize_prt_p2p_payload(patente_clean: str, data: dict) -> dict:
    """
    Normaliza el payload P2P (extraído por el WebView del móvil desde prt.cl)
    al contrato plano interno, añade la revisión técnica y enriquece con SII.

    El payload del cliente usa claves como: patente, marca, modelo, anio,
    nro_motor, chasis, vin, historial_rt (lista de {fecha, planta,
    certificado, vencimiento, estado}), rt_vencimiento, rt_estado, fuente.
    """
    out = dict(data or {})

    # Homologación de patente.
    out["patente"] = out.get("patente") or out.get("plate") or patente_clean
    out["plate"] = out["patente"]

    # Homologación marca/modelo/año -> contrato plano (make/model/year).
    out["marca"] = out.get("marca") or out.get("make")
    out["modelo"] = out.get("modelo") or out.get("model")
    out["anio"] = out.get("anio") or out.get("year")
    out["make"] = out["marca"]
    out["model"] = out["modelo"]
    out["year"] = out["anio"]

    # Homologación VIN <-> chasis.
    vin = out.get("vin") or out.get("chasis") or out.get("nro_chasis")
    chasis = out.get("chasis") or vin
    if vin:
        out["vin"] = vin
    if chasis:
        out["chasis"] = chasis

    # Motor.
    if out.get("nro_motor"):
        out["engine"] = out["nro_motor"]
        out["engine_number"] = out["nro_motor"]

    # Revisión técnica desde el historial (normalizado).
    historial = out.get("historial_rt")
    if isinstance(historial, list) and historial:
        first = historial[0] or {}
        out["rt_estado"] = out.get("rt_estado") or first.get("estado")
        out["rt_vencimiento"] = out.get("rt_vencimiento") or first.get("vencimiento")
        out["rt_planta"] = first.get("planta")
        out["rt_certificado"] = first.get("certificado")
        out["revision_tecnica"] = {
            "estado": out["rt_estado"],
            "fechaUltimaRevision": first.get("fecha"),
            "mesVencimiento": None,
            "anioVencimiento": None,
            "plantaRevisora": out["rt_planta"],
            "codigoCertificado": out["rt_certificado"],
            "historial": historial,
        }

    # Origen de datos.
    out["data_source"] = "PRT_P2P"
    out["source"] = "PRT_P2P"
    out["fuente"] = out.get("fuente") or "PRT Oficial (P2P)"

    # Enriquecimiento con tasaciones/permisos SII (inmediato, registro 100% completo).
    enrich_with_sii(out)

    return out


@app.post("/api/vehicle/cache")
def save_scraped_vehicle(payload: VehicleIngestPayload):
    patente_clean = payload.plate.upper().replace("-", "").replace(" ", "").strip()
    if not CHILEAN_PLATE_REGEX.match(patente_clean):
        raise HTTPException(
            status_code=400,
            detail="Formato de patente inválido para ingesta P2P.",
        )

    normalized = _normalize_prt_p2p_payload(patente_clean, payload.data)

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO vehicle_cache (plate, data) VALUES (%s, %s)
            ON CONFLICT (plate) DO UPDATE SET data = EXCLUDED.data, created_at = CURRENT_TIMESTAMP;
        """, (patente_clean, json.dumps(normalized, default=str)))
        conn.commit()
        cur.close()
        conn.close()
        return {
            "status": "SUCCESS",
            "message": f"Vehículo {patente_clean} guardado en caché permanente.",
            "data_source": "PRT_P2P",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error guardando en caché: {str(e)}")


@app.delete("/api/vehicle/cache/{patente}")
def delete_cached_vehicle(patente: str):
    """Invalidación en caliente: borra la entrada de caché central para que
    la próxima consulta del proveedor (p. ej. Boostr con ?include=owner)
    vuelva a llamar a la fuente oficial."""
    patente_clean = patente.strip().upper().replace("-", "").replace(" ", "")
    if not CHILEAN_PLATE_REGEX.match(patente_clean):
        raise HTTPException(status_code=400, detail="Formato de patente inválido.")
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM vehicle_cache WHERE plate = %s;", (patente_clean,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return {"status": "SUCCESS", "deleted": deleted, "plate": patente_clean}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error invalidando caché: {str(e)}")


@app.post("/api/patente/fallback-boostr")
def fallback_boostr(payload: dict):
    plate = payload.get("patente") or payload.get("plate")
    if not plate:
        raise HTTPException(status_code=400, detail="Falta el campo patente")
    plate_clean = str(plate).strip().upper().replace("-", "").replace(" ", "")
    
    # 1. Revisar si ya está en caché
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT data, created_at FROM vehicle_cache WHERE plate = %s;", (plate_clean,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row.get("data"):
            d = row["data"].get("data") if isinstance(row["data"], dict) and "data" in row["data"] else row["data"]
            if isinstance(d, dict):
                tiene_owner = bool(
                    d.get("owner") or d.get("owner_name")
                    or d.get("owner_rut") or d.get("owner_consultado")
                )
                if tiene_owner:
                    return {"source": "CACHE_LOCAL", "data": d}
            # Sin titular → re-consultar Boostr con ?include=owner.
    except Exception as e:
        print(f"Error en cache fallback: {e}")

    # 2. Consumir Boostr API (siempre con ?include=owner)
    if not BOOSTR_API_KEY:
        raise HTTPException(status_code=500, detail="BOOSTR_API_KEY no configurada")
    
    url = f"https://api.boostr.cl/vehicle/{plate_clean}.json?include=owner"
    headers = {"X-API-KEY": BOOSTR_API_KEY}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        _update_boostr_quota_from_response(r)
        if r.status_code != 200:
            raise HTTPException(status_code=404, detail="Patente no encontrada en Boostr")
        b_res = r.json()
        v_inner = b_res.get("data", {})
        if not v_inner or not v_inner.get("make"):
            raise HTTPException(status_code=404, detail="Datos no encontrados en Boostr")
        
        final_data = v_inner.copy()
        final_data["patente"] = final_data.get("plate", plate_clean)
        final_data["marca"] = final_data.get("marca") or final_data.get("make")
        final_data["modelo"] = final_data.get("modelo") or final_data.get("model")
        final_data["anio"] = final_data.get("anio") or final_data.get("year")
        chassis_val = final_data.get("chassis") or final_data.get("chasis") or final_data.get("vin")
        if chassis_val:
            final_data["chasis"] = chassis_val
            final_data["vin"] = chassis_val
        final_data.setdefault("tipo", final_data.get("body_type") or "")
        final_data.setdefault("nro_motor", final_data.get("engine") or "")
        # Titular a nivel raíz (owner íntegro se preserva).
        owner = final_data.get("owner")
        if isinstance(owner, dict):
            final_data["owner_name"] = owner.get("fullname") or ""
            final_data["owner_rut"] = owner.get("documentNumber") or ""
        else:
            final_data.setdefault("owner_name", "")
            final_data.setdefault("owner_rut", "")
        final_data["owner_consultado"] = True
        # Homologación Boostr: RT NO disponible (campos vacíos explícitos).
        final_data["fuente"] = "Boostr"
        final_data["rt_estado"] = ""
        final_data["rt_vencimiento"] = ""
        final_data["historial_rt"] = []
        final_data["rt_disponible"] = False
        final_data["data_source"] = "BOOSTR_FALLBACK"

        enrich_with_sii(final_data)

        # Guardar en vehicle_cache
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO vehicle_cache (plate, data, created_at) VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT (plate) DO UPDATE SET data = EXCLUDED.data, created_at = NOW();
        """, (plate_clean, json.dumps(final_data)))
        conn.commit()
        cur.close()
        conn.close()

        return {"source": "BOOSTR_FALLBACK", "data": final_data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# DIAGNÓSTICO TEMPORAL — DUMP DEL DOM PRT
# ==========================================
class DebugDumpPayload(BaseModel):
    tag: str = "prt-dom"
    url: str = ""
    html: str = ""
    iframes: list = []
    inputs: list = []
    plate_input: dict = None
    prefill_done: bool = None
    injected: bool = None
    injection_error: str = None
    steps: list = []
    minimal_write: bool = None
    minimal_error: str = None

DEBUG_DUMP_PATH = os.getenv("DEBUG_DUMP_PATH", "/app/debug_prt_dump.jsonl")


@app.post("/api/debug/dump")
def debug_dump(payload: DebugDumpPayload):
    """
    Endpoint de diagnóstico temporal. Recibe un volcado del DOM que el móvil
    recolecta en la pantalla de verificación PRT y lo persiste en un archivo
    JSONL (y en el log) para inspección manual desde el servidor.

    El archivo se escribe en DEBUG_DUMP_PATH (default /app/debug_prt_dump.jsonl
    dentro del contenedor, que corresponde a
    /opt/partfinder360/partfinder/debug_prt_dump.jsonl en el host).
    """
    record = {
        "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "tag": payload.tag,
        "url": payload.url,
        "iframes": payload.iframes,
        "inputs": payload.inputs,
        "plate_input": payload.plate_input,
        "prefill_done": payload.prefill_done,
        "injected": payload.injected,
        "injection_error": payload.injection_error,
        "steps": payload.steps,
        "minimal_write": payload.minimal_write,
        "minimal_error": payload.minimal_error,
        "html_preview_len": len(payload.html or ""),
        "html": (payload.html or "")[:50000],
    }

    # 1) Persistir en JSON Lines (append, robusto ante múltiples reportes).
    try:
        with open(DEBUG_DUMP_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[DEBUG-DUMP] Error escribiendo archivo: {e}", flush=True)

    # 2) Snapshot JSON único (último dump) para inspección rápida con cat/jq.
    try:
        snapshot_path = DEBUG_DUMP_PATH.rsplit(".", 1)[0] + ".json"
        with open(snapshot_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[DEBUG-DUMP] Error escribiendo snapshot: {e}", flush=True)

    # 2) Volcar metadatos al log por si el archivo no es accesible.
    print(f"[DEBUG-DUMP] tag={payload.tag} url={payload.url} "
          f"iframes={len(payload.iframes or [])} inputs={len(payload.inputs or [])} "
          f"html_len={len(payload.html or '')}", flush=True)

    return {
        "status": "OK",
        "tag": payload.tag,
        "iframes_count": len(payload.iframes or []),
        "inputs_count": len(payload.inputs or []),
        "html_len": len(payload.html or ""),
    }


# ==========================================
# INYECCIÓN DINÁMICA + CONSOLA REMOTA
# ==========================================
PRT_INJECTION_PATH = os.getenv("PRT_INJECTION_PATH", "/debug_host/prt_injection.js")

from fastapi.responses import PlainTextResponse  # noqa: E402


@app.get("/api/debug/prt-script.js")
def prt_dynamic_script():
    """
    Sirve el script de inyección dinámica directamente desde el archivo local
    (host /opt/partfinder360/prt_injection.js, montado en el contenedor como
    /debug_host/prt_injection.js). Así cualquier cambio en ese archivo se
    aplica al instante en el móvil sin recompilar el APK.
    """
    try:
        with open(PRT_INJECTION_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        return PlainTextResponse(
            content,
            media_type="application/javascript",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Script de inyección no encontrado")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo script: {str(e)}")


AUTO_SEGURO_INJECTION_PATH = os.getenv("AUTO_SEGURO_INJECTION_PATH", "/debug_host/auto_seguro_injection.js")


@app.get("/api/debug/auto-seguro-script.js")
def auto_seguro_dynamic_script():
    """
    Sirve el script de inyección dinámica del WebView de Auto Seguro
    (host /opt/partfinder360/auto_seguro_injection.js → contenedor
    /debug_host/auto_seguro_injection.js). Hot-served: sin recompilar APK.
    """
    try:
        with open(AUTO_SEGURO_INJECTION_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        return PlainTextResponse(
            content,
            media_type="application/javascript",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Script de inyección Auto Seguro no encontrado")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo script Auto Seguro: {str(e)}")


class DebugLogPayload(BaseModel):
    message: str = ""


@app.post("/api/debug/log")
def debug_log(payload: DebugLogPayload):
    """
    Consola remota en vivo: recibe mensajes del WebView (console.log/error,
    PrtBridge) y los imprime en tiempo real para seguir con `docker logs -f`.
    """
    ts = __import__("datetime").datetime.utcnow().isoformat() + "Z"
    print(f"[PRT-CONSOLE {ts}] {payload.message}", flush=True)
    return {"status": "OK"}
