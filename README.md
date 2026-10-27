# ingesta-facturas

Mete en Supabase las facturas electrónicas que llegan por correo a los buzones de
**Parrilla** y **Agropecuaria**. Reemplaza el workflow de n8n
"Facturas (Outlook) - La Sevillana".

Cada minuto, por cada buzón:

1. Busca los correos nuevos con adjuntos.
2. Saca el ZIP de la DIAN y lee el XML: número, NIT, proveedor, valor y fechas.
3. Sube el PDF a Storage (`invoices/facturas/<NIT>/<número>.pdf`).
4. Crea la factura en "Por revisar" con los aprobadores del proveedor, bloqueados
   hasta que Compras la libere. Es lo mismo que hace «+ Nueva factura» en la app.

No pasa por Vercel: escribe directo en Supabase. Solo lee el correo: no envía, no
borra, no mueve y no marca como leído.

## Dónde corre

En Render, como **Background Worker** (plan Starter, unos US$7 al mes). El plan
gratis de Render no sirve porque se duerme a los 15 minutos.

## Archivos

| Archivo | Qué hace |
|---|---|
| `worker.py` | El proceso que corre en Render: una vuelta cada 60 segundos |
| `login.py` | Inicio de sesión de un buzón (una sola vez, como al conectar Outlook en n8n) |
| `graph.py` | Lectura del correo de Microsoft 365 (librería oficial `msal`) |
| `dian.py` | Lector del ZIP y del XML de la DIAN (misma lógica que `lib/invoices/dian-zip.ts` en la app) |
| `ingesta.py` | De un correo a una factura: duplicados, aprobadores, reintentos |
| `supabase_db.py` | Llamadas a Supabase |
| `alertas.py` | Aviso por correo si un buzón deja de leerse |

En Supabase usa las tablas `mail_ingest_state` (buzones y su sesión) y
`mail_ingest_log` (un registro por correo). Están en `sql/20_ingesta_correo.sql` y
`sql/21_ingesta_python.sql` del repo de la app.

## Preparar el computador (una vez)

```bash
cd ~/Desktop/Proyecto_Aprobaciones/ingesta-facturas
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env      # y llenar los valores
```

## Iniciar sesión en un buzón

Se hace una vez por buzón. Se repite solo si llega el correo de alerta, por ejemplo
porque le cambiaron la contraseña al buzón.

```bash
.venv/bin/python login.py <correo del buzón de Parrilla> parrilla
.venv/bin/python login.py <correo del buzón de Agropecuaria> agropecuaria
```

El comando muestra un código. Abre https://microsoft.com/devicelogin, escribe el
código e inicia sesión **con la cuenta del buzón**, no con la tuya. La sesión queda
guardada en Supabase y el worker la renueva solo.

Un buzón nuevo queda **apagado** hasta el corte.

## Probar sin escribir nada

```bash
.venv/bin/python worker.py --dry-run --mailbox <correo> --since 2026-10-22T00:00:00Z
```

Lista los correos desde esa fecha y lo que haría con cada factura.

Para correr las pruebas automáticas:

```bash
.venv/bin/python -m pytest -q
```

## Desplegar en Render

1. Crear el repo `ingesta-facturas` en GitHub y subir esta carpeta.
2. En Render: **New → Blueprint** y elegir el repo. Render lee `render.yaml` y crea
   el worker `ingesta-facturas`.
3. Llenar las variables que pide: `SUPABASE_SERVICE_ROLE_KEY` (la misma del
   pdf-service), `MS_CLIENT_ID`, `MS_TENANT_ID` y `ALERT_EMAIL`.
4. Cada `git push` a `main` despliega solo.

En **Logs** del worker se ve cada factura que entra (`ok`, `revisar`) y cualquier error.

## Paso de n8n (el día del corte)

Con el worker ya desplegado en Render (los buzones apagados no hacen nada):

1. Desactivar el workflow en n8n.
2. Enseguida, encender los dos buzones leyendo desde media hora antes, para que
   no se escape ningún correo que llegó durante el cambio:
   ```sql
   update mail_ingest_state set enabled = true, last_received_at = now() - interval '30 minutes';
   ```
3. Confirmar en los logs de Render que aparecen las vueltas de los dos buzones.

Las facturas de esa media hora que ya había metido n8n se detectan como
repetidas y no se duplican.

## Revisar qué pasó

```sql
-- Lo último que entró
select mailbox, received_at, subject, status, details
from mail_ingest_log order by received_at desc limit 20;

-- Lo que hay que mirar: "revisar" = el ZIP no trae PDF o número (cargarla con
-- «+ Nueva factura»); "error" = se reintentó 5 veces sin éxito
select * from mail_ingest_log where status in ('error', 'revisar') order by updated_at desc;

-- Estado de cada buzón
select mailbox, enabled, last_received_at, last_run_at, last_run_error from mail_ingest_state;
```

Estados de un correo en `mail_ingest_log`:

| Estado | Qué significa |
|---|---|
| `ok` | Entró la factura, o ya existía (`duplicada` en `details`) y no se tocó |
| `sin_zip` | No trae ZIP de la DIAN: se ignora, como en n8n |
| `revisar` | Trae XML pero le falta el PDF o el número: hay que cargarla a mano |
| `error` | Algo falló; se reintenta solo hasta 5 veces |

Para reintentar un correo que agotó sus 5 intentos:

```sql
update mail_ingest_log set attempts = 0 where message_id = '<id>';
update mail_ingest_state set last_received_at = '<received_at del correo>' where mailbox = '<buzón>';
```

## Si llega el correo "Las facturas de … no están entrando"

Microsoft cerró la sesión de ese buzón (cambio de contraseña, sesión revocada).
Corre de nuevo `login.py` para ese buzón. Las facturas que llegaron mientras tanto
entran solas en el minuto siguiente: no se pierde ninguna.

## Diferencias con n8n

- Si la factura ya existe, no se toca. n8n la sobrescribía aunque ya estuviera en
  revisión, aprobada o pagada.
- Los aprobadores respetan el modo del proveedor (cascada o paralelo) y su orden.
  n8n los metía todos en paralelo.
- El proveedor se busca por NIT sin dígito de verificación. Con otro formato, n8n no
  lo encontraba y la factura entraba sin aprobadores.
- Las notas crédito y débito entran como factura, igual que en n8n, pero con su
  número real.
- La rama "Webhook Aprobar" de n8n no se migró, porque la app no usa esos enlaces.
