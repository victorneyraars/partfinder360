# Deploy - PartFinder 360

Configuracion del stack Docker para el backend PartFinder 360.

## Estructura del servidor

    /opt/
      partfinder360/
        .env                    # variables reales (NO en git, en .gitignore)
        database/               # volumen Postgres
        docker-compose.yml      # symlink -> partfinder/deploy/docker-compose.yml
        partfinder/             # este repo (backend FastAPI)
          deploy/               # esta carpeta
            docker-compose.yml
            .env.example
            README.md
      servicios/
        mtt-service/            # repo victorneyraars/mtt-service
          data/mtt_cache.db     # cache SQLite (volumen)
        boostr-service/         # repo victorneyraars/boostr-service
          data/boostr_cache.db  # cache SQLite (volumen)
        prt-service/            # Node.js en host, puerto 3090 (systemd)

## Red

Todos los contenedores viven en `pf_network` (bridge). DNS interno por
nombre de servicio: `database`, `partfinder-api`, `mtt-service`,
`boostr-service`.

El backend `pf_api` los alcanza via:
- `http://mtt-service:3091`
- `http://boostr-service:3092`
- `http://host.docker.internal:3090` (prt-service, en el host)

## Despliegue inicial

    # 1. Clonar repos
    cd /opt
    git clone git@github.com:victorneyraars/partfinder360.git
    cd partfinder360/partfinder
    git clone git@github.com:victorneyraars/mtt-service.git /opt/servicios/mtt-service
    git clone git@github.com:victorneyraars/boostr-service.git /opt/servicios/boostr-service

    # 2. Configurar env
    cp /opt/partfinder360/partfinder/deploy/.env.example /opt/partfinder360/.env
    # editar /opt/partfinder360/.env con valores reales

    # 3. Symlink del compose
    ln -sf /opt/partfinder360/partfinder/deploy/docker-compose.yml \
           /opt/partfinder360/docker-compose.yml

    # 4. Build imagenes de microservicios
    cd /opt/servicios/mtt-service && docker build -t mtt-service:1.0.0 .
    cd /opt/servicios/boostr-service && docker build -t boostr-service:1.0.0 .

    # 5. Levantar stack
    cd /opt/partfinder360 && docker compose up -d

## Operacion diaria

    # Ver estado
    cd /opt/partfinder360 && docker compose ps

    # Restart de un servicio tras cambio de codigo
    docker restart pf_api

    # Reconstruir un microservicio tras cambio de codigo
    cd /opt/servicios/mtt-service && docker build -t mtt-service:1.0.0 . && \
      cd /opt/partfinder360 && docker compose up -d mtt-service

    # Logs
    docker logs -f pf_api
    docker logs -f pf_boostr_service

## Notas

- El compose dentro del repo usa **paths absolutos** (`/opt/partfinder360/...`)
  para que funcione tanto ejecutado desde `/opt/partfinder360/` como desde
  esta carpeta.
- `env_file` apunta a `/opt/partfinder360/.env` (fuera del repo).
- Los volumenes de los microservicios quedan en sus respectivos repos
  (`/opt/servicios/*/data/`).
