"""Install the locked application in this extracted project, without modifying existing configuration."""

import argparse
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-ocr", action="store_true")
    parser.add_argument("--cloudflared", type=Path, help="Explicitly copy a locally downloaded executable")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error("Python 3.12 is required. Install it, then run: py -3.12 scripts/install_windows.py")
    environment = ROOT / ".venv"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        print("Creating project Python environment...", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    try:
        subprocess.run(
            [str(python), "-m", "pip", "--disable-pip-version-check", "install", "uv==0.13.5"],
            check=True, cwd=ROOT,
        )
        uv = python.with_name("uv.exe" if sys.platform == "win32" else "uv")
        command = [str(uv), "sync", "--frozen", "--no-editable", "--link-mode", "copy"]
        if args.local_ocr:
            command.extend(["--extra", "local-ocr"])
        subprocess.run(command, cwd=ROOT, check=True)
        if args.cloudflared:
            source = args.cloudflared.resolve(strict=True)
            target = ROOT / "tools/cloudflared.exe"
            if not source.is_file() or source.suffix.lower() != ".exe":
                parser.error("--cloudflared must name an existing executable downloaded from Cloudflare")
            target.parent.mkdir(exist_ok=True)
            if source != target.resolve():
                shutil.copyfile(source, target)
        config = ROOT / ".env"
        if not config.exists():
            shutil.copyfile(ROOT / ".env.example", config)
        print("Installed. Double-click the workspace launcher; the offline example needs no key.")
        return 0
    except (OSError, subprocess.CalledProcessError):
        print("Installation failed. Check the error above and the delivery guide.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
