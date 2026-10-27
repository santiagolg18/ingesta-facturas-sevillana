"""
Flujo completo con un buzón y una base de mentira: mismas reglas que en
Supabase (UNIQUE de facturas, claim_mail_message, reintentos).
"""

import uuid

from graph import GraphError
from helpers import PDF, attached_document, document_xml, invoice_zip, make_zip
from ingesta import run_mailbox
from supabase_db import Duplicate

PROVEEDOR = {"id": "sup-1", "nit": "901306960", "required_approvals": 2, "approval_mode": "sequential"}
APROBADOR = {"role": "approver", "is_active": True, "can_approve": False}


class FakeDb:
    def __init__(self):
        self.suppliers = {PROVEEDOR["nit"]: PROVEEDOR}
        self.rules = {
            "sup-1": [
                {"approver_id": "ap-1", "approvers": APROBADOR},
                {"approver_id": "ap-inactivo", "approvers": {**APROBADOR, "is_active": False}},
                {"approver_id": "ap-2", "approvers": {"role": "purchasing", "is_active": True, "can_approve": True}},
            ]
        }
        self.invoices: dict[str, dict] = {}
        self.approvals: list[dict] = []
        self.storage: dict[str, bytes] = {}
        self.log: dict[str, dict] = {}
        self.state: dict[str, dict] = {}
        self.fail_approvals = False

    def claim_message(self, mailbox, message_id, received_at, subject):
        row = self.log.get(message_id)
        if row is None:
            self.log[message_id] = {"status": "procesando", "attempts": 1}
            return "claimed"
        if row["status"] == "error" and row["attempts"] < 5:
            row.update(status="procesando", attempts=row["attempts"] + 1)
            return "claimed"
        return row["status"]

    def finish_message(self, mailbox, message_id, status, details, error):
        self.log[message_id].update(status=status, details=details, error=error)

    def find_supplier(self, nit):
        return self.suppliers.get(nit)

    def find_invoice(self, supplier_id, number):
        for id_, inv in self.invoices.items():
            if inv["supplier_id"] == supplier_id and inv["invoice_number"] == number:
                return id_
        return None

    def approval_rules(self, supplier_id):
        return self.rules.get(supplier_id, [])

    def upload_pdf(self, path, data):
        self.storage[path] = data

    def insert_invoice(self, row):
        # El trigger ensure_invoice_supplier crea el proveedor si falta.
        if row["supplier_id"] is None:
            sup = self.suppliers.setdefault(row["supplier_nit"], {"id": f"auto-{row['supplier_nit']}", "required_approvals": 1})
            row = {**row, "supplier_id": sup["id"]}
        if self.find_invoice(row["supplier_id"], row["invoice_number"]):
            raise Duplicate()
        id_ = str(uuid.uuid4())
        self.invoices[id_] = row
        return id_

    def insert_approvals(self, rows):
        if self.fail_approvals:
            raise RuntimeError("aprobaciones: falla simulada")
        self.approvals.extend(rows)

    def delete_invoice(self, invoice_id):
        del self.invoices[invoice_id]

    def update_state(self, mailbox, values):
        self.state.setdefault(mailbox, {}).update(values)


class FakeGraph:
    def __init__(self, messages: dict[str, tuple[str, list[tuple[str, str, bytes]]]]):
        # id → (receivedDateTime, [(nombre, contentType, bytes)])
        self.messages = messages
        self.fail_download: set[str] = set()

    def list_messages(self, since):
        return [
            {"id": id_, "subject": f"correo {id_}", "receivedDateTime": at}
            for id_, (at, _) in sorted(self.messages.items(), key=lambda kv: kv[1][0])
        ]

    def list_zip_attachments(self, message_id):
        from graph import is_zip

        atts = [
            {"@odata.type": "#microsoft.graph.fileAttachment", "id": f"{message_id}/{n}", "name": n, "contentType": ct}
            for n, ct, _ in self.messages[message_id][1]
        ]
        return [a for a in atts if is_zip(a)]

    def download_attachment(self, message_id, attachment_id):
        if message_id in self.fail_download:
            raise GraphError("Graph 503: simulado")
        name = attachment_id.split("/", 1)[1]
        return next(data for n, _, data in self.messages[message_id][1] if n == name)


MB = {"mailbox": "facturas@prueba.co", "business_front": "parrilla", "last_received_at": "2026-10-23T09:00:00+00:00"}


def zip_att(data, name="factura.zip", ct="application/zip"):
    return (name, ct, data)


def test_factura_nueva_con_aprobadores():
    db = FakeDb()
    graph = FakeGraph({"m1": ("2026-10-23T10:00:00Z", [zip_att(invoice_zip(number="FE1"), ct="application/octet-stream")])})
    summary = run_mailbox(db, graph, MB)

    assert summary["ok"] == 1
    (inv,) = db.invoices.values()
    assert inv["invoice_number"] == "FE1"
    assert inv["business_front"] == "parrilla"
    assert inv["pdf_storage_path"] == "invoices/facturas/901306960/FE1.pdf"
    assert inv["email_message_id"] == "m1"
    # Cascada: aprueban todos los que pueden aprobar, en orden; el inactivo no entra.
    assert inv["approval_mode"] == "sequential" and inv["required_approvals"] == 2
    assert [(a["approver_id"], a["status"], a["approval_order"]) for a in db.approvals] == [
        ("ap-1", "blocked", 1),
        ("ap-2", "blocked", 2),
    ]
    assert db.storage["facturas/901306960/FE1.pdf"] == PDF
    assert db.state[MB["mailbox"]]["last_received_at"] == "2026-10-23T10:00:00+00:00"


def test_duplicada_no_se_toca_y_el_correo_repetido_se_salta():
    db = FakeDb()
    graph = FakeGraph(
        {
            "m1": ("2026-10-23T10:00:00Z", [zip_att(invoice_zip(number="FE1"))]),
            "m2": ("2026-10-23T10:01:00Z", [zip_att(invoice_zip(number="FE1", total="1"))]),
        }
    )
    run_mailbox(db, graph, MB)
    assert len(db.invoices) == 1
    assert next(iter(db.invoices.values()))["total_amount"] == 119000.0
    assert db.log["m2"]["details"][0]["status"] == "duplicada"

    # Segunda vuelta: los dos correos ya están resueltos.
    assert run_mailbox(db, graph, MB)["processed"] == 0


def test_sin_zip_zip_sin_xml_sin_pdf_y_nota_credito():
    db = FakeDb()
    nota = make_zip({"nc.xml": attached_document(document_xml(root="CreditNote", number="NC9")), "nc.pdf": PDF})
    graph = FakeGraph(
        {
            "m1": ("2026-10-23T10:00:00Z", [("factura.pdf", "application/pdf", PDF)]),
            "m2": ("2026-10-23T10:01:00Z", [zip_att(make_zip({"solo.pdf": PDF}))]),
            "m3": ("2026-10-23T10:02:00Z", [zip_att(make_zip({"f.xml": attached_document(document_xml(number="FE2"))}))]),
            "m4": ("2026-10-23T10:03:00Z", [zip_att(nota)]),
        }
    )
    run_mailbox(db, graph, MB)
    assert [db.log[m]["status"] for m in ("m1", "m2", "m3", "m4")] == ["sin_zip", "sin_zip", "revisar", "ok"]
    assert [i["invoice_number"] for i in db.invoices.values()] == ["NC9"]


def test_proveedor_nuevo_entra_sin_aprobadores():
    db = FakeDb()
    graph = FakeGraph({"m1": ("2026-10-23T10:00:00Z", [zip_att(invoice_zip(nit="800111222", number="X1"))])})
    run_mailbox(db, graph, MB)
    (inv,) = db.invoices.values()
    assert inv["supplier_nit"] == "800111222"
    assert inv["approval_mode"] == "parallel" and inv["required_approvals"] == 1
    assert db.approvals == []


def test_falla_se_reintenta_y_el_punto_de_partida_espera():
    db = FakeDb()
    graph = FakeGraph(
        {
            "m1": ("2026-10-23T10:00:00Z", [zip_att(invoice_zip(number="FE1"))]),
            "m2": ("2026-10-23T10:01:00Z", [zip_att(invoice_zip(number="FE2"))]),
            "m3": ("2026-10-23T10:02:00Z", [zip_att(invoice_zip(number="FE3"))]),
        }
    )
    graph.fail_download = {"m2"}
    run_mailbox(db, graph, MB)
    assert db.log["m2"]["status"] == "error"
    # Avanza hasta m1 y se queda: m2 se vuelve a mirar en la siguiente vuelta.
    assert db.state[MB["mailbox"]]["last_received_at"] == "2026-10-23T10:00:00+00:00"
    assert sorted(i["invoice_number"] for i in db.invoices.values()) == ["FE1", "FE3"]

    graph.fail_download = set()
    mb = {**MB, "last_received_at": db.state[MB["mailbox"]]["last_received_at"]}
    run_mailbox(db, graph, mb)
    assert db.log["m2"]["status"] == "ok" and db.log["m2"]["attempts"] == 2
    assert db.state[MB["mailbox"]]["last_received_at"] == "2026-10-23T10:02:00+00:00"


def test_si_fallan_las_aprobaciones_no_queda_factura_a_medias():
    db = FakeDb()
    db.fail_approvals = True
    graph = FakeGraph({"m1": ("2026-10-23T10:00:00Z", [zip_att(invoice_zip(number="FE1"))])})
    run_mailbox(db, graph, MB)
    assert db.invoices == {}
    assert db.log["m1"]["status"] == "error"


def test_error_agotado_deja_avanzar():
    db = FakeDb()
    graph = FakeGraph({"m1": ("2026-10-23T10:00:00Z", [zip_att(invoice_zip(number="FE1"))])})
    graph.fail_download = {"m1"}
    mb = dict(MB)
    for _ in range(5):
        run_mailbox(db, graph, mb)
    assert db.log["m1"]["attempts"] == 5
    assert db.state[MB["mailbox"]]["last_received_at"] == MB["last_received_at"]
    # Sexta vuelta: ya no se reintenta y el punto de partida sigue de largo.
    run_mailbox(db, graph, mb)
    assert db.log["m1"]["attempts"] == 5
    assert db.state[MB["mailbox"]]["last_received_at"] == "2026-10-23T10:00:00+00:00"


def test_since_no_mueve_el_punto_de_partida():
    db = FakeDb()
    graph = FakeGraph({"m1": ("2026-10-23T10:00:00Z", [zip_att(invoice_zip(number="FE1"))])})
    run_mailbox(db, graph, MB, persist=False)
    assert MB["mailbox"] not in db.state
