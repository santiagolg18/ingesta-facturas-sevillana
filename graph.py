"""
Lectura de los buzones de Outlook (Microsoft 365).

Es la misma conexión que usaba el nodo "Microsoft Outlook" de n8n: Microsoft
Graph con el usuario que inició sesión (permiso delegado Mail.Read). La sesión
se abre una sola vez por buzón con login.py; msal (la librería oficial de
Microsoft) la renueva sola mientras se siga usando.

Solo lee: no envía, no mueve, no borra y no marca como leído.
"""

import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
import msal

GRAPH = "https://graph.microsoft.com/v1.0"
# Mail.Read para leer el buzón y User.Read para confirmar en login.py de quién
# es la cuenta. msal agrega solo openid, profile y offline_access (la renovación).
SCOPES = ["Mail.Read", "User.Read"]

# Respuestas de Microsoft que significan "hay que volver a iniciar sesión".
LOGIN_ERRORS = {"invalid_grant", "interaction_required", "invalid_client", "unauthorized_client"}

# Los correos que se miran por corrida y buzón. Lo que no alcance entra en la
# siguiente (un minuto después).
LIST_TOP = 50

# Misma regla que "Localizar ZIP" en n8n.
ZIP_MIMES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/x-zip",
    "multipart/x-zip",
}


class LoginExpired(Exception):
    """La sesión guardada del buzón ya no sirve: hay que correr login.py."""


class GraphError(Exception):
    pass


def msal_app(client_id: str, tenant_id: str, cache: msal.SerializableTokenCache | None = None):
    return msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )


def access_token(client_id: str, tenant_id: str, mailbox: str, token_cache: str | None) -> tuple[str, str | None]:
    """
    Token de acceso para el buzón a partir de su sesión guardada.
    Devuelve también la sesión actualizada si msal la renovó (hay que
    guardarla), o None si no cambió.
    """
    cache = msal.SerializableTokenCache()
    if token_cache:
        cache.deserialize(token_cache)
    app = msal_app(client_id, tenant_id, cache)
    # Cada buzón guarda su propia sesión, con una sola cuenta adentro. No se
    # busca por correo: el nombre de inicio de sesión puede ser distinto de la
    # dirección del buzón.
    accounts = app.get_accounts()
    if not accounts:
        raise LoginExpired("no hay sesión guardada para este buzón")

    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result:
        raise LoginExpired("la sesión guardada ya no es válida")
    if "access_token" not in result:
        detail = (result.get("error_description") or result.get("error") or "").splitlines()[0]
        if result.get("error") in LOGIN_ERRORS:
            raise LoginExpired(detail)
        raise GraphError(f"Microsoft no dio acceso: {detail}")

    return result["access_token"], (cache.serialize() if cache.has_state_changed else None)


def graph_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GraphClient:
    def __init__(self, token: str, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=60)
        self.headers = {
            "Authorization": f"Bearer {token}",
            # Ids que no cambian si una regla del buzón mueve el correo de
            # carpeta: con los normales, el log no lo reconocería.
            "Prefer": 'IdType="ImmutableId"',
        }

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        # Reintento ante 429/5xx, respetando Retry-After.
        for attempt in range(1, 4):
            try:
                resp = self.http.get(url, params=params, headers=self.headers)
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise GraphError(f"sin conexión con Microsoft: {exc}") from exc
                time.sleep(attempt * 2)
                continue
            if resp.status_code < 400:
                return resp
            if resp.status_code != 429 and resp.status_code < 500 or attempt == 3:
                raise GraphError(f"Graph {resp.status_code}: {resp.text[:300]}")
            wait = resp.headers.get("Retry-After", "")
            time.sleep(min(int(wait) if wait.isdigit() else attempt * 2, 10))
        raise GraphError("Graph no respondió")

    def me(self) -> dict:
        return self._get(f"{GRAPH}/me", params={"$select": "mail,userPrincipalName"}).json()

    def list_messages(self, since: datetime) -> list[dict]:
        # /me/messages (todas las carpetas), como el trigger de n8n: si una
        # regla mueve las facturas a otra carpeta, igual se ven. Graph exige
        # que lo que va en $orderby aparezca primero en $filter.
        resp = self._get(
            f"{GRAPH}/me/messages",
            params={
                "$filter": f"receivedDateTime ge {graph_datetime(since)} and hasAttachments eq true",
                "$orderby": "receivedDateTime asc",
                "$top": str(LIST_TOP),
                "$select": "id,subject,receivedDateTime,isDraft",
            },
        )
        return [m for m in resp.json().get("value", []) if not m.get("isDraft")]

    def list_zip_attachments(self, message_id: str) -> list[dict]:
        resp = self._get(
            f"{GRAPH}/me/messages/{quote(message_id, safe='')}/attachments",
            params={"$select": "id,name,contentType,size,isInline"},
        )
        return [a for a in resp.json().get("value", []) if is_zip(a)]

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        resp = self._get(
            f"{GRAPH}/me/messages/{quote(message_id, safe='')}"
            f"/attachments/{quote(attachment_id, safe='')}/$value"
        )
        return resp.content


def is_zip(attachment: dict) -> bool:
    if attachment.get("@odata.type") != "#microsoft.graph.fileAttachment":
        return False
    name = (attachment.get("name") or "").lower()
    mime = (attachment.get("contentType") or "").lower()
    return name.endswith(".zip") or mime in ZIP_MIMES
