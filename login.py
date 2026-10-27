"""
Inicio de sesión de un buzón (se hace una sola vez por buzón, como al
conectar Outlook en n8n).

  python login.py facturas@lasevillana.com.co parrilla
  python login.py facturasagropecuaria@lasevillana.com.co agropecuaria

Muestra un código: se abre https://microsoft.com/devicelogin, se escribe el
código y se inicia sesión con la cuenta DEL BUZÓN. La sesión queda guardada en
Supabase (mail_ingest_state.token_cache) y el worker la renueva solo.

Si el buzón es nuevo queda creado APAGADO (enabled = false): se enciende el día
del corte (README, "Paso de n8n"). Repetirlo en un buzón que ya existe solo
cambia la sesión guardada; no toca hasta dónde se ha leído.
"""

import sys
from datetime import datetime, timedelta, timezone

import msal

import config
from graph import SCOPES, GraphClient, msal_app
from supabase_db import Db

FRONTS = ("parrilla", "agropecuaria")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[2] not in FRONTS:
        raise SystemExit("Uso: python login.py <correo del buzón> <parrilla|agropecuaria>")
    mailbox, front = sys.argv[1].strip().lower(), sys.argv[2]

    db = Db(config.required("SUPABASE_URL"), config.required("SUPABASE_SERVICE_ROLE_KEY"))
    cache = msal.SerializableTokenCache()
    app = msal_app(config.required("MS_CLIENT_ID"), config.required("MS_TENANT_ID"), cache)

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise SystemExit(f"Microsoft no dio un código: {flow.get('error_description', flow)}")
    print()
    print(f"  1. Abre {flow['verification_uri']}")
    print(f"  2. Escribe este código: {flow['user_code']}")
    print(f"  3. Inicia sesión con la cuenta {mailbox} y acepta el permiso de leer correo.")
    print()
    print("Esperando a que inicies sesión...")

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise SystemExit(f"No se pudo iniciar sesión: {result.get('error_description', result.get('error'))}")

    graph = GraphClient(result["access_token"])
    me = graph.me()
    names = {(me.get("mail") or "").lower(), (me.get("userPrincipalName") or "").lower()}
    if mailbox not in names:
        # Con otra cuenta el worker leería otro buzón: no se guarda nada.
        signed_in = me.get("mail") or me.get("userPrincipalName") or "otra cuenta"
        raise SystemExit(f"Iniciaste sesión con {signed_in}, no con {mailbox}. No se guardó nada.")

    # Prueba de lectura antes de guardar.
    recent = graph.list_messages(datetime.now(timezone.utc) - timedelta(days=1))

    row = db.get_mailbox(mailbox)
    if row:
        db.update_state(mailbox, {"token_cache": cache.serialize(), "last_run_error": None, "last_alert_at": None})
        if row["business_front"] != front:
            print(f"Ojo: {mailbox} está registrado como {row['business_front']}, no como {front}. No se cambió.")
        estado = "encendido" if row["enabled"] else "apagado (se enciende en el corte)"
        print(f"Listo: sesión renovada para {mailbox}. El buzón sigue {estado}.")
    else:
        db.create_mailbox(mailbox, front, cache.serialize(), datetime.now(timezone.utc).isoformat())
        print(f"Listo: {mailbox} quedó registrado como {front}, apagado hasta el corte.")
    print(f"En las últimas 24 horas tiene {len(recent)} correos con adjuntos.")


if __name__ == "__main__":
    main()
