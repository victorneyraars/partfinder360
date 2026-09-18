import os
import requests
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

app = FastAPI(
    title="PartFinder 360 API",
    description="API centralizada de consultas de patentes en Chile usando Boostr",
    version="1.0.0"
)

class PatenteQuery(BaseModel):
    patente: str

@app.get("/")
def read_root():
    return {"status": "online", "system": "PartFinder 360 API", "target": "Chile Vehicle Lookup"}

@app.post("/api/patente")
def consultar_patente(query: PatenteQuery, x_boostr_key: str = Header(None)):
    patente_limpia = query.patente.upper().strip()
    
    # Si no pasan la llave por cabecera, podemos buscarla en una variable de entorno o exigir la cabecera
    api_key = x_boostr_key or os.getenv("BOOSTR_API_KEY")
    
    if not api_key:
        raise HTTPException(
            status_code=400, 
            detail="Falta la API Key de Boostr. Proporciónala en la cabecera X-Boostr-Key o configúrala en el servidor."
        )
    
    url = f"https://api.boostr.cl/vehicle/{patente_limpia}.json"
    headers = {
        "X-API-KEY": api_key,
        "accept": "application/json"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code, 
                detail=data.get("message", "Error al consultar la patente en Boostr")
            )
            
        return {
            "status": "success",
            "patente": patente_limpia,
            "resultado": data
        }
        
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error de conexión con el proveedor externo: {str(e)}")
