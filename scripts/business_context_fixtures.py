"""Authored mail/PDF scenarios and injected services for business-context acceptance only."""

from email.message import EmailMessage

from reportlab.pdfgen.canvas import Canvas

from scripts.stage7_fixtures import AuthoredModel
from shipping_billing.pipeline.engine import run_pipeline


def business_email(folder, *, same_document=True, ambiguous_currency=True, conflict=False):
    folder.mkdir(parents=True, exist_ok=True)
    message = EmailMessage()
    message["From"] = "supplier@example.invalid"
    message["To"] = "buyer@example.invalid"
    message["Subject"] = "Authored business context acceptance"
    currency = "$" if ambiguous_currency else "USD"
    message.set_content("\n".join(["Type: Invoice", "Invoice: INV-E2E-001", "MBL: OOLU1234567",
                                    f"Handling Fee | {currency} | 40 | PP"]))
    for index in range(2 if conflict else 1):
        invoice = "INV-E2E-001" if same_document else "INV-E2E-999"
        lines = [f"Invoice: {invoice}", "MBL: OOLU1234567", "PAYER: Buyer Ltd" if not index else "PAYER: Other Ltd",
                 "PAYEE: Supplier Ltd", "Issuer: Supplier Ltd", "Bill to: Buyer Ltd",
                 "Currency: USD" if not index else "Currency: EUR"]
        path = folder / f"support-{index + 1}.pdf"
        canvas = Canvas(str(path), invariant=1)
        for row, text in enumerate(lines):
            canvas.drawString(40, 780 - 22 * row, text)
        canvas.save()
        message.add_attachment(path.read_bytes(), maintype="application", subtype="pdf", filename=path.name)
    path = folder / "business.eml"
    path.write_bytes(message.as_bytes())
    return path


def business_pipeline(paths, **kwargs):
    def ocr(*args):
        return dict(mode="test_injected", network_calls=0, documents=[],
                    text_blocks=["Supporting material; refer to native document text."])
    return run_pipeline(paths, **{**kwargs, "mode": "test_injected", "client": AuthoredModel(), "ocr_provider": ocr})
