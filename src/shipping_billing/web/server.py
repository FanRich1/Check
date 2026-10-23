"""Loopback-only HTTP adapter. Credentials stay in Python; browsers submit files and business context."""

import argparse
import secrets
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from shipping_billing.config import Settings
from shipping_billing.errors import BillingError
from shipping_billing.io import dumps, load_json
from shipping_billing.web.service import DOWNLOADS, MAX_FILE, MAX_REQUEST, JobStore, local_ocr_available

STATIC = Path(__file__).with_name("static")


def open_workspace_browser(url):
    """Keep browser-association failures from silently hiding a running workspace."""
    try:
        if webbrowser.open(url):
            return True
    except (OSError, webbrowser.Error):
        pass
    print(f"未能自动打开浏览器，请手动访问：{url}", flush=True)
    return False


def json_response(value, status=200):
    return Response(dumps(value), status_code=status, media_type="application/json")


def create_app(project, settings=None, *, cloudflared=None, pipeline=None):
    kwargs = {"pipeline": pipeline} if pipeline is not None else {}
    store = JobStore(project, settings or Settings(), cloudflared=cloudflared, **kwargs)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        store.close()

    app = FastAPI(title="航运账单工作台", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def local_boundary(request, call_next):
        origin = request.headers.get("origin")
        landing_page = request.method in ("GET", "HEAD") and request.url.path == "/"
        if not landing_page and (
            (origin and origin.rstrip("/") != str(request.base_url).rstrip("/"))
            or request.headers.get("sec-fetch-site") in ("cross-site", "same-site")
        ):
            return json_response(
                {"error": {"code": "ORIGIN_REJECTED", "message": "请在本地工作台页面内操作。"}}, 403
            )
        if request.method not in ("GET", "HEAD"):
            if not secrets.compare_digest(request.headers.get("x-workspace-token", ""), token):
                return json_response(
                    {"error": {"code": "TOKEN_REQUIRED", "message": "页面会话已失效，请刷新后重试。"}}, 403
                )
            try:
                size = int(request.headers.get("content-length", "-1"))
            except ValueError:
                size = -1
            if size < 0 or size > MAX_REQUEST + 1024 * 1024:
                return json_response(
                    {"error": {"code": "REQUEST_TOO_LARGE", "message": "上传内容超过限制或缺少长度信息。"}},
                    413,
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(BillingError)
    async def business_error(request, exc):
        status = (
            404
            if exc.code in ("JOB_NOT_FOUND", "RECORD_NOT_FOUND")
            else 429
            if exc.code == "QUEUE_FULL"
            else 400
        )
        message = (
            "JSON 文件无效，请检查语法、重复字段及非有限数值。"
            if exc.code == "INVALID_JSON_INPUT"
            else exc.message
        )
        return json_response({"error": {"code": exc.code, "message": message}}, status)

    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        return json_response(
            {
                "error": {
                    "code": "INVALID_OLD_STATE_OR_CONTEXT",
                    "message": "旧账单或业务配置不符合要求，请检查记录编号、字段类型和重复记录。",
                }
            },
            400,
        )

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/assets/{name}")
    def asset(name: str):
        if name not in ("app.css", "app.js", "mark.svg"):
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.get("/api/config")
    def config():
        return json_response(
            dict(
                app="shipping-billing-workspace",
                token=token,
                model_ready=store.settings.mode == "online"
                and store.settings.public_summary()["model_configured"],
                model_name=store.settings.model_name,
                ocr_ready=bool(cloudflared and Path(cloudflared).is_file()),
                local_ocr_ready=local_ocr_available(),
                demo_ready=(store.project / "tests/fixtures/harness/DEV-025/input.xlsx").is_file(),
                max_file_mb=MAX_FILE // 1024 // 1024,
                max_total_mb=MAX_REQUEST // 1024 // 1024,
            )
        )

    @app.get("/api/jobs")
    def jobs():
        return json_response({"jobs": store.listing()})

    @app.post("/api/demo", status_code=202)
    def demo():
        return json_response(store.demo(), 202)

    @app.post("/api/jobs", status_code=202)
    async def submit(request: Request):
        async with request.form(max_files=24, max_fields=10, max_part_size=MAX_FILE) as form:
            unknown = set(form) - {
                "documents",
                "entity",
                "old_state",
                "context",
                "ocr_map",
                "base_job",
                "temporary_ocr",
                "local_ocr_enabled",
            }
            if unknown:
                raise BillingError("INVALID_FORM", "上传表单包含不支持的字段。")
            for name in ("entity", "old_state", "context", "ocr_map", "base_job", "temporary_ocr"):
                if len(form.getlist(name)) > 1:
                    raise BillingError("DUPLICATE_FORM_FIELD", "配置字段不能重复提交。")
            files = []
            total = 0
            for upload in form.getlist("documents"):
                if not isinstance(upload, UploadFile):
                    raise BillingError("INVALID_UPLOAD", "请通过文件选择器上传业务材料。")
                content = await upload.read(MAX_FILE + 1)
                total += len(content)
                if len(content) > MAX_FILE or total > MAX_REQUEST:
                    raise BillingError("UPLOAD_SIZE_LIMIT", "单文件最多 20 MB，合计最多 60 MB。")
                files.append((upload.filename or "document", content))

            async def json_field(name):
                upload = form.get(name)
                if upload is None or (isinstance(upload, UploadFile) and not upload.filename):
                    return None
                if not isinstance(upload, UploadFile):
                    raise BillingError("INVALID_JSON_UPLOAD", "旧账单和业务配置须选择 JSON 文件。")
                content = await upload.read(4 * 1024 * 1024 + 1)
                if len(content) > 4 * 1024 * 1024:
                    raise BillingError("CONFIG_SIZE_LIMIT", "旧账单及配置文件每个最多 4 MB。")
                # Reuse the same unique-key, finite Decimal parser as the CLI, without persisting secrets.
                from tempfile import TemporaryDirectory

                with TemporaryDirectory(prefix="billing-json-") as temporary:
                    path = Path(temporary) / "input.json"
                    path.write_bytes(content)
                    value = load_json(path)
                    if value is None:
                        raise BillingError(
                            "INVALID_JSON_UPLOAD", "配置文件不能为 null，请提供有效的旧账单或配置对象。"
                        )
                    return value

            for name in ("entity", "base_job", "temporary_ocr", "local_ocr_enabled"):
                if form.get(name) is not None and not isinstance(form.get(name), str):
                    raise BillingError("INVALID_FORM_FIELD", "记账主体或处理选项格式不正确。")
            entity = str(form.get("entity", "")).strip()
            if len(entity) > 300:
                raise BillingError("ENTITY_TOO_LONG", "记账主体名称最多 300 字。")
            result = store.submit(
                files,
                entity=entity,
                state_data=await json_field("old_state"),
                context_data=await json_field("context"),
                ocr_map=await json_field("ocr_map"),
                base_job=form.get("base_job") or None,
                temporary_ocr=form.get("temporary_ocr") == "true",
                local_ocr_enabled=form.get("local_ocr_enabled") == "true",
            )
            return json_response(result, 202)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return json_response(store.result(job_id))

    @app.post("/api/jobs/{job_id}/review")
    async def review(job_id: str, request: Request):
        if int(request.headers.get("content-length", "0")) > 16000:
            raise BillingError("INVALID_REVIEW", "核对备注过长。")
        try:
            data = await request.json()
            if (
                not isinstance(data, dict)
                or set(data) != {"record_id", "status", "note"}
                or not all(isinstance(v, str) for v in data.values())
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise BillingError("INVALID_REVIEW", "核对请求格式无效。") from None
        return json_response(store.review(job_id, data["record_id"], data["status"], data["note"]))

    @app.get("/api/jobs/{job_id}/download/{name}")
    def download(job_id: str, name: str):
        if name not in DOWNLOADS:
            raise HTTPException(404)
        payload = store.result(job_id)
        if name not in payload["downloads"]:
            raise HTTPException(404)
        folder = store.folder(job_id)
        return FileResponse(
            folder / name if name == "reviews.json" else folder / "result" / name,
            media_type="application/json",
            filename=name,
        )

    return app


def main():
    parser = argparse.ArgumentParser(description="Start the local shipping billing workspace")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--cloudflared", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    project = args.project_root.resolve()
    env = args.env_file.resolve() if args.env_file else None
    if env and not env.is_file():
        print("未找到指定的 .env 文件，将以未配置模型的状态启动；仍可体验示例。")
        env = None
    try:
        settings = Settings(_env_file=env)
    except ValidationError:
        print("模型配置无效，已关闭在线处理；请修正 .env 后重启。仍可体验示例。")
        settings = Settings(_env_file=None, mode="offline")
    cloudflared = args.cloudflared or next(
        (p for p in (project / "tools/cloudflared.exe", project / "outputs/stage2/tunnel/cloudflared.exe")
         if p.is_file()), project / "tools/cloudflared.exe"
    )
    if not cloudflared.is_file():
        cloudflared = None
    url = f"http://127.0.0.1:{args.port}"
    import socket

    with socket.socket() as probe:
        probe.settimeout(1)
        in_use = probe.connect_ex(("127.0.0.1", args.port)) == 0
    if in_use:
        import json
        import urllib.request

        try:
            with urllib.request.urlopen(url + "/api/config", timeout=2) as response:
                existing = json.load(response)
            if existing.get("app") != "shipping-billing-workspace":
                raise ValueError
        except (OSError, ValueError, AttributeError):
            parser.error("该端口已被其他程序占用，请使用 --port 指定不同端口。")
        print(f"工作台已在运行：{url}", flush=True)
        if args.open_browser:
            open_workspace_browser(url)
        return 0
    if args.open_browser:

        def open_when_ready():
            import time
            import urllib.request

            for _ in range(60):
                try:
                    with urllib.request.urlopen(url + "/api/config", timeout=1) as response:
                        if response.status == 200:
                            print(f"工作台已就绪：{url}", flush=True)
                            open_workspace_browser(url)
                            return
                except OSError:
                    time.sleep(0.3)
            print("启动等待超时，请查看启动窗口的错误信息。", flush=True)

        threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"航运账单工作台：{url}\n按 Ctrl+C 停止服务。", flush=True)
    import uvicorn

    uvicorn.run(
        create_app(project, settings, cloudflared=cloudflared),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
