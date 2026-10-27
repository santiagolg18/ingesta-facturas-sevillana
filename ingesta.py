"""
De un correo a una factura en Supabase.

Hace lo mismo que el workflow de n8n "Facturas (Outlook) - La Sevillana"
(Localizar ZIP → Extraer → Parsear XML → Subir PDF → Buscar proveedor →
Insertar factura → Insertar aprobaciones), con estas mejoras:
  - Si la factura ya existe no se toca (n8n la sobrescribía aunque ya
    estuviera en revisión, aprobada o pagada).
  - El proveedor se busca por NIT normalizado y los aprobadores se arman como
    en createManualInvoice (app/(dashboard)/facturas/actions.ts): en orden,
    solo quienes hoy pueden aprobar, con approval_mode y required_approvals
    del proveedor. Quedan en 'blocked' hasta que Compras libere la factura.
  - Cada correo queda en mail_ingest_log: lo que falló se reintenta y lo que
    no se pudo crear queda a la vista ("revisar").
"""

import logging
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from dian import DianInvoice, DianZipError, read_dian_zip
from graph import GraphClient
from supabase_db import BUCKET, Db, Duplicate

log = logging.getLogger("ingesta")

# Se vuelve a mirar un poco antes del último correo procesado por si Graph
# entrega alguno con retraso; el log evita procesarlo dos veces.
OVERLAP = timedelta(minutes=5)
MAX_PROCESS_PER_MAILBOX = 20


@dataclass
class ZipResult:
    zip: str
    # ok | duplicada | sin_xml | revisar | error
    # "revisar": no se pudo crear y reintentar no lo arregla (falta el PDF o
    # el número); hay que cargarla a mano.
    status: str
    invoice_number: str | None = None
    supplier_nit: str | None = None
    invoice_id: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# Misma regla que canApproveInvoices (lib/auth/current-user.ts).
def can_approve(approver: dict | None) -> bool:
    if not approver or approver.get("is_active") is False:
        return False
    return approver.get("role") == "approver" or approver.get("can_approve") is True


def path_segment(value: str) -> str:
    # Una "/" en el número crearía subcarpetas en Storage.
    return re.sub(r"[\\/]", "_", value.strip())


def create_invoice(db: Db, inv: DianInvoice, pdf: bytes, front: str, message_id: str, zip_name: str) -> ZipResult:
    number = inv.invoice_number
    nit = inv.supplier_nit
    base = {"zip": zip_name, "invoice_number": number, "supplier_nit": nit}

    supplier = db.find_supplier(nit) if nit else None

    # Si ya existe, no se toca: puede estar en revisión, aprobada o pagada.
    if supplier:
        existing = db.find_invoice(supplier["id"], number)
        if existing:
            return ZipResult(**base, status="duplicada", invoice_id=existing)

    # Aprobadores de la ficha del proveedor, en su orden: la preconfiguración
    # que Compras confirma o cambia antes de liberar.
    approver_ids: list[str] = []
    if supplier:
        for rule in db.approval_rules(supplier["id"]):
            if can_approve(rule.get("approvers")) and rule["approver_id"] not in approver_ids:
                approver_ids.append(rule["approver_id"])

    approval_mode = "sequential" if supplier and supplier.get("approval_mode") == "sequential" else "parallel"
    # En cascada aprueban todos; en paralelo se respeta el umbral del proveedor.
    if approval_mode == "sequential":
        required = max(1, len(approver_ids))
    else:
        required = max(1, min((supplier or {}).get("required_approvals") or 1, len(approver_ids) or 1))

    # Misma ruta que usaba n8n.
    object_path = f"facturas/{path_segment(nit or '0')}/{path_segment(number)}.pdf"
    db.upload_pdf(object_path, pdf)

    # Sin proveedor, supplier_id va null y el trigger ensure_invoice_supplier
    # lo crea (o usa "SIN IDENTIFICAR" si no hay NIT).
    try:
        invoice_id = db.insert_invoice(
            {
                "invoice_number": number,
                "supplier_id": supplier["id"] if supplier else None,
                "supplier_nit": nit or "",
                "supplier_name": inv.supplier_name or "",
                "total_amount": inv.total_amount if inv.total_amount is not None else 0,
                "currency": "COP",
                "issue_date": inv.issue_date,
                "due_date": inv.due_date,
                "pdf_storage_path": f"{BUCKET}/{object_path}",
                "email_message_id": message_id,
                "business_front": front,
                "approval_mode": approval_mode,
                "required_approvals": required,
            }
        )
    except Duplicate:
        # Ya existía con el proveedor que resolvió el trigger.
        return ZipResult(**base, status="duplicada")

    if approver_ids:
        try:
            db.insert_approvals(
                [
                    {
                        "invoice_id": invoice_id,
                        "approver_id": approver_id,
                        "token": str(uuid.uuid4()),
                        # 'blocked' hasta que Compras libere la factura.
                        "status": "blocked",
                        "approval_order": index + 1,
                    }
                    for index, approver_id in enumerate(approver_ids)
                ]
            )
        except Exception:
            # Sin esto quedaría una factura a medio crear y el reintento la
            # vería como duplicada.
            db.delete_invoice(invoice_id)
            raise

    return ZipResult(**base, status="ok", invoice_id=invoice_id)


def process_zip(db: Db | None, data: bytes, zip_name: str, front: str, message_id: str) -> ZipResult:
    """db=None es la prueba en seco: lee y cuenta lo que haría, sin escribir."""
    try:
        read = read_dian_zip(data)
    except DianZipError as exc:
        # Un ZIP sin XML de la DIAN no es una factura: se ignora, como en n8n.
        return ZipResult(zip=zip_name, status="sin_xml", detail=str(exc))

    inv = read.invoice
    base = {"zip": zip_name, "invoice_number": inv.invoice_number, "supplier_nit": inv.supplier_nit}
    if not inv.invoice_number:
        return ZipResult(**base, status="revisar", detail="El XML no trae número")
    if read.pdf is None:
        return ZipResult(**base, status="revisar", detail=read.pdf_problem)

    if db is None:
        return ZipResult(
            **base,
            status="ok",
            detail=f"{inv.kind} · {inv.supplier_name} · {inv.total_amount} {inv.currency} · "
            f"emitida {inv.issue_date} · vence {inv.due_date} · PDF {read.pdf_name}",
        )

    try:
        return create_invoice(db, inv, read.pdf, front, message_id, zip_name)
    except Exception as exc:  # noqa: BLE001 — cualquier falla se registra y se reintenta
        return ZipResult(**base, status="error", detail=str(exc))


def message_status(zips: list[ZipResult]) -> str:
    if any(z.status == "error" for z in zips):
        return "error"
    if any(z.status == "revisar" for z in zips):
        return "revisar"
    if not any(z.status in ("ok", "duplicada") for z in zips):
        return "sin_zip"
    return "ok"


def process_message(db: Db | None, graph: GraphClient, front: str, msg: dict) -> tuple[str, list[ZipResult]]:
    zips: list[ZipResult] = []
    for att in graph.list_zip_attachments(msg["id"]):
        name = att.get("name") or "adjunto.zip"
        try:
            data = graph.download_attachment(msg["id"], att["id"])
            zips.append(process_zip(db, data, name, front, msg["id"]))
        except Exception as exc:  # noqa: BLE001
            zips.append(ZipResult(zip=name, status="error", detail=str(exc)))
    return message_status(zips), zips


def run_mailbox(db: Db, graph: GraphClient, mb: dict, persist: bool = True) -> dict:
    """
    Procesa los correos nuevos de un buzón. persist=False en las pruebas con
    --since: no mueve el punto de partida guardado.
    """
    mailbox = mb["mailbox"]
    checkpoint = parse_time(mb["last_received_at"])
    messages = graph.list_messages(checkpoint - OVERLAP)

    # El punto de partida solo avanza por correos ya resueltos y en orden: si
    # uno falla (y le quedan intentos) o lo tiene otra corrida, desde ahí se
    # vuelve a mirar en la siguiente.
    advance_to = checkpoint
    blocked = False
    processed = 0
    counts: dict[str, int] = {}

    for msg in messages:
        if processed >= MAX_PROCESS_PER_MAILBOX:
            break
        received = parse_time(msg["receivedDateTime"])
        claim = db.claim_message(mailbox, msg["id"], msg["receivedDateTime"], msg.get("subject") or "")

        final = True
        if claim == "claimed":
            processed += 1
            error = None
            try:
                status, zips = process_message(db, graph, mb["business_front"], msg)
                failed = [z for z in zips if z.status == "error"]
                if failed:
                    error = " | ".join(f"{z.zip}: {z.detail}" for z in failed)
            except Exception as exc:  # noqa: BLE001
                status, zips, error = "error", [], str(exc)
            db.finish_message(mailbox, msg["id"], status, [z.to_dict() for z in zips], error)
            counts[status] = counts.get(status, 0) + 1
            if status == "error":
                log.error('%s "%s": %s', mailbox, msg.get("subject"), error)
                final = False
            else:
                for z in zips:
                    if z.status in ("ok", "revisar"):
                        log.info("%s %s %s %s %s", mailbox, z.status, z.supplier_nit, z.invoice_number, z.detail or "")
        elif claim == "procesando":
            final = False
        # Cualquier otro valor ('ok', 'sin_zip', 'revisar' o 'error' sin
        # intentos) es un correo ya resuelto.

        if not final:
            blocked = True
        if not blocked and received > advance_to:
            advance_to = received

    if persist:
        db.update_state(
            mailbox,
            {
                "last_received_at": advance_to.isoformat(),
                "last_run_at": datetime.now(timezone.utc).isoformat(),
                "last_run_error": None,
                "last_alert_at": None,
            },
        )

    return {"mailbox": mailbox, "listed": len(messages), "processed": processed, **counts}


def dry_run_mailbox(graph: GraphClient, mb: dict, limit: int = 10) -> list[dict]:
    messages = graph.list_messages(parse_time(mb["last_received_at"]))[:limit]
    report = []
    for msg in messages:
        status, zips = process_message(None, graph, mb["business_front"], msg)
        report.append(
            {
                "received": msg["receivedDateTime"],
                "subject": msg.get("subject"),
                "status": status,
                "zips": [z.to_dict() for z in zips],
            }
        )
    return report
