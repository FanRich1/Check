"""Persist local jobs and execute the existing pipeline without changing ledger rules."""

import hashlib
import importlib.util
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from shipping_billing.config import Settings
from shipping_billing.errors import BillingError
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.pipeline.engine import run_pipeline
from shipping_billing.reconciliation.contracts import LedgerState, ReconciliationContext

DOWNLOADS = {"records.json", "audit.json", "manifest.json", "state.json", "reviews.json"}
FORMATS = {".eml", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".xls", ".xlsx", ".doc", ".docx"}
STAGES = {
    "queued": "等待处理",
    "parse": "读取材料",
    "extract": "提取费用",
    "intents": "识别修改要求",
    "reconcile": "核对旧账单",
    "write": "保存处理结果",
    "done": "处理完成",
}
MAX_FILE = 20 * 1024 * 1024
MAX_REQUEST = 60 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def local_ocr_available():
    return importlib.util.find_spec("rapidocr_onnxruntime") is not None


def native_path(path):
    """Windows model archives contain two SHA-256 names and may exceed MAX_PATH."""
    path = Path(path).resolve()
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(path))
    return path


def file_name(raw):
    # Treat Windows and POSIX separators identically, including uploads made by non-Windows clients.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw).strip(" .")[:140]
    if not name or name in (".", ".."):
        raise BillingError("INVALID_FILENAME", "文件名无效，请重新选择文件。")
    return name


def display(value):
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return dumps(value)


def explain(code):
    messages = {
        "MISSING_ACCOUNTING_ENTITY": "尚未指定记账主体，无法判断应收或应付。",
        "UNKNOWN_PARTY_ROLE": "材料中的开票方或付款方关系不明确。",
        "MULTIPLE_MATCHES": "匹配到多条旧账单，需要人工确认目标记录。",
        "AMBIGUOUS_CURRENCY": "材料仅提供含糊货币符号，无法唯一确定币种。",
        "MISSING_CURRENCY": "材料缺少明确币种。",
        "UNAUTHORIZED_CHANGE": "发现新旧值差异，但材料没有明确授权修改。",
        "IMAGE_TEXT_REQUIRES_VERIFICATION": "图片识别结果需要对照原图核对。",
        "PDF_NO_NATIVE_TEXT": "扫描 PDF 缺少可交叉校验的原生文字，需要核对识别结果。",
        "OCR_EMPTY_CONTENT": "指定 OCR 未返回有效文字。其他已识别费用仍予保留。",
        "OCR_STRUCTURED_REQUIRES_CHECK": "指定 OCR 返回了结构化字段，需要对照原文件核对费用行及单据编号。",
        "OCR_NO_ORIGINAL_TEXT": "指定 OCR 未返回页面原文，不能仅将其结构化字段作为原文证据；请对照原文件核对。",
        "UPDATE_WITHOUT_OLD_SNAPSHOT": "检测到可能的修改请求，但没有对应旧账单，需确认本次是否确实涉及修改。",
        "INVALID_MERGED_CORE": "金额、币种、费用名称或应收应付等必需信息未齐全，暂不能自动入账。",
        "MISSING_OCR_DELIVERY": "该来源需要 OCR。请启用临时识别链接，或提供已有页面链接映射后重新处理。",
        "OCR_TIMEOUT_UNKNOWN_OUTCOME": "OCR 请求超时，未将不确定的识别结果作为成功。",
        "MODEL_REQUEST_FAILED": "模型请求失败，请检查模型配置或网络后重新处理。",
        "MODEL_CALL_FAILED": "模型服务调用失败。请检查网络或模型服务状态，再重新提交；本次没有自动换模型或重试。",
        "MODEL_AUTH_FAILED": "模型密钥未通过验证，请更新项目 .env 中的密钥并重启。",
        "MODEL_BALANCE_REQUIRED": "模型服务提示余额不足，请在服务平台补充额度后重新处理。",
        "MODEL_RATE_LIMITED": "模型服务暂时限制了请求频率，请稍后重新处理。",
        "MODEL_TRUNCATED": "模型返回内容被长度限制截断，请减少本次材料后重新处理。",
        "MODEL_EMPTY_CONTENT": "模型未返回有效内容，请稍后重新处理。",
        "MODEL_INVALID_STRUCTURE": "模型未返回有效结构，有限修复后仍无法使用。",
        "NO_SOURCE_TEXT": "该文件没有可用文字，无法提取费用。",
        "INVALID_OLD_STATE": "旧账单 JSON 无效，或记录编号缺失、重复。",
        "MODEL_NOT_CONFIGURED": "模型尚未配置。请在项目 .env 中完成配置后重启工作台。",
    }
    return messages.get(code, "该项目未满足自动处理条件，请查看原文证据及问题代码。")


class JobStore:
    def __init__(self, project, settings, *, cloudflared=None, pipeline=run_pipeline):
        self.project = Path(project).resolve()
        self.root = self.project / "outputs/web/jobs"
        self.inputs = self.project / "data/local/web"
        self.settings, self.cloudflared, self.pipeline = settings, cloudflared, pipeline
        self.root.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="billing-web")
        self.lock = threading.RLock()
        self.active = set()
        # A terminated process cannot resume an uncertain remote model/OCR invocation automatically.
        for path in self.root.glob("*/job.json"):
            try:
                item = load_json(path)
                if item["status"] in ("queued", "running"):
                    item.update(
                        status="interrupted", message="上次服务已停止，请重新提交处理。", updated_at=now()
                    )
                    write_json(path, item)
            except (BillingError, KeyError, TypeError):
                continue

    def folder(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise BillingError("JOB_NOT_FOUND", "找不到该任务。")
        folder = self.root / job_id
        if not (folder / "job.json").is_file():
            raise BillingError("JOB_NOT_FOUND", "找不到该任务。")
        return folder

    def get(self, job_id):
        with self.lock:
            return load_json(self.folder(job_id) / "job.json")

    def listing(self):
        with self.lock:
            jobs = []
            for path in self.root.glob("*/job.json"):
                try:
                    jobs.append(load_json(path))
                except BillingError:
                    continue
            return sorted(jobs, key=lambda j: j["created_at"], reverse=True)[:100]

    def update(self, job_id, **values):
        with self.lock:
            path = self.folder(job_id) / "job.json"
            item = load_json(path)
            item.update(values, updated_at=now())
            write_json(path, item)

    def submit(
        self,
        files,
        *,
        entity="",
        state_data=None,
        context_data=None,
        base_job=None,
        temporary_ocr=False,
        local_ocr_enabled=False,
        ocr_map=None,
        demo=False,
    ):
        if not files or len(files) > 20:
            raise BillingError("INVALID_FILE_COUNT", "请选择 1 至 20 个业务文件。")
        if any(not b or len(b) > MAX_FILE for _, b in files) or sum(len(b) for _, b in files) > MAX_REQUEST:
            raise BillingError("UPLOAD_SIZE_LIMIT", "单文件须为 1 字节至 20 MB，合计不超过 60 MB。")
        if not demo and (
            self.settings.mode != "online" or not self.settings.public_summary()["model_configured"]
        ):
            raise BillingError("MODEL_NOT_CONFIGURED", explain("MODEL_NOT_CONFIGURED"))
        if base_job and state_data is not None:
            raise BillingError("CONFLICTING_OLD_STATE", "旧账单文件和历史任务账本只能选择一种。")
        if base_job:
            if self.get(base_job).get("demo"):
                raise BillingError(
                    "DEMO_STATE_CANNOT_CONTINUE", "示例账本只用于体验，请为真实材料单独开始一个任务。"
                )
            self.result(base_job)
            state_path = self.folder(base_job) / "result/state.json"
            if not state_path.is_file():
                raise BillingError("STATE_NOT_READY", "所选任务没有可继续使用的账本。")
            state_data = load_json(state_path)
        state = (
            LedgerState(bills=state_data)
            if isinstance(state_data, list)
            else LedgerState.model_validate(state_data if state_data is not None else {})
        )
        context = ReconciliationContext.model_validate(context_data if context_data is not None else {})
        configured = context.normalization_context.accounting_entity
        if entity and configured and entity != configured:
            raise BillingError("CONFLICTING_ACCOUNTING_ENTITY", "页面记账主体与业务配置文件中的主体不同。")
        context.normalization_context.accounting_entity = entity or configured
        if not context.normalization_context.accounting_entity:
            raise BillingError("ENTITY_REQUIRED", "请填写本次记账公司的完整名称。")
        if temporary_ocr and (self.cloudflared is None or not Path(self.cloudflared).is_file()):
            raise BillingError("TUNNEL_TOOL_MISSING", "临时链接工具未就绪，请先按使用说明配置。")
        if local_ocr_enabled and not local_ocr_available():
            raise BillingError("LOCAL_OCR_NOT_INSTALLED", "本地辅助识别尚未安装，请先安装 local-ocr 可选依赖。")
        if ocr_map is not None and not isinstance(ocr_map, dict):
            raise BillingError("INVALID_OCR_MAPPING", "OCR 页面映射必须为 JSON 对象。")
        if temporary_ocr and ocr_map is not None:
            raise BillingError("CONFLICTING_OCR_DELIVERY", "临时识别链接和已有 OCR 映射只能选择一种。")
        cleaned = [(file_name(n), b) for n, b in files]
        if any(Path(n).suffix.lower() not in FORMATS for n, _ in cleaned):
            raise BillingError(
                "UNSUPPORTED_FILE", "业务文件支持 EML、PDF、图片、Excel 和 Word；旧账单 JSON 请单独上传。"
            )
        if (
            not demo
            and not temporary_ocr
            and ocr_map is None
            and any(
                Path(n).suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
                for n, _ in cleaned
            )
        ):
            raise BillingError("MISSING_OCR_DELIVERY", explain("MISSING_OCR_DELIVERY"))
        with self.lock:
            if len(self.active) >= 3:
                raise BillingError("QUEUE_FULL", "已有三个任务正在处理或等待，请稍后再提交。")
            job_id = uuid4().hex
            folder, upload = self.root / job_id, self.inputs / job_id
            folder.mkdir()
            upload.mkdir(parents=True)
            paths = []
            for index, (name, content) in enumerate(cleaned, 1):
                path = upload / f"{index:02}-{name}"
                path.write_bytes(content)
                paths.append(path)
            mapping = None
            if ocr_map is not None:
                mapping = upload / "ocr-map.json"
                write_json(mapping, ocr_map)
            item = dict(
                id=job_id,
                title=cleaned[0][0] if len(files) == 1 else f"{cleaned[0][0]} 等 {len(files)} 个文件",
                entity=context.normalization_context.accounting_entity,
                created_at=now(),
                updated_at=now(),
                status="queued",
                stage="queued",
                message=STAGES["queued"],
                demo=demo,
                input_files=[
                    dict(name=n, size=len(b), sha256=hashlib.sha256(b).hexdigest()) for n, b in cleaned
                ],
                counts={},
                exit_code=None,
                base_job=base_job,
                local_ocr_enabled=local_ocr_enabled,
            )
            write_json(folder / "job.json", item)
            self.active.add(job_id)
            self.executor.submit(
                self.execute, job_id, paths, state, context, temporary_ocr, mapping, demo, local_ocr_enabled
            )
            return deepcopy(item)

    def execute(self, job_id, paths, state, context, temporary_ocr, mapping, demo, local_ocr_enabled=False):
        try:
            local_ocr = None
            if local_ocr_enabled:
                from shipping_billing.parsers.local_text import LocalTextOCR

                local_ocr = LocalTextOCR()
            self.update(job_id, status="running", stage="parse", message=STAGES["parse"])
            self.pipeline(
                paths,
                settings=Settings() if demo else self.settings,
                output=self.folder(job_id) / "result",
                storage=native_path(
                    self.project
                    / (
                        "tests/fixtures/harness/replay-cache/development/round-1"
                        if demo
                        else f"data/local/web-cache/{job_id}"
                    )
                ),
                mode="replay" if demo else "live",
                state=state,
                context=context,
                source_id="case-DEV-025" if demo else None,
                ocr_map=mapping,
                cloudflared=self.cloudflared if temporary_ocr else None,
                local_ocr=local_ocr,
                progress=lambda stage, count: self.update(
                    job_id, stage=stage, message=STAGES.get(stage, stage)
                ),
            )
            manifest = load_json(self.folder(job_id) / "result/manifest.json")
            self.update(
                job_id,
                status=manifest["status"],
                stage="done",
                counts=manifest["counts"],
                exit_code=manifest["exit_code"],
                message={
                    "completed": "处理完成，可以查看和下载账单。",
                    "pending_review": "处理完成，部分内容需要人工核对。",
                    "error": "部分来源处理失败，已保留成功结果及错误原因。",
                }[manifest["status"]],
            )
        except BillingError as exc:
            self.update(job_id, status="failed", message=explain(exc.code), error_code=exc.code)
        except Exception:
            self.update(
                job_id,
                status="failed",
                message="处理未能完成，请检查材料或稍后重试。",
                error_code="WEB_EXECUTION_FAILED",
            )
        finally:
            with self.lock:
                self.active.discard(job_id)

    def demo(self):
        path = self.project / "tests/fixtures/harness/DEV-025/input.xlsx"
        if not path.is_file():
            raise BillingError("DEMO_NOT_INSTALLED", "当前安装不含示例材料，请从项目完整目录启动。")
        return self.submit([(path.name, path.read_bytes())], entity="Cedar Development Buyer", demo=True)

    def result(self, job_id):
        item = self.get(job_id)
        folder = self.folder(job_id)
        result = folder / "result"
        if not (result / "manifest.json").is_file():
            return dict(job=item, records=[], files=[], downloads=[], reviews={})
        manifest = load_json(result / "manifest.json")
        # Only complete, hash-verified artifacts may be displayed, continued or exported.
        try:
            for name in ("records.json", "state.json", "audit.json", "reconciliation.json"):
                if hashlib.sha256((result / name).read_bytes()).hexdigest() != manifest[
                    "artifact_sha256"
                ].get(name):
                    raise BillingError("RESULT_INTEGRITY_FAILED", "结果文件校验未通过，请重新处理。")
        except OSError:
            raise BillingError("RESULT_INTEGRITY_FAILED", "结果文件缺失或无法读取，请重新处理。") from None
        rows = load_json(result / "records.json")
        audit = {r["record_id"]: r for r in load_json(result / "audit.json")}
        decisions = load_json(result / "reconciliation.json")["decisions"]
        records = []
        for row in rows:
            record = dict(row)
            record["amount_display"] = display(row["charge_details"].get("amount"))
            record["quantity_display"] = display(row["charge_details"].get("quantity"))
            record["unit_price_display"] = display(row["charge_details"].get("unit_price"))
            record["issues"] = [
                dict(**issue, explanation=explain(issue["code"])) for issue in row["audit_trail"]["issues"]
            ]
            record["changes_display"] = [
                dict(path=c["path"], old=display(c["old"]), new=display(c["new"]))
                for c in row["audit_trail"]["changes"]
            ]
            record["rules"] = audit.get(row["record_id"], {}).get("rules", [])
            record["observations_display"] = [
                dict(
                    source_id=d["source_id"],
                    amount=display(d.get("observed", {}).get("charge_details", {}).get("amount")),
                    operation=d["operation"],
                    issue_codes=[i["code"] for i in d["issues"]],
                )
                for d in decisions
                if d["record_id"] == row["record_id"]
            ]
            records.append(record)
        files = [
            {**f, "issues": [dict(**i, explanation=explain(i["code"])) for i in f["issues"]]}
            for f in manifest["files"]
        ]
        with self.lock:
            reviews = load_json(folder / "reviews.json") if (folder / "reviews.json").exists() else {}
        return dict(
            job=item,
            records=records,
            files=files,
            reviews=reviews,
            downloads=[
                n for n in sorted(DOWNLOADS) if (folder / n if n == "reviews.json" else result / n).is_file()
            ],
        )

    def review(self, job_id, record_id, status, note):
        if status not in ("unreviewed", "checked", "needs_followup") or len(note) > 2000:
            raise BillingError("INVALID_REVIEW", "核对标记或备注无效，备注最多 2000 字。")
        if record_id not in {r["record_id"] for r in self.result(job_id)["records"]}:
            raise BillingError("RECORD_NOT_FOUND", "任务中不存在这条账单。")
        with self.lock:
            path = self.folder(job_id) / "reviews.json"
            reviews = load_json(path) if path.exists() else {}
            reviews[record_id] = dict(status=status, note=note, updated_at=now())
            write_json(path, reviews)
        return reviews[record_id]

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)
