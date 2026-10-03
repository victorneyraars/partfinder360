#!/usr/bin/env python3
"""
PartFinder360 — Seed oficial SII (Tasación Fiscal por Decreto Exento).

Procesa los dos archivos oficiales publicados por el SII:
  - data/liv2026.xlsx  → Tasación de Vehículos Livianos (incluye motos)
  - data/pes2026.xlsx  → Tasación de Vehículos de Pasajeros y Carga Ajena

Normaliza TODAS las cadenas (sin tildes, mayúsculas, trim, colapso de
espacios) y carga la tabla `sii_tasaciones` (categoria: LIVIANO / MOTO /
PESADO; permiso_circulacion = 0 cuando el archivo no lo publica, como en
pesados). Idempotente: limpia el año tributario antes de insertar.

Sin dependencias externas: parser XLSX propio (zipfile + ElementTree +
sharedStrings).
"""
import os
import re
import sys
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

import psycopg2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIV_PATH = os.path.join(BASE_DIR, "data", "liv2026.xlsx")
PES_PATH = os.path.join(BASE_DIR, "data", "pes2026.xlsx")
ANIO_TRIBUTARIO = 2026

DB_HOST = os.getenv("DB_HOST", "pf_database")
DB_NAME = os.getenv("DB_NAME", "partfinder")
DB_USER = os.getenv("DB_USER", "pf_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secure_db_password_change_me")

M_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def normalize(value):
    """Sin tildes, mayúsculas, trim y colapso de espacios múltiples."""
    if value is None:
        return ""
    s = str(value).strip()
    s = re.sub(r"\s+", " ", s)
    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )
    return s.upper()


def to_int(value, default=None):
    s = str(value or "").replace(".", "").replace(",", "").strip()
    try:
        return int(float(s)) if s else default
    except ValueError:
        return default


def load_shared_strings(zf):
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out = []
    for si in root.findall(f"{M_NS}si"):
        text = "".join(t.text or "" for t in si.iter(f"{M_NS}t"))
        out.append(text)
    return out


def read_sheet_rows(zf, sheet_name):
    """Devuelve [(row_cells dict col->value)] para la hoja indicada."""
    strings = load_shared_strings(zf)
    root = ET.fromstring(zf.read(sheet_name))
    rows = []
    for row in root.findall(f"{M_NS}sheetData/{M_NS}row"):
        cells = {}
        for c in row.findall(f"{M_NS}c"):
            ref = c.get("r") or ""
            col = re.match(r"[A-Z]+", ref)
            if not col:
                continue
            col = col.group(0)
            t = c.get("t")
            v = c.find(f"{M_NS}v")
            val = v.text if v is not None else ""
            if t == "s" and val != "":
                val = strings[int(val)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{M_NS}t"))
            cells[col] = val
        rows.append(cells)
    return rows


def iter_livianos(zf):
    """Columnas liv2026: A codigo, B anio, C tipo, D marca, E modelo,
    F version, G puertas, H cilindrada, I potencia, J combustible,
    K transmision, L marchas, M traccion, N pais, O equip_antiguo,
    P equipamiento, Q tasacion, R permiso, S observacion."""
    for row in read_sheet_rows(zf, "xl/worksheets/sheet1.xml"):
        codigo = normalize(row.get("A"))
        if not re.match(r"^[A-Z]{2}\d+", codigo):
            continue  # salta título/cabecera/filas vacías
        tipo = normalize(row.get("C"))
        yield {
            "codigo_sii": codigo[:16],
            "categoria": "MOTO" if ("MOTO" in tipo or "CUATRIMOTO" in tipo) else "LIVIANO",
            "tipo_vehiculo": tipo[:64],
            "marca": normalize(row.get("D"))[:64],
            "modelo": normalize(row.get("E"))[:96],
            "version": normalize(row.get("F"))[:128] or "SIN VERSION",
            "anio_fabricacion": to_int(row.get("B"), 0),
            "tasacion_fiscal": to_int(row.get("Q"), 0),
            "permiso_circulacion": to_int(row.get("R"), 0),
            "anio_tributario": ANIO_TRIBUTARIO,
            "cilindrada": to_int(row.get("H")),
            "combustible": normalize(row.get("J"))[:50],
            "transmision": normalize(row.get("K"))[:50],
            "puertas": to_int(row.get("G")),
            "equipamiento": normalize(row.get("P"))[:4000],
        }


def iter_pesados(zf):
    """Columnas pes2026: A codigo, B anio, C tipo, D marca, E modelo,
    F version, G cilindrada, H carga, I pasajeros, J transmision,
    K traccion, L pais, M tasacion (sin columna de permiso)."""
    for row in read_sheet_rows(zf, "xl/worksheets/sheet1.xml"):
        codigo = normalize(row.get("A"))
        if not re.match(r"^[A-Z]{2}\d+", codigo):
            continue
        yield {
            "codigo_sii": codigo[:16],
            "categoria": "PESADO",
            "tipo_vehiculo": normalize(row.get("C"))[:64],
            "marca": normalize(row.get("D"))[:64],
            "modelo": normalize(row.get("E"))[:96],
            "version": normalize(row.get("F"))[:128] or "SIN VERSION",
            "anio_fabricacion": to_int(row.get("B"), 0),
            "tasacion_fiscal": to_int(row.get("M"), 0),
            "permiso_circulacion": 0,  # el archivo pesados no publica permiso
            "anio_tributario": ANIO_TRIBUTARIO,
            "cilindrada": to_int(row.get("G")),
            "combustible": "",
            "transmision": normalize(row.get("J"))[:50],
            "puertas": None,
            "equipamiento": "",
        }


def main():
    if not os.path.exists(LIV_PATH):
        print(f"Falta {LIV_PATH}", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(PES_PATH):
        print(f"Falta {PES_PATH}", file=sys.stderr)
        sys.exit(1)

    rows = []
    with zipfile.ZipFile(LIV_PATH) as zf:
        rows.extend(iter_livianos(zf))
    print(f"livianos (incl. motos): {len(rows)} filas")
    n_liv = len(rows)
    with zipfile.ZipFile(PES_PATH) as zf:
        rows.extend(iter_pesados(zf))
    print(f"pesados: {len(rows) - n_liv} filas | total: {len(rows)}")

    conn = psycopg2.connect(
        host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM sii_tasaciones WHERE anio_tributario = %s",
                    (ANIO_TRIBUTARIO,),
                )
                cur.executemany(
                    """
                    INSERT INTO sii_tasaciones (
                        codigo_sii, categoria, tipo_vehiculo, marca, modelo,
                        version, anio_fabricacion, tasacion_fiscal,
                        permiso_circulacion, anio_tributario, cilindrada,
                        combustible, transmision, puertas, equipamiento
                    ) VALUES (
                        %(codigo_sii)s, %(categoria)s, %(tipo_vehiculo)s,
                        %(marca)s, %(modelo)s, %(version)s,
                        %(anio_fabricacion)s, %(tasacion_fiscal)s,
                        %(permiso_circulacion)s, %(anio_tributario)s,
                        %(cilindrada)s, %(combustible)s, %(transmision)s,
                        %(puertas)s, %(equipamiento)s
                    )
                    """,
                    rows,
                )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT categoria, count(*) FROM sii_tasaciones "
                    "WHERE anio_tributario = %s GROUP BY categoria ORDER BY 2 DESC",
                    (ANIO_TRIBUTARIO,),
                )
                print("distribución por categoría:")
                for categoria, total in cur.fetchall():
                    print(f"  {categoria}: {total}")
    finally:
        conn.close()
    print("Seed SII completado OK.")


if __name__ == "__main__":
    main()
