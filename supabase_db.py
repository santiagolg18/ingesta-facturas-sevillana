"""
Acceso a Supabase (PostgREST + Storage) con httpx, como en pdf-service.

Usa la service role key: las tablas de control (mail_ingest_state,
mail_ingest_log) tienen RLS sin políticas y solo este rol las ve.
Las protecciones de la base siguen activas para todo lo que se inserta:
ensure_invoice_supplier, apply_supplier_auto_archive, guard_approval_gate y el
UNIQUE (supplier_id, invoice_number) de invoices.
"""

import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

BUCKET = "invoices"

STATE_COLUMNS = "mailbox,business_front,last_received_at,enabled,token_cache,last_alert_at,last_run_error"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DbError(Exception):
    pass


class Duplicate(Exception):
    """La factura ya existe (UNIQUE supplier_id + invoice_number)."""


class Db:
    def __init__(self, url: str, service_key: str, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.http = http or httpx.Client(timeout=60)
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
        }

    # --- helpers -------------------------------------------------------------

    def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        # Supabase a veces responde 5xx un instante (se vio un 520 de Storage en
        # las pruebas). Solo se repite lo que se puede repetir sin efectos: leer,
        # actualizar y subir el PDF (upsert). Las inserciones no, para no crear
        # nada dos veces; si fallan, el correo se reintenta en la siguiente vuelta.
        retry = method in ("GET", "PATCH") or "/storage/" in url
        for attempt in range(1, 4):
            try:
                resp = self.http.request(method, url, **kwargs)
            except httpx.TransportError:
                if not retry or attempt == 3:
                    raise
                time.sleep(attempt * 2)
                continue
            if resp.status_code < 500 or not retry or attempt == 3:
                return resp
            time.sleep(attempt * 2)
        raise DbError("Supabase no respondió")

    def _rest(self, method: str, path: str, *, params=None, json=None, prefer: str | None = None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        return self._send(method, f"{self.url}/rest/v1/{path}", params=params, json=json, headers=headers)

    @staticmethod
    def _check(resp: httpx.Response, what: str) -> httpx.Response:
        if resp.status_code >= 400:
            raise DbError(f"{what}: {resp.status_code} {resp.text[:300]}")
        return resp

    # --- buzones -------------------------------------------------------------

    def mailboxes(self, only: str | None = None) -> list[dict]:
        params = {"select": STATE_COLUMNS}
        if only:
            params["mailbox"] = f"eq.{only}"
        else:
            params["enabled"] = "eq.true"
        return self._check(self._rest("GET", "mail_ingest_state", params=params), "buzones").json()

    def get_mailbox(self, mailbox: str) -> dict | None:
        rows = self.mailboxes(only=mailbox)
        return rows[0] if rows else None

    def create_mailbox(self, mailbox: str, business_front: str, token_cache: str, since_iso: str) -> None:
        # Nace apagado: se enciende en el corte (README, "Paso de n8n").
        self._check(
            self._rest(
                "POST",
                "mail_ingest_state",
                json={
                    "mailbox": mailbox,
                    "business_front": business_front,
                    "last_received_at": since_iso,
                    "enabled": False,
                    "token_cache": token_cache,
                },
            ),
            "crear buzón",
        )

    def update_state(self, mailbox: str, values: dict) -> None:
        self._check(
            self._rest("PATCH", "mail_ingest_state", params={"mailbox": f"eq.{mailbox}"}, json=values),
            "estado del buzón",
        )

    # --- log de correos ------------------------------------------------------

    def claim_message(self, mailbox: str, message_id: str, received_at: str, subject: str) -> str:
        resp = self._rest(
            "POST",
            "rpc/claim_mail_message",
            json={
                "p_mailbox": mailbox,
                "p_message_id": message_id,
                "p_received_at": received_at,
                "p_subject": subject[:300],
            },
        )
        return self._check(resp, "log").json()

    def finish_message(self, mailbox: str, message_id: str, status: str, details: list, error: str | None) -> None:
        self._check(
            self._rest(
                "PATCH",
                "mail_ingest_log",
                params={"mailbox": f"eq.{mailbox}", "message_id": f"eq.{message_id}"},
                json={"status": status, "details": details, "error": error, "updated_at": _now()},
            ),
            "log",
        )

    # --- facturas ------------------------------------------------------------

    def find_supplier(self, nit: str) -> dict | None:
        rows = self._check(
            self._rest(
                "GET",
                "suppliers",
                params={"select": "id,required_approvals,approval_mode", "nit": f"eq.{nit}", "limit": "1"},
            ),
            "proveedor",
        ).json()
        return rows[0] if rows else None

    def find_invoice(self, supplier_id: str, invoice_number: str) -> str | None:
        rows = self._check(
            self._rest(
                "GET",
                "invoices",
                params={
                    "select": "id",
                    "supplier_id": f"eq.{supplier_id}",
                    "invoice_number": f"eq.{invoice_number}",
                    "limit": "1",
                },
            ),
            "factura existente",
        ).json()
        return rows[0]["id"] if rows else None

    def approval_rules(self, supplier_id: str) -> list[dict]:
        return self._check(
            self._rest(
                "GET",
                "approval_rules",
                params={
                    "select": "approver_id,approvers(role,is_active,can_approve)",
                    "supplier_id": f"eq.{supplier_id}",
                    "order": "approval_order.asc",
                },
            ),
            "reglas",
        ).json()

    def insert_invoice(self, row: dict) -> str:
        resp = self._rest(
            "POST", "invoices", params={"select": "id"}, json=row, prefer="return=representation"
        )
        if resp.status_code == 409 and '"23505"' in resp.text:
            raise Duplicate()
        return self._check(resp, "factura").json()[0]["id"]

    def insert_approvals(self, rows: list[dict]) -> None:
        self._check(self._rest("POST", "approvals", json=rows), "aprobaciones")

    def delete_invoice(self, invoice_id: str) -> None:
        self._check(self._rest("DELETE", "invoices", params={"id": f"eq.{invoice_id}"}), "borrar factura")

    def upload_pdf(self, object_path: str, data: bytes) -> None:
        resp = self._send(
            "POST",
            f"{self.url}/storage/v1/object/{BUCKET}/{quote(object_path, safe='/')}",
            content=data,
            headers={**self.headers, "Content-Type": "application/pdf", "x-upsert": "true"},
        )
        self._check(resp, "PDF")

    # --- secretos ------------------------------------------------------------

    def vault_secret(self, name: str) -> str | None:
        resp = self._rest("POST", "rpc/get_vault_secret", json={"secret_name": name})
        return self._check(resp, "vault").json()
