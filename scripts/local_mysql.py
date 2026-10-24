"""Prepare/start an isolated loopback-only MySQL; credentials and server data remain local."""

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
import zipfile
from pathlib import Path

import httpx
import pymysql
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "data/local/mysql"
RUNTIME = ROOT / "outputs/mysql/runtime"
VERSION = "8.4.13"
CHECKSUM = "7b94489d5524dea9b27097068bea25a8"


def binaries():
    return RUNTIME / f"mysql-{VERSION}-winx64/bin/mysqld.exe"


def info():
    return json.loads((LOCAL / "instance.json").read_text(encoding="utf-8"))


def connect(password, port):
    return pymysql.connect(host="127.0.0.1", port=port, user="root", password=password,
                           connect_timeout=2, read_timeout=5, write_timeout=5, autocommit=True)


def launch_paths():
    # MySQL's Windows executable cannot reliably parse a Unicode argv[0]. Junctions keep data in-project.
    alias = Path(os.environ["LOCALAPPDATA"]) / "CheckBilling" / hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
    alias.mkdir(parents=True, exist_ok=True)
    for name, target in (("runtime", binaries().parents[1]), ("data", LOCAL / "server")):
        target.mkdir(parents=True, exist_ok=True)
        link = alias / name
        if link.exists():
            if link.resolve() != target.resolve():
                raise RuntimeError("Existing MySQL alias points to another directory; left untouched")
        else:
            environment = {**os.environ, "BILLING_ALIAS_LINK": str(link), "BILLING_ALIAS_TARGET": str(target)}
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "New-Item -ItemType Junction -Path $env:BILLING_ALIAS_LINK -Target $env:BILLING_ALIAS_TARGET | Out-Null"],
                           env=environment, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return alias, alias / "runtime/bin/mysqld.exe", alias / "data"


def start(configuration):
    alias, executable, data = launch_paths()
    connection = None
    for password in ([configuration["admin_password"]] if configuration["ready"] else
                     [configuration["admin_password"], ""]):
        try:
            connection = connect(password, configuration["port"])
            break
        except pymysql.MySQLError:
            continue
    if connection:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT @@datadir")
                actual = Path(cursor.fetchone()[0]).resolve()
                if actual != (LOCAL / "server").resolve():
                    raise RuntimeError("The selected port belongs to a different MySQL data directory")
        return
    with socket.socket() as probe:
        probe.settimeout(.5)
        if probe.connect_ex(("127.0.0.1", configuration["port"])) == 0:
            raise RuntimeError("Selected port is in use; no other server was started or modified")
    log = ROOT / "outputs/mysql/server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        subprocess.Popen(
            [str(executable), "--no-defaults", "--basedir=" + str(alias / "runtime"),
             "--datadir=" + str(data), "--bind-address=127.0.0.1",
             "--port=" + str(configuration["port"]), "--mysqlx=OFF", "--skip-log-bin",
             "--character-set-server=utf8mb4", "--collation-server=utf8mb4_bin", "--console"],
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=alias,
        )


def prepare():
    executable = binaries()
    if not executable.is_file():
        RUNTIME.mkdir(parents=True, exist_ok=True)
        archive = RUNTIME / f"mysql-{VERSION}-winx64.zip"
        if not archive.is_file():
            print("Downloading official MySQL Community ZIP...", flush=True)
            with httpx.Client(timeout=180, follow_redirects=True) as client:
                with client.stream("GET", f"https://cdn.mysql.com/Downloads/MySQL-8.4/{archive.name}") as response:
                    response.raise_for_status()
                    with archive.open("wb") as output:
                        for chunk in response.iter_bytes(1024 * 1024):
                            output.write(chunk)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "md5").hexdigest() != CHECKSUM:
                raise RuntimeError("Official MySQL archive checksum mismatch")
        with zipfile.ZipFile(archive) as bundle:
            if any(not (RUNTIME / p.filename).resolve().is_relative_to(RUNTIME.resolve())
                   for p in bundle.infolist()):
                raise RuntimeError("Invalid archive member path")
            bundle.extractall(RUNTIME)
    LOCAL.mkdir(parents=True, exist_ok=True)
    metadata = LOCAL / "instance.json"
    if metadata.is_file():
        configuration = info()
    else:
        if (LOCAL / "server").exists():
            raise RuntimeError("Existing database directory has no managed configuration; it was left untouched")
        configuration = dict(port=3307, admin_password=secrets.token_urlsafe(32),
                             app_password=secrets.token_urlsafe(32), ready=False)
        metadata.write_text(json.dumps(configuration), encoding="utf-8")
        alias, executable, data = launch_paths()
        initial_log = ROOT / "outputs/mysql/initialize.log"
        with initial_log.open("wb") as log:
            completed = subprocess.run(
                [str(executable), "--no-defaults", "--initialize-insecure",
                 "--basedir=" + str(alias / "runtime"), "--datadir=" + str(data)],
                stdout=log, stderr=log, timeout=180,
                cwd=alias,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if completed.returncode:
            raise RuntimeError("MySQL initialization failed; see the local initialization log")
    start(configuration)
    connection = None
    for _ in range(60):
        try:
            try:
                connection = connect(configuration["admin_password"], configuration["port"])
            except pymysql.MySQLError:
                if configuration["ready"]:
                    raise
                connection = connect("", configuration["port"])
            break
        except pymysql.MySQLError:
            time.sleep(.5)
    if connection is None:
        raise RuntimeError("Local MySQL did not become ready; see outputs/mysql/server.log")
    with connection:
        with connection.cursor() as cursor:
            if not configuration["ready"]:
                cursor.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (configuration["admin_password"],))
            for database in ("shipping_billing", "shipping_billing_test"):
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS {database} CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
                for host in ("localhost", "127.0.0.1"):
                    cursor.execute("CREATE USER IF NOT EXISTS 'billing_app'@%s IDENTIFIED BY %s",
                                   (host, configuration["app_password"]))
                    cursor.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,INDEX,REFERENCES,ALTER ON {database}.* "
                                   "TO 'billing_app'@%s", (host,))
    configuration["ready"] = True
    metadata.write_text(json.dumps(configuration), encoding="utf-8")
    env = ROOT / ".env"
    if not env.exists():
        env.write_bytes((ROOT / ".env.example").read_bytes())
    text = env.read_text(encoding="utf-8-sig")
    configured = any(line.strip().startswith("BILLING_DATABASE_URL=") for line in text.splitlines())
    url = URL.create("mysql+pymysql", username="billing_app", password=configuration["app_password"],
                     host="127.0.0.1", port=configuration["port"], database="shipping_billing",
                     query={"charset": "utf8mb4"})
    if not configured:
        with env.open("a", encoding="utf-8") as output:
            output.write("\nBILLING_DATABASE_URL=" + url.render_as_string(hide_password=False) + "\n")
    print("Local MySQL ready on 127.0.0.1:3307. Existing application settings were preserved.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "start", "status"])
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare()
        else:
            configuration = info()
            if args.command == "start":
                start(configuration)
                for attempt in range(60):
                    try:
                        with connect(configuration["admin_password"], configuration["port"]):
                            break
                    except pymysql.MySQLError:
                        if attempt == 59:
                            raise RuntimeError("Local MySQL did not become ready") from None
                        time.sleep(.5)
                print("Local MySQL ready. Data directory preserved.")
            else:
                with connect(configuration["admin_password"], configuration["port"]) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT VERSION()")
                        print("Local MySQL:", cursor.fetchone()[0])
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, pymysql.MySQLError, httpx.HTTPError) as exc:
        print("Local MySQL operation failed:", type(exc).__name__, "; see local logs; credentials were not printed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
