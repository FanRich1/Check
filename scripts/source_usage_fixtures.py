"""Synthetic source-purpose scenarios, never a real OCR accuracy benchmark."""

import io
from email.message import EmailMessage

from PIL import Image
from reportlab.pdfgen.canvas import Canvas

from scripts.stage7_fixtures import AuthoredModel, content
from shipping_billing.errors import BillingError
from shipping_billing.pipeline.engine import run_pipeline


class DummyLocalOCR:
    metadata = {"mode": "test_injected"}

    def __call__(self, image):
        return []


def source_email(folder):
    folder.mkdir(parents=True, exist_ok=True)
    mail = EmailMessage()
    mail["From"] = "supplier@example.invalid"
    mail["To"] = "buyer@example.invalid"
    mail["Subject"] = "Authored source purpose acceptance"
    mail.set_content("\n".join(content()))
    image = io.BytesIO()
    Image.new("RGB", (200, 80), "cyan").save(image, format="PNG")
    mail.add_attachment(image.getvalue(), maintype="image", subtype="png", filename="banner.png")
    for index in range(2):
        buffer = io.BytesIO()
        pdf = Canvas(buffer, invariant=1)
        lines = ["BILL OF LADING", "SHIPPER: Sender Ltd", "CONSIGNEE: Receiver Ltd",
                 f"B/L NUMBER: BL123456{index}", "FREIGHT PREPAID", "Weight 10715.000 KGS"]
        for row, text in enumerate(lines):
            pdf.drawString(40, 780 - row * 22, text)
        pdf.save()
        mail.add_attachment(buffer.getvalue(), maintype="application", subtype="pdf", filename=f"transport-{index + 1}.pdf")
    path = folder / "sources.eml"
    path.write_bytes(mail.as_bytes())
    return path


def source_pipeline(paths, **kwargs):
    def ocr(content_bytes, sid, page, content_type):
        if content_type.startswith("image/"):
            raise BillingError("OCR_EMPTY_CONTENT", "Injected empty billing OCR on a non-billing image")
        return dict(mode="test_injected", network_calls=0, documents=[{"vendor_facts": {}}], text_blocks=[])
    return run_pipeline(paths, **{**kwargs, "mode": "test_injected", "client": AuthoredModel(),
                                  "ocr_provider": ocr, "local_ocr": DummyLocalOCR()})
