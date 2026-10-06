# Sistema de turnos multi-negocio (MVP) — SinPelosenlaLengua

Python 3.9+ (sin dependencias) + SQLite. Cada tabla tiene `business_id` (preparado para multi-negocio; `?b=slug` selecciona negocio).

## Ejecutar local
    ADMIN_PASSWORD=tuclave python3 server.py
    # público: http://localhost:8000   admin: http://localhost:8000/admin
    python3 tests.py   # pruebas de flujos críticos

## Variables de entorno
- `ADMIN_PASSWORD` (obligatoria en producción), `PORT` (8000), `DB_PATH` (ruta del archivo SQLite; en producción debe estar en disco persistente).

## Desplegar
1. Subir a GitHub: `git init && git add . && git commit -m "MVP" && git branch -M main && git remote add origin <URL> && git push -u origin main`
2. Render / Railway / Fly.io: crear servicio desde el repo (Dockerfile incluido), definir `ADMIN_PASSWORD`, y **montar un disco persistente en /data** (sin disco, SQLite se borra en cada deploy). GitHub Pages NO sirve: necesita backend.

## Pendiente / siguiente paso
WhatsApp (tabla `reminders` ya se llena con 24h/4h/1h; falta un worker + credenciales de Meta Cloud API/Twilio), reprogramar turno, vista calendario día/semana, edición de horarios desde la UI, login real con usuarios, PostgreSQL para producción multi-negocio.
