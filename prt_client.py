"""
Cliente HTTP dedicado para el microservicio PRT (estado vehicular y de
revisión técnica en Chile).

Expone una única función de alto nivel, `consultar_revision_tecnica(patente)`,
que consume `GET {PRT_SERVICE_URL}/api/v1/prt/{patente}` y devuelve:

    {
        "vehicle": {...},          # contrato plano interno (marca, modelo, ...)
        "revision_tecnica": {...}, # bloque de inspección
        "raw": {...}               # payload crudo del microservicio (opcional)
    }

Mapea defensivamente los errores del microservicio (400/404/425/502/504) a
HTTPException de FastAPI, y cualquier fallo de red/timeout a un 502 o 504
controlado, de modo que la capa superior pueda decidir el fallback a Boostr.
"""

import os
import requests

PRT_SERVICE_URL = os.getenv("PRT_SERVICE_URL", "http://host.docker.internal:3090")
PRT_TIMEOUT = float(os.getenv("PRT_TIMEOUT", "5"))


def _map_prt_error(status_code: int, payload: dict):
    """
    Traduce un código de estado del microservicio PRT a HTTPException.
    Devuelve la excepción lista para ser lanzada.
    """
    from fastapi import HTTPException

    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    message = error.get("message") or (payload.get("detail") if isinstance(payload, dict) else None)

    # Mapeo defensivo de los códigos documentados del microservicio.
    status_map = {
        400: "Formato de patente no válido.",
        404: "Patente no encontrada en el sistema PRT.",
        425: None,   # bloqueo CAPTCHA/Cloudflare -> se convierte en 502 aguas abajo
        502: None,   # PRT caído -> 502
        504: None,   # timeout -> 504
    }

    if status_code == 400:
        return HTTPException(status_code=400, detail=message or status_map[400])
    if status_code == 404:
        return HTTPException(status_code=404, detail=message or status_map[404])
    if status_code == 425:
        return HTTPException(status_code=502, detail=message or "PRT bloqueó la consulta (CAPTCHA/Cloudflare).")
    if status_code == 502:
        return HTTPException(status_code=502, detail=message or "PRT no está disponible en este momento.")
    if status_code == 504:
        return HTTPException(status_code=504, detail=message or "PRT no respondió a tiempo.")

    # Cualquier otro error HTTP.
    return HTTPException(status_code=502, detail=message or f"PRT respondió con HTTP {status_code}.")


def consultar_revision_tecnica(patente: str, timeout: float = None):
    """
    Consulta el estado vehicular y de revisión técnica de una patente chilena
    contra el microservicio PRT.

    :param patente: patente ya normalizada (mayúsculas, sin guiones/espacios).
    :param timeout: timeout opcional de red (default PRT_TIMEOUT).
    :returns: dict con { vehicle, revision_tecnica, raw } mapeado al contrato plano.
    :raises HTTPException: 400/404/425->502/502/504 según el origen.
    """
    from fastapi import HTTPException

    url = f"{PRT_SERVICE_URL.rstrip('/')}/api/v1/prt/{patente}"
    req_timeout = timeout if timeout is not None else PRT_TIMEOUT

    try:
        response = requests.get(url, timeout=req_timeout)
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="PRT no respondió a tiempo.")
    except requests.exceptions.ConnectionError as e:
        raise HTTPException(status_code=502, detail=f"No se pudo conectar al servicio PRT: {str(e)}")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Fallo de conexión con el servicio PRT: {str(e)}")

    # El microservicio devuelve errores con código HTTP y cuerpo JSON.
    if response.status_code != 200:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        raise _map_prt_error(response.status_code, payload)

    payload = response.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Respuesta inválida del servicio PRT.")

    data = payload.get("data", {}) or {}
    vehicle = data.get("vehicle", {}) or {}
    revision = data.get("revisionTecnica", {}) or {}

    # Mapeo del contrato del microservicio -> contrato plano interno.
    # Homologación VIN <-> chasis (el microservicio usa "vin").
    vin = vehicle.get("vin")
    chasis = vin

    vehicle_flat = {
        "patente": vehicle.get("patente") or patente,
        "marca": vehicle.get("marca"),
        "modelo": vehicle.get("modelo"),
        "anio": vehicle.get("anio"),
        "make": vehicle.get("marca"),
        "model": vehicle.get("modelo"),
        "year": vehicle.get("anio"),
        "vin": vin,
        "chasis": chasis,
        "tipoVehiculo": vehicle.get("tipoVehiculo"),
        "tipo_vehiculo": vehicle.get("tipoVehiculo"),
        "type": vehicle.get("tipoVehiculo"),
        # homologación para el enriquecedor SII (usa marca/modelo/anio o make/model/year)
    }

    revision_tecnica = {
        "estado": revision.get("estado"),
        "fechaUltimaRevision": revision.get("fechaUltimaRevision"),
        "mesVencimiento": revision.get("mesVencimiento"),
        "anioVencimiento": revision.get("anioVencimiento"),
        "plantaRevisora": revision.get("plantaRevisora"),
        "codigoCertificado": revision.get("codigoCertificado"),
        "historial": revision.get("historial"),
    }

    return {
        "vehicle": vehicle_flat,
        "revision_tecnica": revision_tecnica,
        "raw": payload,
    }
