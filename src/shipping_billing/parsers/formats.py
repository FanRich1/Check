"""Identify actual formats without trusting filenames or MIME declarations."""

import io
import re
import zipfile

import olefile

from shipping_billing.errors import BillingError


def inspect_zip(data, limits):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        items = archive.infolist()
        if len(items) > 10000 or sum(x.file_size for x in items) > limits.max_zip_bytes:
            raise BillingError("ARCHIVE_LIMIT", "Office archive exceeds expanded-size or entry limit.")
        if any(x.flag_bits & 1 for x in items):
            raise BillingError("ENCRYPTED_FILE", "Encrypted Office archives require manual handling.")
        return {x.filename for x in items}


def detect(data, limits):
    if data[:1024].lstrip().startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return "tiff"
    if data.startswith(b"PK"):
        names = inspect_zip(data, limits)
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "unknown"
    if data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        with olefile.OleFileIO(io.BytesIO(data)) as archive:
            if archive.exists("EncryptedPackage"):
                raise BillingError("ENCRYPTED_FILE", "Encrypted Office documents require manual handling.")
            if archive.exists("WordDocument"):
                return "doc"
            if archive.exists("Workbook") or archive.exists("Book"):
                return "xls"
    header = re.split(rb"\r?\n\r?\n", data[:65536], maxsplit=1)[0]
    fields = re.findall(rb"(?mi)^([a-z][a-z0-9-]*):", header)
    if (
        len(
            set(x.lower() for x in fields)
            & {b"from", b"to", b"subject", b"mime-version", b"date", b"content-type"}
        )
        >= 2
    ):
        return "eml"
    return "unknown"
