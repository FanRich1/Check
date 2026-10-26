import io
from email.message import EmailMessage

from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

from shipping_billing.web.preflight import requires_local_text


def test_scanned_pages_require_text_but_native_pdf_does_not():
    scan = PdfWriter()
    scan.add_blank_page(width=100, height=100)
    scanned = io.BytesIO()
    scan.write(scanned)
    text = io.BytesIO()
    pdf = Canvas(text)
    pdf.drawString(20, 20, "Invoice USD 40")
    pdf.save()
    assert requires_local_text([("native.pdf", text.getvalue())]) is False
    assert requires_local_text([("misnamed.docx", scanned.getvalue())]) is True
    mixed = PdfWriter()
    mixed.add_page(PdfReader(io.BytesIO(text.getvalue())).pages[0])
    mixed.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    mixed.write(buffer)
    assert requires_local_text([("mixed.pdf", buffer.getvalue())]) is True


def test_inline_image_is_detected_even_when_filename_has_no_image_extension():
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, format="PNG")
    message = EmailMessage()
    message["Subject"] = "Documents"
    message.set_content("Please read the attached material")
    message.add_attachment(image.getvalue(), maintype="application", subtype="octet-stream", filename="data.bin")
    assert requires_local_text([("message.eml", message.as_bytes())]) is True
    assert requires_local_text([("bill.png", b"broken image")]) is False
