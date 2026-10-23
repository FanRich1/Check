"""MIME traversal with one body alternative and preserved attachment identities."""

import base64
import quopri
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from shipping_billing.models import Locator


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.hidden += 1
        if not self.hidden:
            if tag in ("p", "div", "br", "tr", "li", "table"):
                self.parts.append("\n")
            elif tag in ("td", "th"):
                self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ("p", "div", "tr", "li", "table"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def decode_text(part, source):
    raw = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="strict")
    except (LookupError, UnicodeDecodeError):
        source.issue(
            "EMAIL_ENCODING_UNCERTAIN", "Text encoding was invalid; replacement characters preserved."
        )
        return raw.decode("utf-8", errors="replace")


def parse_email(data, source, visit_child):
    message = BytesParser(policy=policy.default).parsebytes(data)
    source.metadata["headers"] = {
        key: [str(v) for v in message.get_all(key, [])]
        for key in ("Subject", "From", "To", "Cc", "Date", "Message-ID")
    }
    source.metadata["addresses"] = {
        key: [{"name": name, "address": address} for name, address in getaddresses(message.get_all(key, []))]
        for key in ("From", "To", "Cc")
    }
    if message.get("Date"):
        try:
            source.metadata["date_iso"] = parsedate_to_datetime(str(message["Date"])).isoformat()
        except (ValueError, TypeError, OverflowError):
            source.issue(
                "EMAIL_DATE_INVALID", "Email date could not be normalized; original header retained."
            )
    for key, values in source.metadata["headers"].items():
        for i, value in enumerate(values):
            source.fragment(value, Locator(kind="email_header", section=f"{key}[{i + 1}]"), "email_header")
    candidates = []
    alternatives = {}
    visited_parts = 0

    def walk(part, part_id, groups=()):
        nonlocal visited_parts
        visited_parts += 1
        if part_id.count(".") > 30 or visited_parts > 1000:
            source.issue("EMAIL_MIME_LIMIT", "Email exceeds MIME nesting or part-count limit.", error=True)
            return
        if part.defects:
            source.issue(
                "EMAIL_MIME_DEFECT", "MIME parser reported a malformed part; content retained where possible."
            )
        filename = part.get_filename()
        ctype = part.get_content_type()
        disposition = part.get_content_disposition()
        if ctype == "message/rfc822":
            transfer = str(part.get("Content-Transfer-Encoding", "")).lower()
            if transfer in ("base64", "quoted-printable"):
                raw_body = part.as_bytes(policy=policy.SMTP).partition(b"\r\n\r\n")[2]
                try:
                    decoded = (
                        base64.b64decode(b"".join(raw_body.split()), validate=True)
                        if transfer == "base64"
                        else quopri.decodestring(raw_body)
                    )
                except ValueError:
                    source.issue("EMAIL_NESTED_INVALID", "Nested message transfer encoding was invalid.")
                    return
                visit_child(
                    decoded,
                    filename or f"message-{part_id}.eml",
                    ctype,
                    f"part-{part_id}-1",
                    {"mime_part": part_id, "disposition": disposition, "transfer_encoding": transfer},
                )
                return
            payload = part.get_payload()
            nested = payload if isinstance(payload, list) else []
            if not nested:
                source.issue("EMAIL_NESTED_INVALID", "Nested message could not be decoded.")
            for i, child in enumerate(nested):
                visit_child(
                    child.as_bytes(policy=policy.SMTP),
                    filename or f"message-{part_id}-{i + 1}.eml",
                    ctype,
                    f"part-{part_id}-{i + 1}",
                    {"mime_part": part_id, "disposition": disposition, "serialization": "email.policy.SMTP"},
                )
            return
        if part.is_multipart():
            group = part_id if ctype == "multipart/alternative" else None
            for i, child in enumerate(part.iter_parts(), 1):
                if visited_parts >= 1000:
                    source.issue(
                        "EMAIL_MIME_LIMIT",
                        "Email part-count limit reached; remainder unexpanded.",
                        error=True,
                    )
                    break
                walk(child, f"{part_id}.{i}", groups + ((group,) if group else ()))
            return
        if ctype in ("text/plain", "text/html") and not filename and disposition != "attachment":
            text = decode_text(part, source)
            if ctype == "text/html":
                parser = HTMLText()
                parser.feed(text)
                text = "".join(parser.parts)
            candidate = {"part_id": part_id, "content_type": ctype, "text": text, "groups": groups}
            candidates.append(candidate)
            for group in groups:
                alternatives.setdefault(group, []).append(candidate)
            return
        raw = part.get_payload(decode=True)
        if raw is None:
            source.issue("EMAIL_ATTACHMENT_UNDECODABLE", "Attachment could not be decoded.")
            return
        visit_child(
            raw,
            filename or f"part-{part_id}",
            ctype,
            f"part-{part_id}",
            {
                "mime_part": part_id,
                "disposition": disposition,
                "content_id": part.get("Content-ID"),
                "inline": disposition == "inline" or bool(part.get("Content-ID")),
                "classification": "unclassified_attachment",
            },
        )

    walk(message, "1")
    selected = {}
    for group, rows in alternatives.items():
        selected[group] = next(
            (r for r in rows if r["content_type"] == "text/plain" and r["text"].strip()),
            next((r for r in rows if r["text"].strip()), rows[0]),
        )
    source.metadata["body_alternatives"] = []
    for row in candidates:
        use = all(selected[g] is row for g in row["groups"])
        source.metadata["body_alternatives"].append(
            {k: v for k, v in row.items() if k != "groups"} | {"selected": use}
        )
        if use:
            source.fragment(
                row["text"],
                Locator(kind="email_body", section=f"MIME {row['part_id']}"),
                "email_html" if row["content_type"] == "text/html" else "email_plain",
            )
