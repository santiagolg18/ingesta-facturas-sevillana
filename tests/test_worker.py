"""Sesión vencida: se registra el error, se avisa una vez al día y el otro buzón sigue."""

from datetime import datetime, timezone

import alertas
import worker
from graph import LoginExpired


class StateDb:
    def __init__(self, rows):
        self.rows = {r["mailbox"]: r for r in rows}

    def mailboxes(self, only=None):
        return [dict(r) for r in self.rows.values() if only in (None, r["mailbox"])]

    def update_state(self, mailbox, values):
        self.rows[mailbox].update(values)

    def vault_secret(self, name):
        return "re_prueba"


def test_sesion_vencida_avisa_una_vez(monkeypatch):
    monkeypatch.setenv("MS_CLIENT_ID", "cliente")
    monkeypatch.setenv("MS_TENANT_ID", "inquilino")
    monkeypatch.setenv("ALERT_EMAIL", "compras@prueba.co")

    db = StateDb(
        [
            {"mailbox": "a@prueba.co", "business_front": "parrilla", "last_received_at": "2026-10-23T09:00:00+00:00"},
            {"mailbox": "b@prueba.co", "business_front": "agropecuaria", "last_received_at": "2026-10-23T09:00:00+00:00"},
        ]
    )

    def fake_token(client_id, tenant_id, mailbox, cache):
        if mailbox == "a@prueba.co":
            raise LoginExpired("AADSTS50173: la contraseña cambió")
        return "token", None

    corridos = []
    monkeypatch.setattr(worker, "access_token", fake_token)
    monkeypatch.setattr(worker, "run_mailbox", lambda db, graph, mb, persist: corridos.append(mb["mailbox"]) or {"processed": 0})

    enviados = []

    class Resp:
        status_code = 200
        text = ""

    monkeypatch.setattr(alertas.httpx, "post", lambda url, **kw: enviados.append(kw["json"]) or Resp())

    worker.run_cycle(db)
    worker.run_cycle(db)

    # El buzón bueno sigue funcionando.
    assert corridos == ["b@prueba.co", "b@prueba.co"]
    # El vencido queda marcado y se avisa una sola vez.
    assert "login.py a@prueba.co parrilla" in db.rows["a@prueba.co"]["last_run_error"]
    assert len(enviados) == 1
    assert enviados[0]["to"] == ["compras@prueba.co"]
    assert "a@prueba.co" in enviados[0]["subject"]
    assert datetime.fromisoformat(db.rows["a@prueba.co"]["last_alert_at"]) <= datetime.now(timezone.utc)
