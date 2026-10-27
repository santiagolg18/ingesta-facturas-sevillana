"""
Aviso por correo cuando un buzón deja de leerse por la sesión de Microsoft
(cambio de contraseña del buzón, sesión revocada, app desactivada).

Sin este aviso las facturas simplemente dejarían de entrar y nadie se daría
cuenta hasta que alguien las extrañe. Se manda con Resend, la misma cuenta de
los recordatorios (llave en el Vault: resend_api_key), máximo una vez al día
por buzón.
"""

import logging
from datetime import datetime, timedelta, timezone
from html import escape

import httpx

from supabase_db import Db

log = logging.getLogger("ingesta")

FROM = "La Sevillana <facturacionagropecuaria@lasevillana.com.co>"
EVERY = timedelta(hours=24)


def maybe_alert(db: Db, mb: dict, detail: str, to: str | None) -> None:
    if not to:
        log.warning("Sin ALERT_EMAIL: no se avisa que %s dejó de leerse", mb["mailbox"])
        return
    last = mb.get("last_alert_at")
    now = datetime.now(timezone.utc)
    if last and now - datetime.fromisoformat(last) < EVERY:
        return

    key = db.vault_secret("resend_api_key")
    if not key:
        log.error("Falta resend_api_key en el Vault: no se pudo avisar")
        return

    mailbox = escape(mb["mailbox"])
    html = (
        f"<p>Desde este momento no están entrando las facturas del buzón <strong>{mailbox}</strong>: "
        f"Microsoft pide volver a iniciar sesión.</p>"
        f"<p>Detalle: {escape(detail)}</p>"
        f"<p>Para arreglarlo, en la carpeta <code>ingesta-facturas</code> corre:</p>"
        f"<pre>python login.py {mailbox} {escape(mb['business_front'])}</pre>"
        f"<p>Inicia sesión con la cuenta del buzón. Las facturas que llegaron mientras tanto entran solas "
        f"en el siguiente minuto: no se pierde ninguna.</p>"
    )
    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "from": FROM,
            "to": [to],
            "subject": f"Las facturas de {mb['mailbox']} no están entrando",
            "html": html,
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        log.error("No se pudo mandar la alerta: %s %s", resp.status_code, resp.text[:300])
        return
    db.update_state(mb["mailbox"], {"last_alert_at": now.isoformat()})
    log.info("Alerta enviada a %s por %s", to, mb["mailbox"])
