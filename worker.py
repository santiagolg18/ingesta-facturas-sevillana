"""
Worker de ingesta de facturas (reemplaza el workflow de n8n).

Cada minuto, por cada buzón activo de mail_ingest_state, lee los correos
nuevos con un ZIP de la DIAN y crea la factura en Supabase.

En Render corre como Background Worker:  python worker.py

Para probar en local:
  python worker.py --once                    una sola vuelta
  python worker.py --dry-run --since 2026-10-22T00:00:00Z
                                              lee y muestra lo que haría, sin escribir nada
  python worker.py --once --mailbox facturas@lasevillana.com.co
                                              solo ese buzón (aunque esté apagado)

Variables de entorno: ver .env.example.
"""

import argparse
import json
import logging
import signal
import threading
import time
from datetime import datetime, timezone

import alertas
import config
from graph import GraphClient, LoginExpired, access_token
from ingesta import dry_run_mailbox, parse_time, run_mailbox
from supabase_db import Db

INTERVAL_SECONDS = 60

log = logging.getLogger("ingesta")
stop = threading.Event()


def run_cycle(db: Db, *, dry_run: bool = False, since: datetime | None = None, only: str | None = None) -> list[dict]:
    client_id = config.required("MS_CLIENT_ID")
    tenant_id = config.required("MS_TENANT_ID")
    alert_to = config.optional("ALERT_EMAIL")
    results = []

    for mb in db.mailboxes(only):
        mailbox = mb["mailbox"]
        if since:
            mb = {**mb, "last_received_at": since.isoformat()}
        try:
            token, new_cache = access_token(client_id, tenant_id, mailbox, mb.get("token_cache"))
            if new_cache:
                db.update_state(mailbox, {"token_cache": new_cache})
            graph = GraphClient(token)

            if dry_run:
                results.append({"mailbox": mailbox, "business_front": mb["business_front"], "messages": dry_run_mailbox(graph, mb)})
                continue

            summary = run_mailbox(db, graph, mb, persist=since is None)
            results.append(summary)
            if summary["processed"]:
                log.info("%s", json.dumps(summary, ensure_ascii=False))
        except LoginExpired as exc:
            message = f"Hay que volver a iniciar sesión (python login.py {mailbox} {mb['business_front']}): {exc}"
            log.error("%s: %s", mailbox, message)
            results.append({"mailbox": mailbox, "error": message})
            if not dry_run:
                db.update_state(mailbox, {"last_run_at": _now(), "last_run_error": message})
                alertas.maybe_alert(db, mb, str(exc), alert_to)
        except Exception as exc:  # noqa: BLE001 — un buzón con problemas no frena al otro
            log.exception("%s: %s", mailbox, exc)
            results.append({"mailbox": mailbox, "error": str(exc)})
            if not dry_run:
                db.update_state(mailbox, {"last_run_at": _now(), "last_run_error": str(exc)[:500]})

    return results


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingesta de facturas desde Outlook")
    parser.add_argument("--once", action="store_true", help="una sola vuelta y termina")
    parser.add_argument("--dry-run", action="store_true", help="lee y muestra lo que haría, sin escribir nada")
    parser.add_argument("--since", help="desde cuándo mirar (ISO, p. ej. 2026-10-22T00:00:00Z); no mueve el punto de partida guardado")
    parser.add_argument("--mailbox", help="solo este buzón, aunque esté apagado")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx registra cada petición en INFO: con un ciclo por minuto tapa lo importante.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db = Db(config.required("SUPABASE_URL"), config.required("SUPABASE_SERVICE_ROLE_KEY"))
    since = parse_time(args.since) if args.since else None

    if args.dry_run or args.once:
        results = run_cycle(db, dry_run=args.dry_run, since=since, only=args.mailbox)
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return

    # Render manda SIGTERM al desplegar una versión nueva: se termina la
    # vuelta en curso y se sale limpio.
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    log.info("Worker iniciado: revisa los buzones cada %s s", INTERVAL_SECONDS)
    while not stop.is_set():
        started = time.monotonic()
        try:
            run_cycle(db, since=since, only=args.mailbox)
        except Exception:  # noqa: BLE001 — p. ej. Supabase caído un momento: se intenta en la siguiente
            log.exception("La vuelta falló")
        stop.wait(max(5, INTERVAL_SECONDS - (time.monotonic() - started)))
    log.info("Worker detenido")


if __name__ == "__main__":
    main()
