# Boletín semanal (ECDC) – Agente automático

Envía por correo un **resumen en español** del último informe semanal del ECDC.

## 1) Secrets necesarios (Settings → Secrets and variables → Actions)
Crea **uno por uno**:
- `SMTP_SERVER` → `smtp.gmail.com`
- `SMTP_PORT` → `465`
- `SENDER_EMAIL` → `agentia70@gmail.com`
- `RECEIVER_EMAIL` → `contra1270@gmail.com`
- `EMAIL_PASSWORD` → **Contraseña de aplicación** de `agentia70@gmail.com` (16 caracteres, 2FA activada)

## 2) Variables opcionales (Settings → … → Actions → Variables)
- `BASE_URL` (por defecto ya apunta al listado del ECDC)
- `PDF_PATTERN` → por defecto `\.pdf` (más flexible)
- `SUMMARY_SENTENCES` → p.ej. `8`
- `CA_FILE` → normalmente vacío

## 3) Ejecutar
- Pestaña **Actions** → workflow **Enviar resumen semanal del ECDC** → **Run workflow**.  
- **Modo prueba** (`--dry-run`): no envía correo, solo logs.

Para enviar de verdad, edita `.github/workflows/weekly-report.yml` y cambia:
