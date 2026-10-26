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
from shipping_billing.domain.normalization import party_key
from shipping_billing.errors import BillingError
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.pipeline.engine import run_pipeline
from shipping_billing.reconciliation.contracts import LedgerState, ReconciliationContext
from shipping_billing.web.preflight import requires_local_text, requires_ocr_delivery
from shipping_billing.web.source_usage import IMAGES, assess_sources

DOWNLOADS = {"records.json", "audit.json", "manifest.json", "state.json", "reviews.json", "review-decisions.json",
             "source-reviews.json"}
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
        "UNKNOWN_PARTY_ROLE": "尚未确认谁付款、谁收款，无法判断应收或应付。发货人、收货人和通知方不能直接当作付款双方；请核对付款说明或补充关联邮件。",
        "BILLING_PARTY_ROLE_UNCONFIRMED": "原文仅证明收款、银行或运输角色，不能确认其为单据签发方或接收方；该字段已留空，请核对原件后确认。",
        "TUNNEL_NOT_READY": "OCR 临时访问链接未建立成功，该页尚未提交识别。请稍后重新处理；更换账本或公司名称不能解决此网络问题。",
        "TUNNEL_DOWNLOAD_FAILED": "OCR 临时文件链接无法访问，该页尚未提交识别。请检查网络后重新处理。",
        "NATIVE_TEXT_VERIFIED": "费用字段已逐项核对 PDF 原生文字；OCR 结构化结果仅作为辅助信息。",
        "IMAGE_TEXT_VERIFIED": "图片中的交易双方和字段已核验，两路识别的费用及编号一致，费用合计也已通过校验。",
        "OCR_AMOUNT_NOT_CORROBORATED": "OCR 费用金额与 PDF 原生费用行未能对应，请核对具体费用行。",
        "MULTIPLE_MATCHES": "匹配到多条旧账单，需要人工确认目标记录。",
        "RELATED_BUSINESS_FIELD_CONFLICT": "相关材料对同一字段提供了不同取值，已保留各自出处，请确认本笔费用应采用哪一个。",
        "RELATED_PARTIES_REQUIRE_CHECK": "已从相关材料找到付款双方候选，请核对是否适用于本笔费用，再确认入账。",
        "AMBIGUOUS_CURRENCY": "材料仅提供含糊货币符号，无法唯一确定币种。",
        "MISSING_CURRENCY": "材料缺少明确币种。",
        "PAYMENT_CONDITION_ONLY": "已保留原文中的付款条件；“立即付款”或 COD 不直接等同 PP/CC，此项不单独阻止入账。",
        "ACCOUNTING_ENTITY_NOT_PARTY": "记账主体与已识别的付款方、收款方均不匹配；请核对公司全称，缩写或其他名称需配置公司别名。",
        "LOCAL_OCR_REQUIRED": "材料含图片或扫描页，请勾选「为图片 / 扫描件补充本地文字识别」后提交；未安装时请先运行安装脚本安装 local-ocr。",
        "LOCAL_OCR_NOT_INSTALLED": "本地文字识别尚未安装，请运行安装脚本安装 local-ocr，再刷新页面。",
        "CHILD_REQUIRES_REVIEW": "附件有未完成的识别或复核项，请查看对应附件；正文费用按自身证据单独核对。",
        "OCR_IDENTIFIER_NOT_CORROBORATED": "指定 OCR 的单据号或箱号与原文不一致，需对照原件确认。",
        "OCR_POSSIBLE_MERGED_FEE_ROWS": "指定 OCR 可能合并了原件中同名的多行费用；已保留分别提取的费用，请逐行核对。",
        "UNAUTHORIZED_CHANGE": "发现新旧值差异，但材料没有明确授权修改。",
        "IMAGE_TEXT_REQUIRES_VERIFICATION": "图片识别结果需要对照原图核对。",
        "PDF_NO_NATIVE_TEXT": "扫描 PDF 缺少可交叉校验的原生文字，需要核对识别结果。",
        "OCR_EMPTY_CONTENT": "指定 OCR 未返回有效文字。其他已识别费用仍予保留。",
        "OCR_STRUCTURED_REQUIRES_CHECK": "指定 OCR 返回了结构化字段，需要对照原文件核对费用行及单据编号。",
        "OCR_NO_ORIGINAL_TEXT": "指定 OCR 未返回页面原文，不能仅将其结构化字段作为原文证据；请对照原文件核对。",
        "UPDATE_WITHOUT_OLD_SNAPSHOT": "检测到可能的修改请求，但没有对应旧账单，需确认本次是否确实涉及修改。",
        "INVALID_MERGED_CORE": "金额、币种、费用名称或应收应付等必需信息未齐全，暂不能自动入账。",
        "MISSING_OCR_DELIVERY": "文件或邮件附件包含 PDF / 图片，请勾选「为 PDF / 图片启用指定 OCR」，或提供已有页面链接映射后重新提交。本地文字识别不能替代此选项。",
        "OCR_TIMEOUT_UNKNOWN_OUTCOME": "OCR 请求超时，未将不确定的识别结果作为成功。",
        "MODEL_REQUEST_FAILED": "模型请求失败，请检查模型配置或网络后重新处理。",
        "MODEL_CALL_FAILED": "模型服务调用失败。请检查网络或模型服务状态，再重新提交；本次没有自动换模型或重试。",
        "MODEL_AUTH_FAILED": "模型密钥未通过验证，请更新项目 .env 中的密钥并重启。",
        "MODEL_BALANCE_REQUIRED": "模型服务提示余额不足，请在服务平台补充额度后重新处理。",
        "MODEL_RATE_LIMITED": "模型服务暂时限制了请求频率，请稍后重新处理。",
        "MODEL_TRUNCATED": "模型返回内容被长度限制截断，请减少本次材料后重新处理。",
        "MODEL_EMPTY_CONTENT": "模型未返回有效内容，请稍后重新处理。",
        "MODEL_INVALID_STRUCTURE": "模型未返回有效结构，有限修复后仍无法使用。",
        "IMAGE_OCR_EMPTY_REQUIRES_PURPOSE_CONFIRMATION": "图片识别调用已完成，未返回账单内容；请查看原图确认用途，不能据此认定没有费用。",
        "NO_SOURCE_TEXT": "尚未取得可引用的原文，无法提取费用。图片或扫描件请启用本地文字识别后重新处理；这不代表原件没有费用。",
        "INVALID_OLD_STATE": "旧账单 JSON 无效，或记录编号缺失、重复。",
        "MODEL_NOT_CONFIGURED": "模型尚未配置。请在项目 .env 中完成配置后重启工作台。",
    }
    return messages.get(code, "该项目未满足自动处理条件，请查看原文证据及问题代码。")


def explain_issue(issue):
    if not issue.get("blocks_automation", True) and issue["code"] in (
        "OCR_STRUCTURED_REQUIRES_CHECK", "OCR_NO_ORIGINAL_TEXT", "LOCAL_OCR_LOW_CONFIDENCE",
        "IMAGE_TEXT_REQUIRES_VERIFICATION"
    ):
        return "本次费用字段已通过证据核验，此项识别提示不再阻止自动入账。"
    return explain(issue["code"])


def processing_summary(manifest, *, pending=None, assessed_files=None):
    """Keep billing decisions separate from incomplete document/attachment coverage."""
    counts, files = manifest["counts"], manifest["files"]
    fees = counts["fee_lines"]
    pending = counts["PENDING_REVIEW"] if pending is None else pending
    files = assessed_files if assessed_files is not None else files
    errors = [f for f in files if f.get("assessment", {}).get("status", f["status"]) == "error"]
    purposes = (sum(f.get("assessment", {}).get("status") == "purpose_pending" for f in files)
                if assessed_files is not None else counts.get("purpose_pending_sources", 0))
    attachments = sum(f.get("parent_source_id") is not None for f in errors)
    fee_message = f"已识别 {fees} 条费用，其中 {pending} 条待复核"
    if not fees:
        fee_message = "尚未提取到费用" if errors or purposes else "本次未发现费用"
    source_message = f"共 {len(files)} 个来源，{len(errors)} 个异常（其中 {attachments} 个附件）"
    if assessed_files is not None:
        source_message += f"，{purposes} 个用途待确认"
    message = (
        f"{fee_message}；{source_message}，已保留可用结果，请查看来源列表。" if errors
        else f"{fee_message}；{purposes} 个来源用途待确认，请查看原件后确认。" if purposes
        else f"{fee_message}，请核对后处理。" if pending
        else f"{fee_message}，可以查看和下载结果。"
    )
    return dict(fee_lines=fees, pending_fee_lines=pending, source_errors=len(errors),
                attachment_errors=attachments, fee_message=fee_message,
                source_message=source_message, purpose_pending_count=purposes, message=message)


class JobStore:
    def __init__(self, project, settings, *, cloudflared=None, pipeline=run_pipeline, repository=None):
        self.project = Path(project).resolve()
        self.root = self.project / "outputs/web/jobs"
        self.inputs = self.project / "data/local/web"
        self.settings, self.cloudflared, self.pipeline = settings, cloudflared, pipeline
        self.repository = repository
        if self.repository is None and settings.database_url:
            from shipping_billing.storage.database import LedgerRepository

            self.repository = LedgerRepository(settings.database_url.get_secret_value())
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
                if self.repository:
                    try:
                        saved = self.repository.get_job(item["id"])
                    except BillingError as exc:
                        if exc.code != "JOB_NOT_FOUND":
                            raise
                        self.repository.save_job(item)
                        self._migrate_artifacts(item)
                    else:
                        if saved["status"] in ("queued", "running"):
                            saved.update(status="interrupted", message="上次服务已停止，请重新提交处理。")
                            self.repository.save_job(saved)
            except BillingError as exc:
                if exc.code != "INVALID_JSON_INPUT":
                    raise
            except (KeyError, TypeError):
                continue

    def _migrate_artifacts(self, item):
        folder = self.root / item["id"]
        if not (folder / "result/manifest.json").is_file():
            return
        try:
            verified = self.result(item["id"])
        except BillingError as exc:
            if exc.code == "RESULT_INTEGRITY_FAILED":
                return
            raise
        records = load_json(folder / "result/records.json")
        manifest = load_json(folder / "result/manifest.json")
        self.repository.save_results(item, records, manifest["files"])
        if (folder / "reviews.json").is_file():
            known = {r["record_id"] for r in verified["records"]}
            for record_id, review in load_json(folder / "reviews.json").items():
                if record_id in known:
                    self.repository.save_review(item["id"], record_id, review)

    def folder(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise BillingError("JOB_NOT_FOUND", "找不到该任务。")
        folder = self.root / job_id
        if not folder.is_dir() or (not self.repository and not (folder / "job.json").is_file()):
            raise BillingError("JOB_NOT_FOUND", "找不到该任务。")
        return folder

    def get(self, job_id):
        with self.lock:
            if self.repository:
                return self.repository.get_job(job_id)
            return load_json(self.folder(job_id) / "job.json")

    def listing(self):
        with self.lock:
            if self.repository:
                jobs = self.repository.list_jobs()
            else:
                jobs = []
                for path in self.root.glob("*/job.json"):
                    try:
                        jobs.append(load_json(path))
                    except BillingError:
                        continue
                jobs = sorted(jobs, key=lambda j: j["created_at"], reverse=True)[:100]
            for index, item in enumerate(jobs):
                if item["status"] in {"completed", "pending_review", "error"}:
                    try:
                        if not (self.folder(item["id"]) / "result/manifest.json").is_file():
                            continue
                        jobs[index] = self.result(item["id"])["job"]
                    except BillingError:
                        pass  # History remains visible; opening a damaged task reports integrity failure.
            return jobs

    def update(self, job_id, **values):
        with self.lock:
            path = self.folder(job_id) / "job.json"
            item = self.get(job_id)
            item.update(values, updated_at=now())
            if self.repository:
                self.repository.save_job(item)
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
        ledger_id=None,
    ):
        if not files or len(files) > 20:
            raise BillingError("INVALID_FILE_COUNT", "请选择 1 至 20 个业务文件。")
        if any(not b or len(b) > MAX_FILE for _, b in files) or sum(len(b) for _, b in files) > MAX_REQUEST:
            raise BillingError("UPLOAD_SIZE_LIMIT", "单文件须为 1 字节至 20 MB，合计不超过 60 MB。")
        if ledger_id:
            if not self.repository:
                raise BillingError("DATABASE_NOT_CONFIGURED", "尚未配置数据库，不能选择数据库账本。")
            if demo or state_data is not None or base_job:
                raise BillingError("CONFLICTING_OLD_STATE", "已选择数据库账本，请通过账本管理导入旧账单。")
            book = self.repository.ledger(ledger_id)
            if entity and party_key(entity) != party_key(book["entity"]):
                raise BillingError("LEDGER_ENTITY_CONFLICT", "记账主体与所选账本不一致。")
            entity = book["entity"]
        elif self.repository and not demo:
            raise BillingError("LEDGER_REQUIRED", "请选择或新建一个数据库账本，再开始处理。")
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
        if entity and configured and party_key(entity) != party_key(configured):
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
            and (requires_ocr_delivery(cleaned) or any(
                Path(n).suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
                for n, _ in cleaned
            ))
        ):
            raise BillingError("MISSING_OCR_DELIVERY", explain("MISSING_OCR_DELIVERY"))
        if not demo and not local_ocr_enabled and requires_local_text(cleaned):
            raise BillingError("LOCAL_OCR_REQUIRED", explain("LOCAL_OCR_REQUIRED"))
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
                ledger_id=ledger_id,
                ledger_commit_status="pending" if ledger_id else None,
            )
            write_json(folder / "job.json", item)
            if self.repository:
                self.repository.save_job(item)
            self.active.add(job_id)
            self.executor.submit(
                self.execute, job_id, paths, state, context, temporary_ocr, mapping, demo, local_ocr_enabled
            )
            return deepcopy(item)

    def execute(self, job_id, paths, state, context, temporary_ocr, mapping, demo, local_ocr_enabled=False):
        try:
            ledger_id = self.get(job_id).get("ledger_id")
            if ledger_id:
                # Queued jobs read the latest committed ledger when execution actually begins.
                state, revision = self.repository.snapshot(ledger_id)
                self.update(job_id, ledger_base_revision=revision)
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
            final = dict(
                status=manifest["status"],
                stage="done",
                counts=manifest["counts"],
                exit_code=manifest["exit_code"],
                message=processing_summary(manifest)["message"],
            )
            if self.repository:
                # Verify archived artifacts before applying any balance changes.
                self.result(job_id)
                item = {**self.get(job_id), **final, "updated_at": now()}
                records = load_json(self.folder(job_id) / "result/records.json")
                if ledger_id:
                    next_state = LedgerState.model_validate(load_json(self.folder(job_id) / "result/state.json"))
                    item = self.repository.commit_job(item, next_state, records, manifest["files"])
                else:
                    self.repository.save_results(item, records, manifest["files"])
                # A mirror-write failure cannot turn a committed SQL transaction into a failed booking.
                try:
                    write_json(self.folder(job_id) / "job.json", item)
                except BillingError:
                    pass
            else:
                self.update(job_id, **final)
        except BillingError as exc:
            self.update(
                job_id, status="pending_review" if exc.code == "LEDGER_CHANGED" else "failed",
                message=exc.message if exc.code.startswith(("LEDGER_", "DATABASE_")) else explain(exc.code),
                error_code=exc.code,
                ledger_commit_status="conflict" if exc.code == "LEDGER_CHANGED" else "failed",
            )
        except Exception:
            self.update(
                job_id,
                status="failed",
                message="处理未能完成，请检查材料或稍后重试。",
                error_code="WEB_EXECUTION_FAILED",
                ledger_commit_status="failed",
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
            return dict(job=item, records=[], files=[], downloads=[], reviews={}, review_decisions={})
        manifest = load_json(result / "manifest.json")
        summary = processing_summary(manifest)
        if item["status"] in {"completed", "pending_review", "error"} and not item.get("error_code"):
            item = {**item, "message": summary["message"]}
        # Only complete, hash-verified artifacts may be displayed, continued or exported.
        try:
            for name in ("records.json", "state.json", "audit.json", "reconciliation.json", "extraction.json", "parse.json"):
                if hashlib.sha256((result / name).read_bytes()).hexdigest() != manifest[
                    "artifact_sha256"
                ].get(name):
                    raise BillingError("RESULT_INTEGRITY_FAILED", "结果文件校验未通过，请重新处理。")
        except OSError:
            raise BillingError("RESULT_INTEGRITY_FAILED", "结果文件缺失或无法读取，请重新处理。") from None
        rows = load_json(result / "records.json")
        extraction_data = load_json(result / "extraction.json")
        candidate_fields = {c["source_line_id"]: c.get("related_fields", [])
                            for file in extraction_data["files"] for c in file["candidates"]}
        audit = {r["record_id"]: r for r in load_json(result / "audit.json")}
        decisions = load_json(result / "reconciliation.json")["decisions"]
        records = []
        for row in rows:
            record = dict(row)
            record["amount_display"] = display(row["charge_details"].get("amount"))
            record["quantity_display"] = display(row["charge_details"].get("quantity"))
            record["unit_price_display"] = display(row["charge_details"].get("unit_price"))
            record["issues"] = [
                dict(**issue, explanation=explain_issue(issue)) for issue in row["audit_trail"]["issues"]
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
            record["related_fields"] = [field for d in decisions if d["record_id"] == row["record_id"]
                                        for field in candidate_fields.get(d["source_line_id"], [])]
            records.append(record)
        files = [
            {**f, "issues": [dict(**i, explanation=explain_issue(i)) for i in f["issues"]]}
            for f in manifest["files"]
        ]
        with self.lock:
            reviews = self.repository.get_reviews(job_id) if self.repository else (
                load_json(folder / "reviews.json") if (folder / "reviews.json").exists() else {}
            )
        resolutions = self.repository.get_decisions(job_id) if self.repository else {}
        from shipping_billing.storage.review import visible_plan

        resolutions = {rid: visible_plan(value) for rid, value in resolutions.items()}
        source_reviews = self.source_reviews(job_id)
        files = assess_sources(files, load_json(result / "parse.json"), extraction_data, records, resolutions, source_reviews)
        summary = processing_summary(manifest, pending=sum(
            d["operation"] == "PENDING_REVIEW" and d["record_id"] not in resolutions
            for d in decisions), assessed_files=files)
        if item["status"] in {"completed", "pending_review", "error"} and not item.get("error_code"):
            status = ("error" if summary["source_errors"] else "pending_review"
                      if summary["pending_fee_lines"] or summary["purpose_pending_count"] else "completed")
            item = {**item, "original_status": item["status"], "status": status, "message": summary["message"],
                    "source_purpose_pending_count": summary["purpose_pending_count"],
                    "source_error_count": summary["source_errors"]}
        return dict(
            job=item,
            processing_summary=summary,
            records=records,
            files=files,
            reviews=reviews,
            review_decisions=resolutions,
            business_links=extraction_data.get("business_links", []),
            source_reviews=source_reviews,
            downloads=[
                n for n in sorted(DOWNLOADS)
                if (folder / n if n == "reviews.json" else result / n).is_file()
                and not (n == "state.json" and item.get("ledger_id")
                         and item.get("ledger_commit_status") != "committed")
            ] + (["review-decisions.json"] if resolutions else []) + (["source-reviews.json"] if source_reviews else []),
        )

    def source_reviews(self, job_id):
        if self.repository:
            return self.repository.get_source_reviews(job_id)
        path = self.folder(job_id) / "source-reviews.json"
        return load_json(path) if path.is_file() else {}

    def review_source(self, job_id, request):
        from shipping_billing.extraction.engine import digest

        with self.lock:
            result = self.result(job_id)
            if result["job"].get("demo") or result["job"]["status"] not in {"completed", "pending_review", "error"}:
                raise BillingError("SOURCE_REVIEW_UNAVAILABLE", "任务尚未完成或不能确认材料用途。")
            file = next((f for f in result["files"] if f["source_id"] == request.source_id), None)
            if file is None:
                raise BillingError("SOURCE_NOT_FOUND", "找不到该来源文件。")
            if request.sha256 != file.get("sha256"):
                raise BillingError("SOURCE_REVIEW_CHANGED", "来源文件哈希不匹配，请刷新后核对原件。")
            if not file["assessment"]["can_confirm"]:
                raise BillingError("SOURCE_REVIEW_UNAVAILABLE", "该来源有费用或真实读取故障，不能用用途确认代替费用复核或故障处理。")
            if request.classification == "non_billing_image" and file["detected_format"] not in IMAGES:
                raise BillingError("INVALID_SOURCE_REVIEW", "只有图片可以确认为非账单图片。")
            self.original_file(job_id, request.source_id)  # The hashed original must still be available.
            if self.repository:
                return self.repository.save_source_review(job_id, request, file)
            history = self.source_reviews(job_id)
            previous = history.get(request.source_id, [])
            fingerprint = digest(request.model_dump(mode="python"))
            if previous and previous[-1]["request_sha256"] == fingerprint:
                return previous[-1]
            revision = previous[-1]["revision"] if previous else 0
            if request.revision != revision:
                raise BillingError("SOURCE_REVIEW_CHANGED", "该来源的用途确认已更新，请刷新后重新核对。")
            item = {**request.model_dump(mode="python"), "revision": revision + 1, "decided_at": now(),
                    "original_status": file["status"], "original_issues": file["issues"], "request_sha256": fingerprint}
            history.setdefault(request.source_id, []).append(item)
            write_json(self.folder(job_id) / "source-reviews.json", history)
            return item

    def review_origin(self, job_id, record_id, ledger_id):
        if not self.repository:
            raise BillingError("DATABASE_NOT_CONFIGURED", "正式复审需要先启用数据库账本。")
        result = self.result(job_id)
        original = next((r for r in load_json(self.folder(job_id) / "result/records.json")
                         if r["record_id"] == record_id), None) if result["records"] else None
        if original is None:
            raise BillingError("RECORD_NOT_FOUND", "任务中不存在这条费用。")
        if original["operation"] != "PENDING_REVIEW":
            raise BillingError("REVIEW_NOT_PENDING", "只有原始待复核费用可以正式复审。")
        item = result["job"]
        if item.get("demo") or item["status"] not in ("completed", "pending_review", "error"):
            raise BillingError("REVIEW_NOT_AVAILABLE", "任务尚未完成或不能正式复审。")
        book = self.repository.ledger(ledger_id)
        bound = item.get("ledger_id") or item.get("review_ledger_id")
        if bound and bound != ledger_id:
            raise BillingError("LEDGER_ENTITY_CONFLICT", "请使用该任务所属的账本。")
        if item.get("ledger_id") and item.get("ledger_commit_status") != "committed":
            raise BillingError("REVIEW_NOT_AVAILABLE", "本次任务账本提交未完成，请重新处理后再复审。")
        manifest = load_json(self.folder(job_id) / "result/manifest.json")

        def artifact(name):
            path = self.folder(job_id) / "result" / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest["artifact_sha256"].get(name):
                raise BillingError("RESULT_INTEGRITY_FAILED", "复审来源文件校验未通过，请重新处理。")
            return load_json(path)

        from shipping_billing.domain.contracts import NormalizationReport
        from shipping_billing.extraction.contracts import ExtractionReport
        from shipping_billing.extraction.engine import digest
        from shipping_billing.reconciliation.matching import occurrence

        observed = [d for d in artifact("reconciliation.json")["decisions"] if d["record_id"] == record_id]
        normalized = NormalizationReport.model_validate(artifact("normalized.json"))
        extraction = ExtractionReport.model_validate(artifact("extraction.json"))
        hashes = {f["source_id"]: f["sha256"] for f in manifest["files"] if f.get("sha256")}
        lines = {line.source_line_id: line for line in normalized.lines}
        occurrences = []
        for decision in observed:
            line = lines[decision["source_line_id"]].model_copy(deep=True)
            line.financial_attributes.accounting_entity = book["entity"]
            physical, _ = occurrence(line, extraction, hashes)
            incoming = deepcopy(decision["observed"])
            incoming["financial_attributes"]["accounting_entity"] = book["entity"]
            if physical:
                occurrences.append(dict(key=physical, observed_sha256=digest(incoming)))
        suggested = deepcopy(original)
        if len(observed) == 1:
            for group, values in observed[0]["observed"].items():
                if isinstance(values, dict):
                    suggested.setdefault(group, {}).update({k: v for k, v in values.items() if v is not None})
        new_bill = deepcopy(observed[0]["observed"]) if observed else deepcopy(original)
        checked = [dict(values=line.candidate.charge_details.model_dump(mode="python")) for line in normalized.lines
                   if line.source_line_id in {d["source_line_id"] for d in observed}
                   and any(c.code == "QUANTITY_PRICE" and c.status == "passed" for c in line.checks)]
        related = [field.model_dump(mode="python") for file in extraction.files for c in file.candidates
                   if c.source_line_id in {d["source_line_id"] for d in observed} for field in c.related_fields]
        return dict(job_id=job_id, original=original, suggested=suggested, new_bill=new_bill, observations=observed,
                    related_fields=related,
                    checked_lines=checked,
                    occurrences=occurrences, issues=original["audit_trail"]["issues"])

    def review_workspace(self, job_id, record_id, ledger_id):
        from shipping_billing.storage.review import form_values, visible_bill

        origin = self.review_origin(job_id, record_id, ledger_id)
        state, revision = self.repository.snapshot(ledger_id)
        book = self.repository.ledger(ledger_id)
        return dict(ledger={**book, "revision": revision}, record_id=record_id,
                    suggested=form_values(origin["suggested"]),
                    create_suggested=form_values(origin["new_bill"]),
                    targets=[visible_bill(b.model_dump(mode="python", exclude_unset=True)) for b in state.bills],
                    candidate_ids=sorted({rid for d in origin["observations"] for rid in d["matched_record_ids"]}),
                    existing_record_id=origin["original"].get("existing_record_id"),
                    observations=[dict(source_id=d["source_id"], amount_display=display(
                        d["observed"].get("charge_details", {}).get("amount"))) for d in origin["observations"]],
                    related_fields=origin["related_fields"],
                    decision=self.repository.get_decisions(job_id).get(record_id))

    def original_file(self, job_id, source_id):
        result = self.result(job_id)
        source = next((f for f in result["files"] if f["source_id"] == source_id), None)
        if source is None or not re.fullmatch(r"[a-f0-9]{64}", source.get("sha256") or ""):
            raise BillingError("SOURCE_NOT_FOUND", "该来源没有可下载的原文件。")
        for folder in (self.inputs / job_id, self.folder(job_id) / "result/work/sources"):
            folder = native_path(folder)
            for path in folder.glob("*"):
                if path.is_file() and path.resolve().is_relative_to(folder.resolve()) and hashlib.sha256(
                    path.read_bytes()
                ).hexdigest() == source["sha256"]:
                    return path, file_name(source["original_name"])
        raise BillingError("SOURCE_NOT_FOUND", "原文件已移走或校验不一致，请重新提供材料。")

    def review(self, job_id, record_id, status, note):
        if status not in ("unreviewed", "checked", "needs_followup") or len(note) > 2000:
            raise BillingError("INVALID_REVIEW", "核对标记或备注无效，备注最多 2000 字。")
        if record_id not in {r["record_id"] for r in self.result(job_id)["records"]}:
            raise BillingError("RECORD_NOT_FOUND", "任务中不存在这条账单。")
        with self.lock:
            path = self.folder(job_id) / "reviews.json"
            reviews = load_json(path) if path.exists() else {}
            reviews[record_id] = dict(status=status, note=note, updated_at=now())
            if self.repository:
                self.repository.save_review(job_id, record_id, reviews[record_id])
            write_json(path, reviews)
        return reviews[record_id]

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)
        if self.repository:
            self.repository.close()
