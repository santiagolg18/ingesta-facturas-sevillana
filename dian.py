"""
Lector del ZIP de la DIAN (PDF + XML de la factura electrónica).

Es la misma lógica de lib/invoices/dian-zip.ts en la app (el de «+ Nueva
factura»), para que una factura dé los mismos datos entre por correo o a mano.
Si se corrige algo allá, corregirlo aquí también. Diferencias:
  - Las notas crédito y débito SÍ se leen: entran como factura, igual que con
    n8n. El formulario manual las rechaza.
  - El XML se lee con defusedxml, que rechaza XML malicioso (entidades
    externas, "billion laughs"): el ZIP llega de cualquier remitente.

El XML es un "contenedor" (AttachedDocument) que lleva la factura adentro como
texto, junto con la validación de la DIAN. Los nombres de los archivos no
siguen un patrón, así que todo se reconoce por el contenido.
"""

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import Element, ParseError

CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NAMESPACES = {"cbc": CBC, "cac": CAC}

MAX_XML_BYTES = 5 * 1024 * 1024
# El mismo tope de la factura que se adjunta a mano (límite del bucket).
MAX_PDF_BYTES = 10 * 1024 * 1024

DOCUMENT_ROOTS = ("Invoice", "CreditNote", "DebitNote")


@dataclass
class DianInvoice:
    kind: str  # Invoice | CreditNote | DebitNote
    invoice_number: str | None
    # Sin dígito de verificación, como lo guarda la base.
    supplier_nit: str | None
    # Razón social, no el nombre comercial.
    supplier_name: str | None
    # None si no se pudo leer.
    total_amount: float | None
    currency: str
    issue_date: str | None  # AAAA-MM-DD
    due_date: str | None


@dataclass
class DianZip:
    invoice: DianInvoice
    pdf_name: str | None
    pdf: bytes | None
    # Por qué no hay PDF, si no lo hay.
    pdf_problem: str | None


class DianZipError(Exception):
    """El ZIP no trae un XML de la DIAN que se pueda leer."""


# --- NIT (misma regla que lib/suppliers/nit.ts y normalize_nit()) -----------

def normalize_nit(raw: str) -> str | None:
    digits = re.sub(r"[^0-9]", "", re.sub(r"-\s*[0-9kK]\s*$", "", raw))
    return digits or None


def canonical_nit(raw: str) -> str:
    """Sin dígito de verificación si es colombiano; una identificación extranjera con letras queda igual."""
    text = raw.strip()
    looks_like_nit = bool(re.fullmatch(r"[\d.\-\s]+[kK]?", text)) and bool(re.search(r"\d", text))
    return (normalize_nit(text) if looks_like_nit else None) or text


# --- XML -------------------------------------------------------------------

def _parse_xml(source: str | bytes) -> Element | None:
    if isinstance(source, str):
        # La declaración <?xml ?> solo puede ir al puro inicio: el texto
        # embebido suele traer espacios o un BOM delante. Se quita porque el
        # texto ya está decodificado y su "encoding=" ya no aplica.
        source = source.lstrip("﻿").strip()
        source = re.sub(r"^<\?xml[^>]*\?>", "", source).strip()
        if not source.startswith("<"):
            return None
    try:
        return SafeET.fromstring(source)
    except (ParseError, DefusedXmlException, ValueError):
        return None


def _local(el: Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _children(parent: Element | None, step: str) -> list[Element]:
    # Por espacio de nombres + nombre: el prefijo ("cbc:", "cac:") es una
    # convención que cada proveedor tecnológico puede cambiar.
    if parent is None:
        return []
    prefix, name = step.split(":")
    return list(parent.findall(f"{{{NAMESPACES[prefix]}}}{name}"))


def _find(root: Element | None, path: str) -> Element | None:
    current = root
    for step in path.split("/"):
        found = _children(current, step)
        if not found:
            return None
        current = found[0]
    return current


def _text(root: Element | None, path: str) -> str | None:
    el = _find(root, path)
    if el is None:
        return None
    value = re.sub(r"\s+", " ", "".join(el.itertext())).strip()
    return value or None


def _iso_date(raw: str | None) -> str | None:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw or "")
    if not match:
        return None
    try:
        # Descarta fechas que no existen ("2026-02-31").
        return date(int(match[1]), int(match[2]), int(match[3])).isoformat()
    except ValueError:
        return None


def _read_party(doc: Element, role: str) -> tuple[str | None, str | None]:
    party = _find(doc, f"{role}/cac:Party")
    tax = _find(party, "cac:PartyTaxScheme")
    legal = _find(party, "cac:PartyLegalEntity")
    company_id = _text(tax, "cbc:CompanyID") or _text(legal, "cbc:CompanyID")
    name = (
        _text(tax, "cbc:RegistrationName")
        or _text(legal, "cbc:RegistrationName")
        or _text(party, "cac:PartyName/cbc:Name")
    )
    return (canonical_nit(company_id) if company_id else None), name


def _read_document(doc: Element, kind: str) -> DianInvoice:
    nit, name = _read_party(doc, "cac:AccountingSupplierParty")

    # La nota débito pone el total en RequestedMonetaryTotal.
    payable = _find(doc, "cac:LegalMonetaryTotal/cbc:PayableAmount")
    if payable is None:
        payable = _find(doc, "cac:RequestedMonetaryTotal/cbc:PayableAmount")
    currency = (
        (payable.get("currencyID") if payable is not None else None)
        or _text(doc, "cbc:DocumentCurrencyCode")
        or "COP"
    ).upper()
    try:
        amount = float((payable.text or "").strip()) if payable is not None else None
    except ValueError:
        amount = None

    issue_date = _iso_date(_text(doc, "cbc:IssueDate"))
    # Varios facturadores no llenan DueDate y dejan el vencimiento en el
    # medio de pago.
    due_date = _iso_date(_text(doc, "cbc:DueDate"))
    for means in _children(doc, "cac:PaymentMeans"):
        due_date = due_date or _iso_date(_text(means, "cbc:PaymentDueDate"))
    # Un vencimiento anterior a la emisión es un dato mal puesto.
    if due_date and issue_date and due_date < issue_date:
        due_date = None

    return DianInvoice(
        kind=kind,
        invoice_number=_text(doc, "cbc:ID"),
        supplier_nit=nit,
        supplier_name=name,
        total_amount=amount,
        currency=currency,
        issue_date=issue_date,
        due_date=due_date,
    )


def _classify(root: Element) -> DianInvoice | None:
    kind = _local(root)
    return _read_document(root, kind) if kind in DOCUMENT_ROOTS else None


def _decode_xml(data: bytes) -> str:
    # La declaración es ASCII, así que se puede leer antes de saber la
    # codificación del resto. Se decodifica aquí (y no en expat) porque expat
    # no conoce codificaciones como windows-1252.
    prolog = data[:200].decode("latin-1")
    declared = re.search(r"""encoding\s*=\s*["']([\w.-]+)["']""", prolog, re.I)
    try:
        return data.decode(declared[1] if declared else "utf-8")
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", errors="replace")


def parse_dian_xml(source: str | bytes) -> DianInvoice | None:
    """Lee el XML de un ZIP de la DIAN: el contenedor o el documento suelto."""
    if isinstance(source, bytes):
        source = _decode_xml(source)
    root = _parse_xml(source)
    if root is None:
        return None

    direct = _classify(root)
    if direct:
        return direct
    if _local(root) != "AttachedDocument":
        return None

    # El contenedor lleva dos documentos embebidos como texto: la factura (o
    # nota) y la respuesta de la DIAN. Se abre cada uno y se toma el documento.
    for description in root.iter(f"{{{CBC}}}Description"):
        inner = _parse_xml("".join(description.itertext()))
        result = _classify(inner) if inner is not None else None
        if result:
            return result
    return None


# --- ZIP -------------------------------------------------------------------

def _base_name(entry_name: str) -> str:
    return entry_name.rsplit("/", 1)[-1]


def _entry_kind(entry_name: str) -> str | None:
    base = _base_name(entry_name)
    # macOS mete en los ZIP una copia fantasma de cada archivo ("._factura.xml").
    if not base or base.startswith("._") or entry_name.startswith("__MACOSX/"):
        return None
    lower = base.lower()
    if lower.endswith(".xml"):
        return "xml"
    if lower.endswith(".pdf"):
        return "pdf"
    return None


def _stem(entry_name: str) -> str:
    # "factura.xml" y "factura.pdf" → "factura", para emparejarlos.
    return re.sub(r"\.[^.]+$", "", _base_name(entry_name)).lower()


def read_dian_zip(data: bytes) -> DianZip:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DianZipError("No se pudo abrir el ZIP") from exc

    with archive:
        # Solo se descomprime lo que se va a usar, y con tope: el tamaño
        # declarado es lo que se reserva en memoria.
        xml_entries = [i for i in archive.infolist() if _entry_kind(i.filename) == "xml"]
        pdf_entries = [i for i in archive.infolist() if _entry_kind(i.filename) == "pdf"]
        pdf_too_big = any(i.file_size > MAX_PDF_BYTES for i in pdf_entries)
        pdf_entries = [i for i in pdf_entries if i.file_size <= MAX_PDF_BYTES]

        invoice: DianInvoice | None = None
        invoice_entry: str | None = None
        for info in xml_entries:
            if info.file_size > MAX_XML_BYTES:
                continue
            try:
                result = parse_dian_xml(archive.read(info))
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
                result = None
            if result:
                invoice, invoice_entry = result, info.filename
                break

        if invoice is None:
            raise DianZipError(
                "El ZIP no trae XML" if not xml_entries else "No se pudo leer el XML de la DIAN"
            )

        # Si hay varios PDF, el que se llama como el XML de la factura.
        pdf_info = next(
            (i for i in pdf_entries if _stem(i.filename) == _stem(invoice_entry)),
            pdf_entries[0] if pdf_entries else None,
        )
        pdf = archive.read(pdf_info) if pdf_info else None

    return DianZip(
        invoice=invoice,
        pdf_name=_base_name(pdf_info.filename) if pdf_info else None,
        pdf=pdf,
        pdf_problem=None
        if pdf_info
        else ("El PDF del ZIP supera 10 MB" if pdf_too_big else "El ZIP no trae PDF"),
    )
