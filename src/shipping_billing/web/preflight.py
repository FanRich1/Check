"""Read-only checks for images and scanned pages before queuing remote work."""

import io
from email import policy
from email.parser import BytesParser

from pypdf import PdfReader

from shipping_billing.parsers.contracts import ParseLimits
from shipping_billing.parsers.formats import detect


def requires_local_text(files):
    """Recognize real bytes, including email attachments; leave corrupt files to parsing."""
    budget = [0, 0]
    limits = ParseLimits()

    def inspect(content, depth=0):
        budget[0] += 1
        budget[1] += len(content)
        if (depth > limits.max_depth or budget[0] > limits.max_sources
                or budget[1] > limits.max_total_bytes or len(content) > limits.max_file_bytes):
            return False
        try:
            kind = detect(content, limits)
            if kind in {"png", "jpg", "tiff"}:
                return True
            if kind == "pdf":
                pdf = PdfReader(io.BytesIO(content), strict=False)
                if pdf.is_encrypted or len(pdf.pages) > limits.max_pages:
                    return False
                return any(not (page.extract_text() or "").strip() for page in pdf.pages)
            if kind == "eml":
                message = BytesParser(policy=policy.default).parsebytes(content)
                for index, part in enumerate(message.walk()):
                    if index >= 1000:
                        break
                    if part.is_multipart():
                        continue
                    if part.get_content_type() in {"text/plain", "text/html"} and not part.get_filename():
                        continue
                    raw = part.get_payload(decode=True)
                    if raw and inspect(raw, depth + 1):
                        return True
        except Exception:
            # Invalid/encrypted/budget-limited files still receive normal source-level errors.
            return False
        return False

    return any(inspect(content) for _, content in files)
