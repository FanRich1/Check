"""Bounded DOC conversion with a dedicated hidden Word process on Windows."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from shipping_billing.errors import BillingError


def convert_doc(data: bytes, workdir: Path, timeout=60) -> bytes:
    if os.name != "nt":
        raise BillingError(
            "DOC_CONVERTER_UNAVAILABLE", "DOC conversion requires Windows with Microsoft Word installed."
        )
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="word-", dir=workdir.resolve()) as name:
        folder = Path(name)
        original, target = folder / "input.doc", folder / "output.docx"
        original.write_bytes(data)
        pid_file = folder / "word.pid"
        try:
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "shipping_billing.parsers.word_worker",
                    str(original),
                    str(target),
                    str(pid_file),
                    "16",
                ],
                capture_output=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            # Only this newly-created instance is recorded by the converter, never a user's existing Word.
            if pid_file.exists() and pid_file.read_text().strip().isdigit():
                subprocess.run(
                    ["taskkill", "/PID", pid_file.read_text().strip(), "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            raise BillingError("DOC_CONVERSION_TIMEOUT", "DOC conversion exceeded its time limit.") from exc
        if run.returncode or not target.is_file():
            raise BillingError(
                "DOC_CONVERSION_FAILED", "DOC could not be converted by the configured Word installation."
            )
        return target.read_bytes()
