# PartFinder 360 — API Backend

API de consulta de vehículos por patente chilena, con enriquecimiento de
tasaciones SII y, desde la integración con el **microservicio PRT**, el estado
de la revisión técnica.

- **Stack**: Python 3.11 · FastAPI · Uvicorn · PostgreSQL 15 · `requests`.
- **Puerto**: `8000` (publicado en el host).
- **Orquestación**: `docker-compose.yml` (servicios `database` y `partfinder-api`).

---

## Flujo de resolución de patentes

La consulta principal (`GET /api/patente/{patente}`) sigue una **cascada
inteligente**, con un flujo P2P para patentes no cacheadas (porque la IP del
servidor está bloqueada por `prt.cl`):

```
1. Caché local (PostgreSQL, tabla vehicle_cache)
        │  ── si existe registro válido → responde CACHE_LOCAL (+ enriquecimiento SII)
        ▼
2. provider = "prt"  (por defecto)
        │  ── si NO está cacheada → 404 estructurado { require_prt_solve: true }
        │     → el cliente (móvil) abre su resolver P2P (WebView) y consulta
        │       prt.cl desde su IP residencial, luego hace POST /api/vehicle/cache
        ▼
3. provider = "boostr"  (explícito)
        │  ── consulta directa a Boostr; si 200 → BOOSTR_API y guarda en caché
        ▼
   (404 si ninguna fuente tiene registros)
```

### Selección de proveedor

| `?provider=` | Comportamiento |
|--------------|----------------|
| `prt` (default) | Caché → si no está cacheada, emite `require_prt_solve: true` para resolución P2P en el dispositivo. |
| `boostr` | Salta PRT y consulta **directamente** a Boostr (respeta la petición explícita). |

### Contrato 404 estructurado (patente no cacheada)

Cuando `provider=prt` y la patente no existe en `vehicle_cache`, el backend
responde **`404`** con un cuerpo estructurado para que el cliente active la
resolución P2P:

```json
{
  "detail": {
    "error": "not_cached",
    "message": "Patente no encontrada en caché local",
    "require_prt_solve": true
  }
}
```

> **Nota**: el microservicio PRT (`/api/prt/{patente}`) sigue disponible como
> vía directa/servidor; el flujo P2P es el camino principal para patentes no
> cacheadas. Ver el README del microservicio en `/opt/servicios/prt-service`.

---

## Endpoints principales

### `GET /api/patente/{patente}`

Consulta principal (cascada). Parámetro opcional `?provider=prt|boostr`.

```bash
curl "http://localhost:8000/api/patente/ABCD12"
```

Respuesta (resumen, campos representativos):

```json
{
  "source": "PRT_SERVICE",
  "data_source": "PRT_SERVICE",
  "data": {
    "patente": "ABCD12",
    "marca": "TOYOTA",
    "modelo": "HILUX 2.4",
    "anio": "2020",
    "make": "TOYOTA",
    "model": "HILUX 2.4",
    "year": "2020",
    "vin": "MR0KB8CD000000001",
    "chasis": "MR0KB8CD000000001",
    "tipoVehiculo": "CAMIÓN / CAMIONETA",
    "status": "VIGENTE",
    "revision_tecnica": {
      "estado": "VIGENTE",
      "fechaUltimaRevision": "2024-11-08",
      "mesVencimiento": "Noviembre",
      "anioVencimiento": "2025",
      "plantaRevisora": "PLANTA VALPARAÍSO",
      "codigoCertificado": "RT-ABCD12-2024",
      "historial": []
    },
    "sii": {
      "codigo_sii": "CT2350166",
      "tasacion_2026": 8546214,
      "permiso_2026": 130835
    }
  }
}
```

- `source` / `data_source` indican la fuente que resolvió: `CACHE_LOCAL`,
  `PRT_SERVICE` o `BOOSTR_API`.
- El bloque `revision_tecnica` solo aparece cuando la fuente es PRT.
- El bloque `sii` es el enriquecimiento de tasaciones/permisos (si aplica).

### `GET /api/prt/{patente}`

Endpoint **directo** al microservicio PRT (sin caché ni Boostr). Devuelve la
información pura de vehículo + revisión técnica.

```bash
curl "http://localhost:8000/api/prt/BBCL10"
```

```json
{
  "source": "PRT_SERVICE",
  "data": {
    "vehicle": {
      "patente": "BBCL10",
      "marca": "SEAT",
      "modelo": "LEÓN 1.4 TSI",
      "anio": "2019",
      "make": "SEAT",
      "model": "LEÓN 1.4 TSI",
      "year": "2019",
      "vin": "VSSZZZ5FZKR123456",
      "chasis": "VSSZZZ5FZKR123456",
      "tipoVehiculo": "AUTOMÓVIL"
    },
    "revision_tecnica": {
      "estado": "APROBADA",
      "fechaUltimaRevision": "2024-03-15",
      "mesVencimiento": "Marzo",
      "anioVencimiento": "2025",
      "plantaRevisora": "REVISIONES METROPOLITANA",
      "codigoCertificado": "RT-BBCL10-2024",
      "historial": []
    }
  }
}
```

### Otros endpoints

| Endpoint | Descripción |
|----------|-------------|
| `GET /api/tasacion` | Consulta de tasaciones SII (por `codigo_sii` o `marca`+`modelo`+`anio`). |
| `GET /api/patente/{patente}/pdf` | Ficha técnica en PDF. |
| `GET /api/boostr/status` | Telemetría del plan Boostr. |
| `POST /api/patente/cache`, `POST /api/vehicle/cache`, `POST /api/patente/fallback-boostr` | Gestión de caché y fallback. |
| `GET /api/r/meli` | Redirección de afiliado MercadoLibre. |

### Ingesta P2P — `POST /api/vehicle/cache`

Tras resolver la patente en el dispositivo (WebView contra `prt.cl`), el móvil
persiste el resultado en el backend mediante este endpoint. El backend
**normaliza el payload**, marca el origen como `PRT_P2P` y ejecuta el
**enriquecimiento SII automático** para dejar el registro completo.

```bash
curl -X POST "http://localhost:8000/api/vehicle/cache" \
  -H "Content-Type: application/json" \
  -d '{
    "plate": "GGHH88",
    "data": {
      "patente": "GGHH88",
      "tipo": "AUTOMOVIL",
      "marca": "KIA",
      "modelo": "RIO",
      "anio": "2021",
      "nro_motor": "G4FG-987654",
      "chasis": "KNADM411AM6123456",
      "historial_rt": [
        {"fecha":"2024-08-01","cod_planta":"PRT-014","planta":"REVISIONES METROPOLITANA","certificado":"RT-GGHH88-2024","vencimiento":"2025-07-31","estado":"APROBADA"}
      ],
      "rt_vencimiento": "2025-07-31",
      "rt_estado": "APROBADA",
      "fuente": "PRT Oficial"
    }
  }'
```

Respuesta:

```json
{ "status": "SUCCESS", "message": "Vehículo GGHH88 guardado en caché permanente.", "data_source": "PRT_P2P" }
```

- El payload se normaliza: homologa `marca`→`make`, `chasis`↔`vin`,
  `nro_motor`→`engine`, y extrae de `historial_rt` el bloque `revision_tecnica`.
- El registro se guarda en `vehicle_cache` con `data_source = "PRT_P2P"`.
- `enrich_with_sii()` se ejecuta **de inmediato**, añadiendo el bloque `sii`
  (tasaciones y permisos) al dato persistido.

---

## Variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `BOOSTR_API_KEY` | — | API key del proveedor comercial Boostr. |
| `PRT_SERVICE_URL` | `http://host.docker.internal:3090` | URL base del microservicio PRT. |
| `PRT_TIMEOUT` | `5` | Timeout (segundos) de las consultas al microservicio PRT. |
| `DB_HOST` | `pf_database` | Host de PostgreSQL. |
| `DB_NAME` | `partfinder` | Nombre de la base de datos. |
| `DB_USER` | `pf_user` | Usuario de PostgreSQL. |
| `DB_PASSWORD` | `secure_db_password_change_me` | Contraseña de PostgreSQL. |

Estas variables se definen en el archivo `.env` (raíz del proyecto) y se
inyectan al contenedor `partfinder-api` vía `env_file` / `environment`.

---

## Arquitectura de conectividad (Docker → Host)

La API corre dentro de un contenedor Docker (`pf_api`) y el microservicio PRT
corre **en el host nativo** gestionado por **systemd** (puerto `3090`). Para
que el contenedor alcance el host se usa:

1. **`extra_hosts`** en `docker-compose.yml`:
   ```yaml
   extra_hosts:
     - "host.docker.internal:172.18.0.1"
   ```
   `172.18.0.1` es el gateway del bridge de red `partfinder360_pf_network`
   (la interfaz `br-<id>` del host), que es la vía correcta hacia el host.

2. **Binding del microservicio**: el servicio PRT escucha en `0.0.0.0:3090`
   (no solo `127.0.0.1`) para ser alcanzable desde el bridge Docker.

3. **Firewall (UFW)**: se habilitó el puerto `3090/tcp` en entrada y reenvío:
   ```bash
   ufw allow 3090/tcp
   ufw route allow 3090/tcp
   ```

```
[ pf_api (Docker) ] --host.docker.internal:3090--> [ host: PRT systemd service ]
       172.18.0.2                                       0.0.0.0:3090 (172.18.0.1)
```

> **Importante**: si Docker recrea la red con otro subnet, el valor del gateway
> (`172.18.0.1`) en `extra_hosts` debe actualizarse — se puede obtener con
> `docker inspect pf_api` (campo `NetworkSettings.Networks.*.Gateway`).

---

## Verificación rápida

```bash
# Salud del microservicio PRT (desde el host)
curl http://127.0.0.1:3090/health

# Endpoint directo PRT (desde el host)
curl http://localhost:8000/api/prt/BBCL10

# Cascada completa
curl "http://localhost:8000/api/patente/ABCD12"
```
