from pathlib import Path

import pytest

from dian import DianZipError, canonical_nit, parse_dian_xml, read_dian_zip
from helpers import PDF, attached_document, document_xml, invoice_zip, make_zip


def test_factura_dentro_del_contenedor():
    z = read_dian_zip(invoice_zip())
    inv = z.invoice
    assert inv.kind == "Invoice"
    assert inv.invoice_number == "FE100"
    assert inv.supplier_nit == "901306960"
    # Razón social, no el nombre comercial.
    assert inv.supplier_name == "PROVEEDOR DE PRUEBA SAS"
    assert inv.total_amount == 119000.0
    assert inv.issue_date == "2026-10-07"
    # Sin DueDate se toma el del medio de pago.
    assert inv.due_date == "2026-11-06"
    assert z.pdf == PDF


def test_due_date_tiene_prioridad_y_se_descarta_si_es_anterior():
    assert read_dian_zip(invoice_zip(due="2026-10-20")).invoice.due_date == "2026-10-20"
    assert read_dian_zip(invoice_zip(payment_due="2026-10-01")).invoice.due_date is None


def test_nit_sin_digito_de_verificacion():
    assert read_dian_zip(invoice_zip(nit="901.306.960-1")).invoice.supplier_nit == "901306960"
    assert canonical_nit("805010305-4") == "805010305"
    # Una identificación extranjera con letras no se toca.
    assert canonical_nit("ABC123") == "ABC123"


def test_nota_credito_y_debito_se_leen():
    nc = read_dian_zip(make_zip({"nc.xml": attached_document(document_xml(root="CreditNote", number="NC7")), "nc.pdf": PDF}))
    assert (nc.invoice.kind, nc.invoice.invoice_number) == ("CreditNote", "NC7")
    nd = parse_dian_xml(document_xml(root="DebitNote", number="ND3", total_block="RequestedMonetaryTotal", total="5000"))
    assert (nd.kind, nd.invoice_number, nd.total_amount) == ("DebitNote", "ND3", 5000.0)


def test_factura_suelta_y_latin1():
    xml = document_xml(name="PANADERÍA ÑAPA").replace('encoding="UTF-8"', 'encoding="ISO-8859-1"')
    inv = read_dian_zip(make_zip({"f.xml": xml.encode("latin-1"), "f.pdf": PDF})).invoice
    assert inv.supplier_name == "PANADERÍA ÑAPA"


def test_zip_sin_xml_y_basura():
    with pytest.raises(DianZipError, match="no trae XML"):
        read_dian_zip(make_zip({"factura.pdf": PDF}))
    with pytest.raises(DianZipError, match="No se pudo leer"):
        read_dian_zip(make_zip({"otra.xml": "<html>no es la DIAN</html>", "f.pdf": PDF}))
    with pytest.raises(DianZipError, match="abrir"):
        read_dian_zip(b"esto no es un zip")


def test_xml_malicioso_no_se_procesa():
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><Invoice>&b;</Invoice>'
    assert parse_dian_xml(bomb) is None


def test_sin_pdf_y_pdf_emparejado():
    z = read_dian_zip(make_zip({"f.xml": attached_document(document_xml())}))
    assert z.pdf is None and z.pdf_problem == "El ZIP no trae PDF"
    z = read_dian_zip(
        make_zip({"aa.pdf": b"%PDF otro", "factura9.xml": attached_document(document_xml()), "factura9.pdf": PDF, "__MACOSX/._factura9.pdf": b"x"})
    )
    assert z.pdf == PDF


# Los ZIP reales de ~/Downloads/Facturas-Dian (solo en el computador de quien
# los tenga) contra lo que n8n guardó en la base para esas mismas facturas.
REALES = Path.home() / "Downloads" / "Facturas-Dian"
ESPERADO = {
    "GOFE809.zip": ("GOFE809", "901306960", 3094000.0, "2026-10-07", "2026-10-15"),
    "ad09006491140002600045188.xml.zip": ("CALI64641", "900649114", 26400.0, "2026-10-09", "2026-10-09"),
    "f2f0bc16-a835-40b6-95f8-aac871468d7d.zip": ("FVP11687", "901354972", 435897.0, "2026-10-09", "2026-11-08"),
    "z0016273179016260000440b.zip": ("FE17419", "16273179", 2537080.0, "2026-10-03", "2026-11-02"),
    "z09006856120004266620680.zip": ("FEMO3112", "900685612", 17800.0, "2026-10-06", "2026-10-06"),
    "z09011454980162600001b21.zip": ("VA206945", "901145498", 309400.0, "2026-09-18", "2026-10-18"),
}


@pytest.mark.parametrize("name", sorted(ESPERADO))
def test_igual_que_n8n(name):
    path = REALES / name
    if not path.exists():
        pytest.skip("no están los ZIP reales en este computador")
    inv = read_dian_zip(path.read_bytes()).invoice
    assert (inv.invoice_number, inv.supplier_nit, inv.total_amount, inv.issue_date, inv.due_date) == ESPERADO[name]
