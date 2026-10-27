"""ZIP de la DIAN de mentira para las pruebas (no se suben facturas reales al repo)."""

import io
import zipfile

NS = (
    'xmlns="urn:oasis:names:specification:ubl:schema:xsd:{root}-2" '
    'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
    'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"'
)


def document_xml(
    root: str = "Invoice",
    number: str = "FE100",
    nit: str = "901306960",
    name: str = "PROVEEDOR DE PRUEBA SAS",
    total: str = "119000.00",
    issue: str = "2026-10-07",
    due: str | None = None,
    payment_due: str | None = "2026-11-06",
    total_block: str = "LegalMonetaryTotal",
) -> str:
    due_xml = f"<cbc:DueDate>{due}</cbc:DueDate>" if due else ""
    means = (
        f"<cac:PaymentMeans><cbc:ID>1</cbc:ID><cbc:PaymentDueDate>{payment_due}</cbc:PaymentDueDate></cac:PaymentMeans>"
        if payment_due
        else ""
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><{root} {NS.format(root=root)}>'
        f"<cbc:ID>{number}</cbc:ID><cbc:IssueDate>{issue}</cbc:IssueDate>{due_xml}"
        f"<cac:AccountingSupplierParty><cac:Party>"
        f"<cac:PartyName><cbc:Name>NOMBRE COMERCIAL</cbc:Name></cac:PartyName>"
        f'<cac:PartyTaxScheme><cbc:RegistrationName>{name}</cbc:RegistrationName><cbc:CompanyID schemeID="1">{nit}</cbc:CompanyID></cac:PartyTaxScheme>'
        f"</cac:Party></cac:AccountingSupplierParty>"
        f"{means}"
        f'<cac:{total_block}><cbc:PayableAmount currencyID="COP">{total}</cbc:PayableAmount></cac:{total_block}>'
        f"</{root}>"
    )


def attached_document(inner: str) -> str:
    """El contenedor que manda la DIAN: la factura va como texto en un CDATA."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<AttachedDocument xmlns="urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2" '
        'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
        'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
        "<cbc:ID>CONTENEDOR-1</cbc:ID>"
        f"<cac:Attachment><cac:ExternalReference><cbc:Description><![CDATA[{inner}]]></cbc:Description></cac:ExternalReference></cac:Attachment>"
        "<cac:ParentDocumentLineReference><cac:DocumentReference><cac:Attachment><cac:ExternalReference>"
        "<cbc:Description><![CDATA[<?xml version=\"1.0\"?><ApplicationResponse>respuesta DIAN</ApplicationResponse>]]></cbc:Description>"
        "</cac:ExternalReference></cac:Attachment></cac:DocumentReference></cac:ParentDocumentLineReference>"
        "</AttachedDocument>"
    )


PDF = b"%PDF-1.4 factura de prueba"


def make_zip(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content.encode("utf-8") if isinstance(content, str) else content)
    return buf.getvalue()


def invoice_zip(**kwargs) -> bytes:
    return make_zip({"ad0901306960.xml": attached_document(document_xml(**kwargs)), "ad0901306960.pdf": PDF})
