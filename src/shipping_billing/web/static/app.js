"use strict";

const $ = (id) => document.getElementById(id);
const S = {config: null, files: [], jobs: [], jobId: null, result: null, baseJob: null, operation: "all", search: "", timer: null, historyTimer: null, recordId: null, submitting: false, ledgers: [], ledgerId: "", entityDraft: ""};
const operationNames = {CREATE: "新增", UPDATE: "修改", NO_CHANGE: "无变化", PENDING_REVIEW: "待复核", REJECTED: "已拒绝"};
const statusNames = {queued: "等待处理", running: "处理中", completed: "已完成", processed: "已处理", pending_review: "待复核", error: "来源异常", failed: "处理失败", interrupted: "已中断", no_charge: "无费用"};
const reviewNames = {unreviewed: "待核对", checked: "已核对", needs_followup: "需补材料"};
const fieldNames = {"/charge_details/charge_item": "标准费用名称", "/charge_details/charge_item_raw": "原始费用名称", "/charge_details/amount": "费用金额", "/charge_details/currency": "币种", "/charge_details/payment_term": "付款方式", "/document_info/reference_no": "单据编号", "/document_info/document_type": "单据类型", "/document_info/document_date": "单据日期", "/transaction_party/issuer": "签发方", "/transaction_party/recipient": "接收方", "/financial_attributes/accounting_entity": "记账主体", "/financial_attributes/posting_type": "记账方向"};
const stages = ["parse", "extract", "intents", "reconcile", "write"];
const allowed = new Set(["eml", "pdf", "png", "jpg", "jpeg", "tif", "tiff", "xls", "xlsx", "doc", "docx"]);

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}
function show(id, visible = true) { $(id).hidden = !visible; }
function clear(id) { $(id).replaceChildren(); }
function toast(message) {
  $("toast").textContent = message;
  show("toast");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => show("toast", false), 4500);
}
function dateText(value) {
  return new Date(value).toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false});
}
function sizeText(size) { return size >= 1048576 ? `${(size / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`; }
function pill(status, text) { return node("span", `pill ${status}`, text || operationNames[status] || statusNames[status] || status); }
function originalLink(file, jobId = S.jobId) {
  const link = node("a", "text-button", `下载原件：${file.original_name} ↗`);
  link.href = `/api/jobs/${jobId}/sources/${encodeURIComponent(file.source_id)}/download`;
  link.setAttribute("download", "");
  return link;
}
function decisionOperation(record) {
  const decision = S.result?.review_decisions?.[record.record_id];
  return decision ? {create: "CREATE", update: "UPDATE", duplicate: "NO_CHANGE", reject: "REJECTED"}[decision.action] : record.operation;
}
function displayedRecord(record) {
  const decision = S.result?.review_decisions?.[record.record_id];
  return decision ? {...record, ...(decision.display_bill || decision.after || {}), record_id: record.record_id, operation: decisionOperation(record)} : record;
}
function busy(value) {
  S.submitting = value;
  $("submit-button").disabled = value || !S.config?.model_ready;
  $("demo-button").disabled = value || !S.config?.demo_ready;
  $("submit-button").firstChild.textContent = value ? "正在创建任务…" : "开始处理材料";
}
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {...options, headers: {"X-Workspace-Token": S.config?.token || "", ...options.headers}});
  } catch {
    throw new Error("无法连接本地工作台。请双击「启动账单工作台.cmd」并保持启动窗口打开，然后刷新页面。若刚才已提交，请先检查历史任务，避免重复处理。");
  }
  let data;
  try { data = await response.json(); } catch { throw new Error("服务没有返回有效结果，请检查工作台是否仍在运行。"); }
  if (!response.ok) throw new Error(data.error?.message || data.detail || "请求未能完成，请重试。");
  return data;
}
async function refreshLedgers() {
  if (!S.config?.database_enabled) return;
  const data = await api("/api/ledgers");
  S.ledgers = data.ledgers;
  renderLedgerChoices();
}
function renderLedgerChoices() {
  const select = $("ledger-select");
  select.replaceChildren(new Option(S.ledgers.length ? "选择已有账本，或点击下方新建" : "暂无账本，请点击下方「新建账本」", ""));
  select.disabled = !S.ledgers.length;
  S.ledgers.forEach((book) => select.append(new Option(`${book.name} · ${book.bill_count} 条费用`, book.id)));
  if (!S.ledgers.some((b) => b.id === S.ledgerId)) S.ledgerId = "";
  select.value = S.ledgerId;
  selectLedger();
}
function selectLedger() {
  const entity = $("entity");
  if (!entity.readOnly) S.entityDraft = entity.value;
  S.ledgerId = $("ledger-select").value;
  const book = S.ledgers.find((b) => b.id === S.ledgerId);
  entity.readOnly = Boolean(book);
  entity.value = book ? book.entity : S.entityDraft;
  $("entity-help").textContent = book
    ? "记账主体由所选账本确定。更换公司请切换账本或点击「新建账本」。按该主体判断应收（AR）或应付（AP）。"
    : "填写公司完整名称，再点击上方「新建账本」，名称会自动带入；已有账本请直接选择。";
  $("ledger-summary").textContent = book ? `${book.entity} · ${book.bill_count} 条有效费用 · 版本 ${book.revision}` : "新建或选择账本后，系统自动匹配该账本中的历史费用。";
  $("manage-ledger-button").disabled = !book;
  $("new-ledger-button").className = book ? "text-button" : "button primary compact";
  $("ledger-next-step").textContent = book
    ? (S.files.length ? "账本已选好。点击下方「开始处理材料」即可，无需导入旧账单。" : "账本已选好。下一步：在左侧添加 PDF、图片或邮件等材料。")
    : (S.ledgers.length ? "下一步：选择已有账本；首次为这家公司记账时，点击「新建账本」。" : "首次使用：点击「新建账本」，填写公司名称，创建后即可上传材料。无需准备旧账单。");
  localStorage.setItem("billing-ledger-id", S.ledgerId);
}
function ledgerError(message) {
  $("ledger-error").textContent = message;
  show("ledger-error", Boolean(message));
}
async function managedLedger() {
  ledgerError("");
  const book = S.ledgers.find((b) => b.id === S.ledgerId);
  $("managed-ledger-summary").textContent = book ? `${book.name} · ${book.entity} · 版本 ${book.revision}` : "请先在工作台选择账本。";
  $("export-ledger-button").disabled = !book;
  updateImportAvailability();
  $("ledger-bills").replaceChildren();
  if (!book) return;
  const data = await api(`/api/ledgers/${book.id}`);
  if (!data.bills.length) $("ledger-bills").textContent = "账本已就绪，目前还没有有效费用。返回上传材料即可开始；待复核费用保存在各次任务中。";
  data.bills.forEach((bill) => $("ledger-bills").append(node("p", "", `${bill.document_info.reference_no || "无单据编号"} · ${bill.charge_details.charge_item || "费用待确认"} · ${bill.amount_display} ${bill.charge_details.currency || ""}`)));
}
function updateImportAvailability() {
  const book = S.ledgers.find((b) => b.id === S.ledgerId);
  const file = $("import-ledger-file").files[0];
  $("import-ledger-file").disabled = !book || book.revision !== 0;
  $("import-ledger-button").disabled = !book || book.revision !== 0 || !file;
  $("ledger-import-help").textContent = !book ? "请先选择账本。" : book.revision !== 0
    ? "当前账本已有处理记录，不能导入覆盖。后续账单请在工作台上传，系统会自动匹配。"
    : file ? `已选择 ${file.name}，确认后点击导入。` : "选择 JSON 文件后才能导入；没有旧账单可直接返回上传材料。";
}
function openCreateLedger() {
  $("create-ledger-form").reset();
  $("new-ledger-entity").value = $("entity").value;
  show("create-ledger-error", false);
  $("create-ledger-dialog").showModal();
  $("new-ledger-entity").focus();
}
function view(name) {
  for (const key of ["new", "history", "result"]) show(`${key}-view`, key === name);
  $("nav-new").classList.toggle("active", name !== "history");
  $("nav-history").classList.toggle("active", name === "history");
  $("breadcrumb-title").textContent = {new: "处理工作台", history: "历史任务", result: "处理结果"}[name];
  if (name !== "result") {
    clearTimeout(S.timer); S.jobId = null;
    if (S.config) refreshJobs().catch((error) => toast(error.message));
  } else { clearTimeout(S.historyTimer); }
  window.scrollTo({top: 0, behavior: "instant"});
}
function formError(message) {
  $("form-error").textContent = message;
  show("form-error", Boolean(message));
}
function addFiles(files) {
  formError("");
  let error = "";
  for (const file of files) {
    const extension = file.name.split(".").pop().toLowerCase();
    if (!allowed.has(extension)) { error = `「${file.name}」不是支持的业务文件。旧账单 JSON 请在右侧单独选择。`; continue; }
    if (!file.size || file.size > 20 * 1048576) { error = `「${file.name}」为空或超过 20 MB。`; continue; }
    if (S.files.some((f) => f.name === file.name && f.size === file.size && f.lastModified === file.lastModified)) continue;
    if (S.files.length >= 20 || S.files.reduce((n, f) => n + f.size, 0) + file.size > 60 * 1048576) { error = "每次最多 20 个文件，合计不超过 60 MB。"; break; }
    S.files.push(file);
  }
  renderFiles();
  if (error) formError(error);
}
function renderFiles() {
  clear("selected-files");
  S.files.forEach((file, index) => {
    const row = node("div", "selected-file");
    const remove = node("button", "remove-file", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `移除 ${file.name}`);
    remove.addEventListener("click", () => { S.files.splice(index, 1); renderFiles(); });
    row.append(node("strong", "", file.name.split(".").pop().toUpperCase()), node("span", "", file.name), node("small", "", sizeText(file.size)), remove);
    $("selected-files").append(row);
  });
  if (S.config?.database_enabled) selectLedger();
}
function renderBase() {
  show("base-job-banner", Boolean(S.baseJob));
  $("base-job-title").textContent = S.baseJob?.title || "";
  $("old-state").disabled = Boolean(S.config?.database_enabled || S.baseJob);
}
function renderJobs() {
  $("history-count").textContent = S.jobs.length;
  for (const [id, jobs] of [["recent-jobs", S.jobs.slice(0, 4)], ["history-jobs", S.jobs]]) {
    clear(id);
    if (!jobs.length) {
      $(id).append(node("div", "empty-state", "还没有处理记录。上传第一份材料，或先体验一个示例。"));
      continue;
    }
    jobs.forEach((job) => {
      const button = node("button", "job-card");
      button.type = "button";
      const content = node("span", "job-card-main");
      content.append(node("strong", "", `${job.demo ? "[示例] " : ""}${job.title}`), node("small", "", `${job.entity} · ${dateText(job.created_at)}`));
      button.append(node("span", "job-file-icon", "▤"), content, pill(job.status, job.review_resolved_count && !job.review_pending_count ? "已复审" : undefined), node("span", "chevron", "›"));
      button.addEventListener("click", () => openJob(job.id));
      $(id).append(button);
    });
  }
}
async function refreshJobs() {
  clearTimeout(S.historyTimer);
  const data = await api("/api/jobs");
  S.jobs = data.jobs;
  renderJobs();
  if (!S.jobId && S.jobs.some((job) => ["queued", "running"].includes(job.status))) {
    S.historyTimer = setTimeout(() => refreshJobs().catch((error) => toast(error.message)), 2500);
  }
}
async function openJob(id) {
  clearTimeout(S.timer);
  S.jobId = id;
  S.operation = "all";
  S.search = "";
  $("record-search").value = "";
  S.result = null;
  show("result-content", false);
  show("result-banner", false);
  show("progress-card", true);
  $("progress-title").textContent = "正在读取任务";
  $("result-title").textContent = "处理结果";
  show("continue-button", false);
  view("result");
  await loadJob(id);
}
async function loadJob(id) {
  try {
    const data = await api(`/api/jobs/${id}`);
    if (S.jobId !== id) return;
    S.result = data;
    renderResult();
    if (["queued", "running"].includes(data.job.status)) {
      S.timer = setTimeout(() => loadJob(id), 1200);
    } else {
      await refreshJobs();
      if (S.config.database_enabled) await refreshLedgers();
    }
  } catch (error) {
    if (S.jobId !== id) return;
    toast(error.message);
    S.timer = setTimeout(() => loadJob(id), 5000);
  }
}
function renderResult() {
  const data = S.result;
  const job = data.job;
  const active = ["queued", "running"].includes(job.status);
  $("result-title").textContent = `${job.demo ? "示例 · " : ""}${job.title}`;
  $("result-meta").textContent = `${job.entity} · ${dateText(job.created_at)} · ${job.input_files.length} 个上传文件${job.demo ? " · 已归档模型响应回放，不调用在线服务" : ""}`;
  if (job.ledger_commit_status === "committed" && !job.review_resolved_count) $("result-meta").textContent += ` · 已同步账本版本 ${job.ledger_revision} · 共 ${job.ledger_bill_count} 条有效费用`;
  if (job.review_resolved_count) $("result-meta").textContent += ` · 最近复审账本版本 ${job.review_latest_revision} · 当时共 ${job.review_bill_count} 条有效费用`;
  if (job.review_resolved_count) $("result-meta").textContent += ` · 已复审 ${job.review_resolved_count} 条 · 尚待复审 ${job.review_pending_count} 条`;
  if (job.ledger_id && !active && job.ledger_commit_status !== "committed") $("result-meta").textContent += " · 本次结果未入账，以下为处理建议";
  show("progress-card", active);
  $("progress-title").textContent = job.message;
  const stageIndex = stages.indexOf(job.stage);
  for (const step of $("progress-steps").children) {
    const index = stages.indexOf(step.dataset.stage);
    step.classList.toggle("done", index < stageIndex);
    step.classList.toggle("active", index === stageIndex);
  }
  show("result-banner", !active);
  $("result-banner").textContent = `${job.message}${job.error_code ? `（${job.error_code}）` : ""}`;
  if (job.review_resolved_count && !job.review_pending_count) $("result-banner").textContent = "本次已识别的待复核费用均已处理。可下载当前有效账本；原始识别结果和来源异常仍保留供核对。";
  $("result-banner").className = `notice ${["error", "failed", "interrupted"].includes(job.status) ? "error" : job.status === "pending_review" ? "warning" : ""}`;
  show("result-content", data.downloads.includes("records.json"));
  show("continue-button", data.downloads.includes("state.json") && !job.demo && !active && (!S.config.database_enabled || Boolean(job.ledger_id || job.review_ledger_id)));
  show("download-current-ledger", Boolean(S.config.database_enabled && (job.ledger_id || job.review_ledger_id)));
  if (!data.downloads.includes("records.json")) return;
  clear("metrics");
  const descriptions = {CREATE: "明确的新费用", UPDATE: "仅应用授权变更", NO_CHANGE: "已存在或重复来源", PENDING_REVIEW: "需要进一步确认"};
  for (const operation of ["CREATE", "UPDATE", "NO_CHANGE", "PENDING_REVIEW"]) {
    // Main records are deduplicated; per-source decisions are counted separately in the manifest.
    const count = data.records.filter((r) => decisionOperation(r) === operation).length;
    const metric = node("div", "metric");
    const label = node("div", "metric-label", operationNames[operation]);
    label.append(node("span", "metric-dot"));
    metric.append(label, node("strong", "", count), node("small", "", descriptions[operation]));
    $("metrics").append(metric);
  }
  $("result-count").textContent = `${data.records.length} 条费用`;
  for (const option of $("download-kind").options) option.disabled = !data.downloads.includes(option.value);
  if (!data.downloads.includes($("download-kind").value)) $("download-kind").value = "records.json";
  renderRecords();
  renderSources();
}
function renderRecords() {
  if (!S.result) return;
  clear("record-rows");
  for (const button of $("operation-filters").children) button.classList.toggle("selected", button.dataset.operation === S.operation);
  const query = S.search.trim().toLocaleLowerCase();
  const records = S.result.records.map(displayedRecord).filter((row) => (S.operation === "all" || row.operation === S.operation) && (!query || [row.charge_details.charge_item, row.document_info.reference_no, row.transaction_party.issuer, row.transaction_party.recipient].some((v) => String(v || "").toLocaleLowerCase().includes(query))));
  show("records-empty", !records.length);
  if (!S.result.records.length) $("records-empty").textContent = "本次没有可输出的费用。请查看下方来源文件中的识别结果及原因。";
  else $("records-empty").textContent = "没有符合条件的费用。";
  records.forEach((record) => {
    const row = node("tr");
    const name = node("td");
    name.append(node("strong", "", record.charge_details.charge_item || record.charge_details.charge_item_raw || "未确定费用名"), node("small", "", record.document_info.reference_no || "未提供单据编号"));
    const parties = node("td");
    parties.append(node("strong", "", record.transaction_party.issuer || "签发方待确认"), node("small", "", `→ ${record.transaction_party.recipient || "接收方待确认"}`));
    const amount = node("td", "number-cell");
    amount.append(node("strong", "amount", record.amount_display), node("small", "currency", record.charge_details.currency || "币种待确认"));
    const posting = node("td", `posting ${record.financial_attributes.posting_type === "AR" ? "ar" : ""}`, {AR: "应收 AR", AP: "应付 AP"}[record.financial_attributes.posting_type] || "待确认");
    const operation = node("td");
    operation.append(pill(record.operation));
    if (S.result.review_decisions?.[record.record_id]) operation.append(node("small", "", "人工复审决定"));
    const review = S.result.reviews[record.record_id]?.status || "unreviewed";
    const resolved = Boolean(S.result.review_decisions?.[record.record_id]);
    const reviewCell = node("td", `review-mark ${resolved ? "checked" : review}`, resolved ? "已正式复审" : reviewNames[review]);
    const actions = node("td");
    const details = node("button", "text-button", S.result.review_decisions?.[record.record_id] ? "查看复审 / 依据 ↗" : "查看依据 ↗");
    details.addEventListener("click", () => openDetail(record.record_id));
    actions.append(details);
    row.append(name, parties, amount, posting, operation, reviewCell, actions);
    $("record-rows").append(row);
  });
}
function renderSources() {
  clear("source-files");
  $("source-count").textContent = `${S.result.files.length} 个来源（含附件）`;
  for (const file of S.result.files) {
    const row = node("div", "source-row");
    const info = node("div", "source-info");
    info.append(node("strong", "", file.original_name), node("small", "", `${file.detected_format.toUpperCase()} · ${file.source_id}`));
    if (file.sha256 && !S.result.job.demo) {
      info.append(originalLink(file));
    }
    const seen = new Set();
    for (const issue of file.issues) {
      if (seen.has(issue.code)) continue;
      seen.add(issue.code);
      const problem = node("div", "source-issue", issue.explanation);
      problem.append(node("code", "", issue.code));
      info.append(problem);
    }
    row.append(info, pill(file.status));
    $("source-files").append(row);
  }
}
function field(label, value) {
  const item = node("div", "detail-field");
  item.append(node("small", "", label), node("strong", "", value === null || value === undefined || value === "" ? "—" : value));
  return item;
}
function section(title) {
  const item = node("section", "detail-section");
  item.append(node("h3", "", title));
  return item;
}
function locatorText(locator) {
  return [locator.page ? `第 ${locator.page} 页` : null, locator.sheet ? `工作表 ${locator.sheet}` : null, locator.cells, locator.block, locator.region, locator.section, locator.pointer].filter(Boolean).join(" · ");
}
function openDetail(id) {
  const record = S.result.records.find((r) => r.record_id === id);
  if (!record) return;
  S.recordId = id;
  $("detail-title").textContent = record.charge_details.charge_item || "费用详情";
  clear("detail-body");
  const grid = node("div", "detail-grid");
  grid.append(field("本次操作", operationNames[record.operation]), field("金额 / 币种", `${record.amount_display} ${record.charge_details.currency || "币种待确认"}`), field("原始费用名称", record.charge_details.charge_item_raw), field("单据编号", record.document_info.reference_no), field("签发方", record.transaction_party.issuer), field("接收方", record.transaction_party.recipient), field("付款描述", record.charge_details.payment_term_raw || record.charge_details.payment_term), field("数量 / 单价", `${record.quantity_display} / ${record.unit_price_display}`), field("记账主体", record.financial_attributes.accounting_entity), field("记账方向", {AP: "应付 AP", AR: "应收 AR"}[record.financial_attributes.posting_type] || "待确认"));
  $("detail-body").append(grid);
  const sourceIds = new Set(record.observations_display.map((item) => item.source_id));
  const originals = S.result.files.filter((file) => sourceIds.has(file.source_id) && file.sha256);
  if (originals.length && !S.result.job.demo) {
    const sources = section("原始材料");
    originals.forEach((file) => { const line = node("p"); line.append(originalLink(file)); sources.append(line); });
    $("detail-body").append(sources);
  }
  if (record.issues.length) {
    const issues = section("需要关注的内容");
    for (const issue of record.issues) {
      const item = node("div", "detail-issue", issue.explanation);
      item.append(node("small", "", `${issue.code}${issue.field ? ` · ${fieldNames[issue.field] || issue.field}` : ""}`));
      issues.append(item);
    }
    $("detail-body").append(issues);
  }
  if (record.operation === "PENDING_REVIEW" && record.existing_record_id) {
    const observations = section("本次观察与旧账单");
    observations.append(node("p", "", "上方显示保留的旧账单值，本次材料中的观察值如下；待复核修改尚未应用。"));
    record.observations_display.forEach((o) => observations.append(node("p", "", `材料金额：${o.amount} · ${o.source_id}`)));
    $("detail-body").append(observations);
  }
  const changes = section("本次实际应用的变更");
  if (!record.changes_display.length) changes.append(node("p", "", "没有已应用的字段变更。"));
  for (const change of record.changes_display) {
    const item = node("div", "change-row");
    item.append(node("small", "", fieldNames[change.path] || change.path), node("span", "change-old", change.old), node("span", "", "→"), node("span", "change-new", change.new));
    changes.append(item);
  }
  $("detail-body").append(changes);
  const evidence = section("原文依据");
  let evidenceCount = 0;
  for (const [path, refs] of Object.entries(record.audit_trail.field_evidence)) {
    if (!refs.length) continue;
    for (const ref of refs) {
      const item = node("div", "evidence-item");
      item.append(node("strong", "", fieldNames[path] || path), node("small", "", `${ref.source_id} · ${locatorText(ref.locator)}`), node("blockquote", "", ref.quote));
      evidence.append(item);
      evidenceCount++;
    }
  }
  if (!evidenceCount) evidence.append(node("p", "", "这条记录没有可定位证据，请对照原始材料核对。"));
  $("detail-body").append(evidence);
  const decision = S.result.review_decisions?.[id];
  show("manual-review-button", Boolean(S.config.database_enabled && !S.result.job.demo && record.operation === "PENDING_REVIEW" && !decision));
  if (decision) {
    const summary = section("正式人工复审记录");
    summary.append(node("p", "", `${decision.action_label} · ${decision.reviewer} · ${dateText(decision.decided_at)} · 账本版本 ${decision.ledger_revision}`), node("p", "", decision.reason));
    if (decision.after) summary.append(node("p", "", `正式账单：${decision.after.charge_details.charge_item} · ${decision.after.amount_display} ${decision.after.charge_details.currency}`));
    else if (decision.display_bill) summary.append(node("p", "", `拒绝的材料费用：${decision.display_bill.charge_details.charge_item || "费用待确认"} · ${decision.display_bill.amount_display} ${decision.display_bill.charge_details.currency || "币种待确认"}`));
    for (const change of decision.changes) summary.append(node("p", "", `${fieldNames[change.path] || change.path}：${change.old_display} → ${change.new_display}`));
    summary.append(node("p", "", "上方为原始识别或当时保留的旧值；正式决定已另存，原始材料及结果未改写。"));
    $("detail-body").append(summary);
  }
  const review = S.result.reviews[id] || {};
  $("review-status").value = review.status || "unreviewed";
  $("review-note").value = review.note || "";
  $("detail-dialog").showModal();
}
async function downloadResult() {
  const name = $("download-kind").value;
  const response = await fetch(`/api/jobs/${S.jobId}/download/${encodeURIComponent(name)}`);
  if (!response.ok) { toast("下载未能完成，请刷新任务后重试。"); return; }
  const url = URL.createObjectURL(await response.blob());
  const link = node("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

$("nav-new").addEventListener("click", () => view("new"));
$("nav-history").addEventListener("click", () => view("history"));
$("show-history").addEventListener("click", () => $("nav-history").click());
$("history-new").addEventListener("click", () => view("new"));
$("back-workspace").addEventListener("click", () => view("new"));
$("nav-help").addEventListener("click", () => $("help-dialog").showModal());
$("close-help").addEventListener("click", () => $("help-dialog").close());
$("close-detail").addEventListener("click", () => $("detail-dialog").close());
$("drop-zone").addEventListener("click", () => $("document-input").click());
$("drop-zone").addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); $("document-input").click(); } });
$("document-input").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
for (const name of ["dragenter", "dragover"]) $("drop-zone").addEventListener(name, (event) => { event.preventDefault(); $("drop-zone").classList.add("drag-over"); });
for (const name of ["dragleave", "drop"]) $("drop-zone").addEventListener(name, (event) => { event.preventDefault(); $("drop-zone").classList.remove("drag-over"); });
$("drop-zone").addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
$("old-state").addEventListener("change", () => { $("old-state-name").textContent = $("old-state").files[0]?.name || "选择旧账单或账本 JSON"; show("clear-old", Boolean($("old-state").files.length)); });
$("clear-old").addEventListener("click", () => { $("old-state").value = ""; $("old-state").dispatchEvent(new Event("change")); });
$("clear-base").addEventListener("click", () => { S.baseJob = null; renderBase(); });
$("temporary-ocr").addEventListener("change", () => { if ($("temporary-ocr").checked) $("ocr-map").value = ""; });
$("ocr-map").addEventListener("change", () => { if ($("ocr-map").files.length) $("temporary-ocr").checked = false; });
$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (S.submitting) return;
  formError("");
  if (!S.files.length) { formError("请先添加至少一个业务文件。"); return; }
  if (S.config.database_enabled && !S.ledgerId) {
    formError("材料已保留。请先选择已有账本，或新建账本后再点击「开始处理材料」。");
    if (!S.ledgers.length || $("entity").value.trim()) openCreateLedger();
    else $("ledger-select").focus();
    return;
  }
  if (!$("upload-form").reportValidity()) return;
  const form = new FormData();
  S.files.forEach((file) => form.append("documents", file));
  form.append("entity", $("entity").value.trim());
  if (S.config.database_enabled) form.append("ledger_id", S.ledgerId);
  form.append("temporary_ocr", String($("temporary-ocr").checked));
  form.append("local_ocr_enabled", String($("local-ocr").checked));
  if (S.baseJob) form.append("base_job", S.baseJob.id);
  for (const [input, key] of [["old-state", "old_state"], ["context-file", "context"], ["ocr-map", "ocr_map"]]) {
    if ($(input).files.length && !$(input).disabled) form.append(key, $(input).files[0]);
  }
  busy(true);
  try {
    const job = await api("/api/jobs", {method: "POST", body: form});
    localStorage.setItem("billing-entity", $("entity").value.trim());
    await openJob(job.id);
  } catch (error) { formError(error.message); }
  finally { busy(false); }
});
$("demo-button").addEventListener("click", async () => {
  if (S.submitting) return;
  busy(true);
  try { const job = await api("/api/demo", {method: "POST"}); await openJob(job.id); }
  catch (error) { toast(error.message); }
  finally { busy(false); }
});
$("result-refresh").addEventListener("click", () => { if (S.jobId) { clearTimeout(S.timer); loadJob(S.jobId); } });
$("continue-button").addEventListener("click", () => {
  S.baseJob = S.result.job;
  if (S.config.database_enabled && (S.result.job.ledger_id || S.result.job.review_ledger_id)) {
    S.ledgerId = S.result.job.ledger_id || S.result.job.review_ledger_id;
    S.baseJob = null;
    refreshLedgers().catch((error) => formError(error.message));
  }
  S.files = [];
  renderFiles();
  $("entity").value = S.result.job.entity;
  $("clear-old").click();
  renderBase();
  formError("");
  view("new");
  toast("已接续账本，添加下一批材料即可继续。");
});
$("operation-filters").addEventListener("click", (event) => { if (event.target.dataset.operation) { S.operation = event.target.dataset.operation; renderRecords(); } });
$("record-search").addEventListener("input", () => { S.search = $("record-search").value; renderRecords(); });
$("download-button").addEventListener("click", () => downloadResult().catch((error) => toast(error.message)));
$("review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const saved = await api(`/api/jobs/${S.jobId}/review`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({record_id: S.recordId, status: $("review-status").value, note: $("review-note").value})});
    S.result.reviews[S.recordId] = saved;
    S.result.downloads = [...new Set([...S.result.downloads, "reviews.json"])];
    renderResult();
    $("detail-dialog").close();
    toast("核对记录已保存。");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});

async function initialize() {
  busy(true);
  try {
    S.config = await api("/api/config");
    $("model-status").textContent = "";
    $("model-status").append(node("i"), document.createTextNode(S.config.model_ready ? `模型已配置 · ${S.config.model_name}` : "模型未配置"));
    $("model-status").classList.toggle("ready", S.config.model_ready);
    $("ocr-status").textContent = S.config.ocr_ready ? "✓ 临时识别工具已就绪" : "临时识别工具未配置，可在更多选项提供链接映射";
    $("ocr-status").classList.toggle("unready", !S.config.ocr_ready);
    $("temporary-ocr").disabled = !S.config.ocr_ready;
    $("local-ocr").disabled = !S.config.local_ocr_ready;
    $("local-ocr-status").textContent = S.config.local_ocr_ready ? "✓ 本地辅助识别已就绪" : "未安装；可按交付说明安装 local-ocr 可选依赖";
    if (!S.config.model_ready) {
      $("configuration-notice").className = "notice warning";
      $("configuration-notice").textContent = "尚未配置在线模型。可以先体验示例；处理自己的材料前，请在项目 .env 配置模型后重启工作台。";
      show("configuration-notice");
    }
    S.entityDraft = localStorage.getItem("billing-entity-draft") ?? localStorage.getItem("billing-entity") ?? "";
    $("entity").value = S.entityDraft;
    if (S.config.database_enabled) {
      show("database-ledger-fields");
      show("legacy-ledger-input", false);
      $("old-state").disabled = true;
      $("upload-form").noValidate = true;
      $("submit-note").textContent = "自动匹配所选账本的历史费用；只有通过校验的项目才会入账。";
      S.ledgerId = localStorage.getItem("billing-ledger-id") || "";
      await refreshLedgers();
    }
    await refreshJobs();
  } catch (error) {
    $("configuration-notice").className = "notice error";
    $("configuration-notice").textContent = `无法连接本地服务：${error.message}`;
    show("configuration-notice");
  } finally { busy(false); }
}
$("ledger-select").addEventListener("change", selectLedger);
$("entity").addEventListener("input", () => {
  if (!$("entity").readOnly) {
    S.entityDraft = $("entity").value;
    localStorage.setItem("billing-entity-draft", S.entityDraft);
  }
});
$("new-ledger-button").addEventListener("click", openCreateLedger);
$("close-create-ledger").addEventListener("click", () => $("create-ledger-dialog").close());
$("cancel-create-ledger").addEventListener("click", () => $("create-ledger-dialog").close());
$("manage-ledger-button").addEventListener("click", () => {
  $("import-ledger-file").value = "";
  $("ledger-import-options").open = false;
  managedLedger().catch((e) => ledgerError(e.message));
  $("ledger-dialog").showModal();
});
$("close-ledger").addEventListener("click", () => $("ledger-dialog").close());
$("return-to-upload").addEventListener("click", () => {
  $("ledger-dialog").close();
  (S.files.length ? $("submit-button") : $("drop-zone")).focus();
});
$("create-ledger-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("create-ledger-submit");
  button.disabled = true;
  $("close-create-ledger").disabled = true;
  $("cancel-create-ledger").disabled = true;
  show("create-ledger-error", false);
  try {
    const entity = $("new-ledger-entity").value.trim();
    if (!entity) throw new Error("请填写记账公司的完整名称。");
    const name = $("new-ledger-name").value.trim() || `${entity} · 业务账本`.slice(0, 200);
    const book = await api("/api/ledgers", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name, entity})});
    S.ledgerId = book.id;
    S.ledgers = [book, ...S.ledgers.filter((b) => b.id !== book.id)];
    renderLedgerChoices();
    $("create-ledger-dialog").close();
    formError("");
    (S.files.length ? $("submit-button") : $("drop-zone")).focus();
    toast(S.files.length ? "账本已创建并选用，材料已保留。点击「开始处理材料」即可。" : "账本已创建并选用。下一步：添加业务材料，无需导入旧账单。");
  } catch (error) {
    $("create-ledger-error").textContent = error.message;
    show("create-ledger-error");
  } finally {
    button.disabled = false;
    $("close-create-ledger").disabled = false;
    $("cancel-create-ledger").disabled = false;
  }
});
$("create-ledger-dialog").addEventListener("cancel", (event) => {
  if ($("create-ledger-submit").disabled) event.preventDefault();
});
$("import-ledger-file").addEventListener("change", () => { ledgerError(""); updateImportAvailability(); });
$("import-ledger-button").addEventListener("click", async () => {
  const book = S.ledgers.find((b) => b.id === S.ledgerId);
  const file = $("import-ledger-file").files[0];
  if (!book || !file || book.revision !== 0) { updateImportAvailability(); return; }
  const form = new FormData(); form.append("state", file); form.append("revision", String(book.revision));
  $("import-ledger-button").disabled = true;
  try { await api(`/api/ledgers/${book.id}/import`, {method: "POST", body: form}); await refreshLedgers(); await managedLedger(); toast("旧账单已导入，后续材料将自动与它匹配。"); }
  catch (error) { ledgerError(error.message); }
  finally { updateImportAvailability(); }
});
$("export-ledger-button").addEventListener("click", async () => {
  try {
    const response = await fetch(`/api/ledgers/${S.ledgerId}/export`);
    if (!response.ok) throw new Error("账本导出失败，请刷新后重试。");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a"); link.href = url; link.download = `ledger-${S.ledgerId}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (error) { ledgerError(error.message); }
});
initialize();
