"""Build and verify an allowlisted source ZIP, with exact hashes and no local credentials or inputs."""

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TOP = {"src", "scripts", "tests", "schemas", "docs", "tools"}
FILES = {"README.md", "pyproject.toml", "uv.lock", ".env.example", ".gitignore", ".gitattributes"}
KEY = re.compile(rb"sk-[A-Za-z0-9]{20,}")


def allowed(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return False
    if any(p in {"__pycache__", ".venv", ".git", ".pytest_cache", ".ruff_cache"} for p in path.parts):
        return False
    if path.name.startswith(".env") and name != ".env.example":
        return False
    if path.suffix.lower() in {".pyc", ".pyo", ".log", ".exe"}:
        return False
    return name in FILES or (len(path.parts) == 1 and path.suffix == ".cmd") or path.parts[0] in TOP


def verify(archive):
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive paths")
        manifest_name = "Check/release-manifest.json"
        manifest = json.loads(bundle.read(manifest_name))
        expected = {"Check/" + entry["path"]: entry for entry in manifest["files"]}
        if set(names) != {*expected, manifest_name}:
            raise ValueError("Archive inventory mismatch")
        for name, entry in expected.items():
            relative = name.removeprefix("Check/")
            content = bundle.read(name)
            if not name.startswith("Check/") or not allowed(relative):
                raise ValueError("Forbidden archive member: " + relative)
            if KEY.search(content):
                raise ValueError("Credential-like content in: " + relative)
            if len(content) != entry["bytes"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError("File hash mismatch: " + relative)
    return manifest


def source_files(root):
    listed = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
    )
    return sorted({p.decode("utf-8") for p in listed.split(b"\0") if p and allowed(p.decode("utf-8"))})


def compare_source(archive, root):
    """An internally valid old ZIP is still stale when compared with the selected source tree."""
    root = Path(root).resolve()
    manifest = verify(archive)
    expected = {entry["path"]: entry for entry in manifest["files"]}
    names = set(source_files(root))
    missing = sorted(names - expected.keys())
    extra = sorted(expected.keys() - names)
    changed = sorted(name for name in names & expected.keys()
                     if not (root / name).is_file()
                     or hashlib.sha256((root / name).read_bytes()).hexdigest() != expected[name]["sha256"])
    return dict(passed=not (missing or extra or changed), compared=len(names), missing=missing,
                extra=extra, changed=changed)


def build(output, version):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise ValueError("Invalid release version")
    names = source_files(ROOT)
    entries, contents = [], {}
    for name in names:
        if not allowed(name):
            continue
        source = ROOT / name
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT) or not source.is_file():
            raise ValueError("Unsafe or missing release file: " + name)
        content = source.read_bytes()
        if KEY.search(content):
            raise ValueError("Credential-like content in: " + name)
        entries.append(dict(path=name, bytes=len(content), sha256=hashlib.sha256(content).hexdigest()))
        contents[name] = content
    manifest = dict(
        version=version, format=1, root="Check", files=entries,
        scope="public_source_synthetic_fixtures_and_archived_model_responses",
        private_documents_included=False, credentials_included=False,
        system_dependencies="Python 3.12; Windows Microsoft Word for legacy .doc; optional cloudflared",
        source_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        source_dirty=bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
    )
    contents["release-manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name, content in sorted(contents.items()):
            info = zipfile.ZipInfo("Check/" + name, date_time=(2026, 10, 23, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, content)
    verify(output)
    sha = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(sha + "  " + output.name + "\n", encoding="ascii")
    print(json.dumps(dict(path=str(output), files=len(entries), sha256=sha, verified=True)))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--version", default="stage9-v1")
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--compare-root", type=Path, help="Also compare the ZIP against this Git working tree")
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(dict(version=verify(args.verify)["version"], verified=True)))
        if args.compare_root:
            result = compare_source(args.verify, args.compare_root)
            print(json.dumps(result))
            if not result["passed"]:
                raise SystemExit(1)
    elif args.output:
        build(args.output.resolve(), args.version)
    else:
        parser.error("supply --output or --verify")


if __name__ == "__main__":
    main()
