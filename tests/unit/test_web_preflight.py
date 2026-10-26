import io
from email.message import EmailMessage

from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

from shipping_billing.web.preflight import requires_local_text, requires_ocr_delivery


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


def test_native_pdf_in_email_requires_designated_ocr_but_not_local_text():
    buffer = io.BytesIO()
    pdf = Canvas(buffer)
    pdf.drawString(20, 20, "Native invoice USD 40")
    pdf.save()
    mail = EmailMessage()
    mail.set_content("See the attached invoice")
    mail.add_attachment(buffer.getvalue(), maintype="application", subtype="octet-stream", filename="data.bin")
    files = [("message.eml", mail.as_bytes())]
    assert requires_ocr_delivery(files) is True
    assert requires_local_text(files) is False
    outer = EmailMessage()
    outer.set_content("Forwarded material")
    outer.add_attachment(mail)
    assert requires_ocr_delivery([("forward.eml", outer.as_bytes())]) is True


def test_plain_email_and_text_attachment_do_not_require_ocr_delivery():
    mail = EmailMessage()
    mail.set_content("Handling charge USD 50")
    mail.add_attachment(b"supporting plain text", maintype="text", subtype="plain", filename="notes.txt")
    assert requires_ocr_delivery([("plain.eml", mail.as_bytes())]) is False
