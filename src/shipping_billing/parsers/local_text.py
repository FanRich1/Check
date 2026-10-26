"""Optional local OCR evidence, explicitly separate from the required remote OCR."""

import hashlib
from importlib.metadata import version
from pathlib import Path

from shipping_billing.errors import BillingError


class LocalTextOCR:
    enhance_small_images = True

    def __init__(self):
        try:
            import rapidocr_onnxruntime
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise BillingError(
                "LOCAL_OCR_NOT_INSTALLED", "Install the local-ocr extra to enable supplementary recognition."
            ) from exc
        self.engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
        folder = Path(rapidocr_onnxruntime.__file__).parent
        self.metadata = {
            "engine": "rapidocr-onnxruntime",
            "version": version("rapidocr-onnxruntime"),
            "models": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*.onnx")},
            "remote_ocr_replacement": False,
        }

    def __call__(self, image):
        results, _ = self.engine(image)
        return [
            {"polygon": [[float(x), float(y)] for x, y in box], "text": text, "confidence": float(score)}
            for box, text, score in (results or [])
        ]
