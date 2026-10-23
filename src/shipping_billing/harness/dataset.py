"""Frozen input verification and release fingerprints. No business labels reach model clients."""

import hashlib
import re
from pathlib import Path

from shipping_billing.errors import BillingError
from shipping_billing.extraction.engine import digest
from shipping_billing.io import load_json, write_json


def inside(root, relative):
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise BillingError("DATASET_PATH_ESCAPE", "Dataset paths must stay inside their root.")
    return target


def load_split(root, split):
    manifest = load_json(root / ("manifest-" + split + ".json"))
    ids = [c["case_id"] for c in manifest["cases"]]
    if (
        len(ids) != len(set(ids))
        or any(
            not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", cid)
            for cid in ids
        )
        or any(c["split"] != split for c in manifest["cases"])
    ):
        raise BillingError("INVALID_DATASET_SPLIT", "Case IDs and split assignments must be unambiguous.")
    for file in manifest["files"]:
        path = inside(root, file["path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != file["sha256"]:
            raise BillingError("DATASET_HASH_MISMATCH", "Frozen fixture bytes changed.")
    actual = {
        str(p.relative_to(root)).replace("\\", "/")
        for c in manifest["cases"]
        for p in inside(root, c["path"]).rglob("*")
        if p.is_file()
    }
    if actual != {f["path"] for f in manifest["files"]}:
        raise BillingError(
            "UNFROZEN_DATASET_FILE", "Every case input and label must be in the frozen file inventory."
        )
    return manifest


def code_fingerprint():
    root = Path(__file__).resolve().parents[1]
    return digest(
        [
            (str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*.py"))
        ]
    )


def freeze_release(root, output):
    manifests = [load_split(root, s) for s in ("development", "validation", "holdout")]
    families = {}
    for manifest in manifests:
        for case in manifest["cases"]:
            families.setdefault(case["family_id"], set()).add(case["split"])
    if any(len(s) != 1 for s in families.values()):
        raise BillingError("DATASET_FAMILY_LEAK", "Source/template families cannot cross splits.")
    if [len(m["cases"]) for m in manifests] != [30, 15, 15]:
        raise BillingError("INCOMPLETE_DATASET", "Release requires 30/15/15 frozen business scenarios.")
    if output.exists():
        raise BillingError("RELEASE_ALREADY_EXISTS", "Do not overwrite a recorded evaluation release.")
    release = {
        "code_sha256": code_fingerprint(),
        "datasets": {m["split"]: digest(m) for m in manifests},
        "annotation_review": "assistant_authored_not_independently_business_signed",
        "policy": "Holdout is evaluated after application/scorer freeze; feedback consumes that holdout.",
    }
    write_json(output, release)
    return release


def verify_release(root, path, split):
    release = load_json(path)
    if release["code_sha256"] != code_fingerprint() or release["datasets"][split] != digest(
        load_split(root, split)
    ):
        raise BillingError(
            "EVALUATION_RELEASE_MISMATCH", "Code or frozen dataset changed after the release marker."
        )
    return release
