import sys
import zipfile
import xml.etree.ElementTree as ET
import psycopg2
from psycopg2.extras import execute_values
from main import DB_HOST, DB_NAME, DB_USER, DB_PASSWORD

def get_db():
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )

def init_table():
    conn = get_db()
    cur = conn.cursor()
    print(">>> Creando tabla sii_tasaciones e indices...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sii_tasaciones (
            id SERIAL PRIMARY KEY,
            origen VARCHAR(20),
            codigo_sii VARCHAR(30),
            anio INTEGER,
            tipo VARCHAR(100),
            marca VARCHAR(100),
            modelo VARCHAR(150),
            version VARCHAR(255),
            puertas INTEGER,
            cilindrada INTEGER,
            potencia INTEGER,
            combustible VARCHAR(50),
            transmision VARCHAR(50),
            marchas VARCHAR(20),
            traccion VARCHAR(50),
            pais VARCHAR(100),
            equipamiento TEXT,
            tasacion_2026 BIGINT,
            permiso_2026 BIGINT,
            observacion TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_sii_search ON sii_tasaciones (marca, modelo, anio);
        CREATE INDEX IF NOT EXISTS idx_sii_codigo ON sii_tasaciones (codigo_sii);
        CREATE INDEX IF NOT EXISTS idx_sii_anio ON sii_tasaciones (anio);
    """)
    conn.commit()
    cur.close()
    conn.close()
    print(">>> Tabla e indices listos.")

def safe_int(val):
    if not val:
        return None
    try:
        clean = "".join([c for c in str(val) if c.isdigit()])
        return int(clean) if clean else None
    except:
        return None

def parse_and_insert(filename, origen, header_idx):
    print(f"\n==========================================")
    print(f">>> Procesando {filename} ({origen})...")
    
    with zipfile.ZipFile(filename, 'r') as z:
        shared_strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            tree = ET.fromstring(z.read('xl/sharedStrings.xml'))
            for si in tree.findall('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si'):
                t = si.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t')
                if t is not None and t.text:
                    shared_strings.append(t.text)
                else:
                    shared_strings.append("".join([elem.text for elem in si.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t') if elem.text]))

        sheet_data = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        rows = sheet_data.findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row')
        total_rows = len(rows)
        print(f">>> Filas totales en archivo: {total_rows}")

        records = []
        conn = get_db()
        cur = conn.cursor()
        batch_size = 5000

        for r_idx in range(header_idx, total_rows):
            row = rows[r_idx]
            vals = {}
            for c in row.findall('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c'):
                ref = c.attrib.get('r', '')
                # Obtener la columna alfabética (A, B, C...)
                col_letter = "".join([ch for ch in ref if ch.isalpha()])
                v = c.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v')
                val = v.text if v is not None else ""
                if c.attrib.get('t') == 's' and val.isdigit():
                    val = shared_strings[int(val)]
                vals[col_letter] = val.strip()

            # Mapeo según el tipo de archivo
            if origen == 'LIVIANO':
                # A=Cod, B=Año, C=Tipo, D=Marca, E=Modelo, F=Versión, G=Puertas, H=CC, I=HP, J=Comb, K=Trans, L=Marchas, M=Traccion, N=Pais, P=Equip, Q=Tasacion, R=Permiso, S=Obs
                cod = vals.get('A', '')
                anio = safe_int(vals.get('B', ''))
                if not cod or not anio:
                    continue
                records.append((
                    origen,
                    cod,
                    anio,
                    vals.get('C', ''),
                    vals.get('D', '').upper(),
                    vals.get('E', '').upper(),
                    vals.get('F', '').upper(),
                    safe_int(vals.get('G', '')),
                    safe_int(vals.get('H', '')),
                    safe_int(vals.get('I', '')),
                    vals.get('J', ''),
                    vals.get('K', ''),
                    vals.get('L', ''),
                    vals.get('M', ''),
                    vals.get('N', ''),
                    vals.get('P', '') or vals.get('O', ''),
                    safe_int(vals.get('Q', '')),
                    safe_int(vals.get('R', '')),
                    vals.get('S', '')
                ))
            else:
                # PESADO: A=Cod, B=Año, C=Tipo, D=Marca, E=Modelo, F=Versión, G=CC, H=Carga, I=Pasajeros, J=Trans, K=Traccion, L=Pais, M=Tasacion
                cod = vals.get('A', '')
                anio = safe_int(vals.get('B', ''))
                if not cod or not anio:
                    continue
                records.append((
                    origen,
                    cod,
                    anio,
                    vals.get('C', ''),
                    vals.get('D', '').upper(),
                    vals.get('E', '').upper(),
                    vals.get('F', '').upper(),
                    None, # puertas
                    safe_int(vals.get('G', '')),
                    None, # hp
                    None, # comb
                    vals.get('J', ''),
                    None, # marchas
                    vals.get('K', ''),
                    vals.get('L', ''),
                    f"Carga: {vals.get('H','')} KG | Pasajeros: {vals.get('I','')}",
                    safe_int(vals.get('M', '')),
                    None, # permiso
                    None  # obs
                ))

            if len(records) >= batch_size:
                execute_values(cur, """
                    INSERT INTO sii_tasaciones (
                        origen, codigo_sii, anio, tipo, marca, modelo, version,
                        puertas, cilindrada, potencia, combustible, transmision,
                        marchas, traccion, pais, equipamiento, tasacion_2026,
                        permiso_2026, observacion
                    ) VALUES %s
                """, records)
                conn.commit()
                print(f"  -> {r_idx}/{total_rows} filas guardadas...")
                records = []

        if records:
            execute_values(cur, """
                INSERT INTO sii_tasaciones (
                    origen, codigo_sii, anio, tipo, marca, modelo, version,
                    puertas, cilindrada, potencia, combustible, transmision,
                    marchas, traccion, pais, equipamiento, tasacion_2026,
                    permiso_2026, observacion
                ) VALUES %s
            """, records)
            conn.commit()

        cur.close()
        conn.close()
        print(f">>> {filename} importado con exito.")

if __name__ == '__main__':
    init_table()
    parse_and_insert('/opt/partfinder360/data/sii/liv2026.xlsx', 'LIVIANO', 12)
    parse_and_insert('/opt/partfinder360/data/sii/pes2026.xlsx', 'PESADO', 10)
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), origen FROM sii_tasaciones GROUP BY origen;")
    print("\n=== RESUMEN FINAL EN POSTGRESQL ===")
    for row in cur.fetchall():
        print(f"Total {row[1]}: {row[0]:,} registros")
    cur.close()
    conn.close()
