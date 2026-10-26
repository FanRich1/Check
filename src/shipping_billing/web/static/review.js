"use strict";

const sourceKinds = {non_billing_image: "非账单图片", supporting_document: "支持材料", reopen: "用途仍待确认"};
function openSourceReview(file) {
  S.sourceReview = {jobId: S.jobId, file};
  $("source-review-form").reset();
  show("source-review-error", false);
  $("source-review-name").textContent = file.original_name;
  $("source-review-original").replaceChildren(originalLink(file));
  const image = ["png", "jpg", "tiff"].includes(file.detected_format);
  $("source-review-kind").options[0].disabled = !image;
  $("source-review-kind").value = file.assessment.review?.classification || (image ? "non_billing_image" : "supporting_document");
  $("source-reviewer").value = localStorage.getItem("billing-reviewer") || "";
  $("source-review-reason").value = "";
  const history = S.result.source_reviews?.[file.source_id] || [];
  $("source-review-history-items").replaceChildren();
  for (const item of history) $("source-review-history-items").append(node("p", "", `${sourceKinds[item.classification]} · ${item.reviewer} · ${dateText(item.decided_at)}\n${item.reason}`));
  show("source-review-history", history.length > 0);
  $("source-review-dialog").showModal();
}
$("close-source-review").addEventListener("click", () => $("source-review-dialog").close());
$("source-review-dialog").addEventListener("close", () => { S.sourceReview = null; });
$("source-review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const session = S.sourceReview;
  if (!session || session.submitting) return;
  show("source-review-error", false);
  const body = {source_id: session.file.source_id, sha256: session.file.sha256,
    classification: $("source-review-kind").value, revision: session.file.assessment.revision,
    reviewer: $("source-reviewer").value.trim(), reason: $("source-review-reason").value.trim(),
    confirmed: $("source-review-confirm").checked};
  session.submitting = true;
  $("source-review-form").inert = true;
  try {
    await api(`/api/jobs/${session.jobId}/source-review`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    localStorage.setItem("billing-reviewer", body.reviewer);
    if (S.sourceReview === session) $("source-review-dialog").close();
    await loadJob(session.jobId);
    toast("材料用途与确认记录已保存，费用及账本版本保持原值。");
  } catch (error) {
    if (S.sourceReview === session) { $("source-review-error").textContent = error.message; show("source-review-error"); }
  } finally { session.submitting = false; $("source-review-form").inert = false; }
});

const manualFields = [
  ["document-type", "/document_info/document_type", "单据类型", "text"],
  ["reference", "/document_info/reference_no", "单据编号", "text"],
  ["date", "/document_info/document_date", "单据日期", "date"],
  ["fee", "/charge_details/charge_item", "费用名称", "text"],
  ["amount", "/charge_details/amount", "费用金额", "text"],
  ["currency", "/charge_details/currency", "明确币种，例如 USD / CNY", "text"],
  ["payment", "/charge_details/payment_term", "付款方式，PP / CC 或留空", "text"],
  ["quantity", "/charge_details/quantity", "数量（原件未提供可留空）", "text"],
  ["unit-price", "/charge_details/unit_price", "单价（原件未提供可留空）", "text"],
  ["issuer", "/transaction_party/issuer", "单据签发方", "text"],
  ["recipient", "/transaction_party/recipient", "单据接收方", "text"],
];
for (const [id, path, title, type] of manualFields) {
  const label = node("label", "", title);
  const input = node("input", "text-input");
  input.id = `manual-field-${id}`; input.type = type; input.maxLength = 500;
  input.dataset.path = path;
  if (id === "amount") input.inputMode = "decimal";
  label.append(input); $("manual-fields").append(label);
}
function manualError(message) {
  $("manual-error").textContent = message; show("manual-error", Boolean(message));
}
function invalidateManualPreview() {
  if (S.manual) { S.manual.preview = null; S.manual.editVersion = (S.manual.editVersion || 0) + 1; }
  show("manual-preview", false); $("manual-confirm").checked = false; $("manual-approve").disabled = true;
}
function manualDirection() {
  const entity = S.manual?.workspace?.ledger.entity || "";
  const payer = $("manual-payer").value.trim(), payee = $("manual-payee").value.trim();
  const normalized = (value) => value.toLocaleLowerCase().replace(/\s+/g, " ").trim().replace(/\b(inc|ltd|llc|corp|co)\.$/, "$1");
  const type = payer && payee && normalized(payer) === normalized(payee) ? "付款方和收款方不能相同" : entity && normalized(entity) === normalized(payer) ? "应付 AP" : entity && normalized(entity) === normalized(payee) ? "应收 AR" : "请确认记账公司在交易中的付款或收款角色";
  $("manual-direction").textContent = `${entity} · ${type}`;
}
function populateManualFields() {
  invalidateManualPreview();
  const work = S.manual?.workspace;
  if (!work) return;
  const action = $("manual-action").value;
  const edits = ["create", "update"].includes(action), needsTarget = ["update", "duplicate"].includes(action);
  show("manual-fields", edits); show("manual-parties", edits); show("manual-direction", edits);
  show("manual-target-group", needsTarget); $("manual-target").required = needsTarget;
  show("manual-distinct-label", action === "create"); $("manual-distinct").required = action === "create";
  $("manual-distinct").checked = false;
  const target = work.targets.find((bill) => bill.record_id === $("manual-target").value);
  const base = action === "update" ? {...(target?.form_values || {})} : {...work.create_suggested};
  S.manual.baseline = base;
  const proposed = {...base};
  if (action === "update") for (const [path, value] of Object.entries(work.suggested)) if (value !== null) proposed[path] = value;
  for (const [id, path] of manualFields) {
    const input = $(`manual-field-${id}`);
    input.value = proposed[path] ?? ""; input.disabled = !edits;
    input.required = edits && ["fee", "amount", "currency"].includes(id);
  }
  for (const id of ["manual-payer", "manual-payee"]) { $(id).disabled = !edits; $(id).required = edits; }
  $("manual-payer").value = proposed["/transaction_party/recipient"] || "";
  $("manual-payee").value = proposed["/transaction_party/issuer"] || "";
  $("manual-related-fields").replaceChildren();
  if (work.related_fields?.length) $("manual-related-fields").append(relatedFieldsPanel(work.related_fields, edits));
  manualDirection();
}
async function loadManualWorkspace() {
  const session = S.manual;
  if (!session) return;
  const ledger = $("manual-ledger").value;
  const epoch = (session.epoch || 0) + 1; session.epoch = epoch;
  session.workspace = null; invalidateManualPreview(); manualError("");
  $("manual-preview-button").disabled = true;
  if (!ledger) { manualError("请先选择正式账本；没有账本时请返回工作台新建。"); return; }
  try {
    const work = await api(`/api/jobs/${session.jobId}/review-workspace?record_id=${encodeURIComponent(session.recordId)}&ledger_id=${ledger}`);
    if (S.manual !== session || session.epoch !== epoch) return;
    if (work.decision) throw new Error("这条费用已完成正式复审，请关闭后刷新任务查看。");
    session.workspace = work;
    $("manual-book-summary").textContent = `${work.ledger.name} · ${work.ledger.entity} · 当前版本 ${work.ledger.revision}`;
    $("manual-observations").replaceChildren(node("p", "", `原始识别对应 ${work.observations.length} 条来源观察；可能匹配 ${work.candidate_ids.length} 条旧账。`));
    work.observations.forEach((item) => $("manual-observations").append(node("p", "", `材料观察金额：${item.amount_display} · ${item.source_id}`)));
    const sourceIds = new Set(work.observations.map((item) => item.source_id));
    S.result.files.filter((file) => sourceIds.has(file.source_id) && file.sha256).forEach((file) => {
      const line = node("p"); line.append(originalLink(file, session.jobId)); $("manual-observations").append(line);
    });
    const original = S.result.records.find((record) => record.record_id === session.recordId);
    original?.issues.forEach((issue) => $("manual-observations").append(node("p", "", `${issue.explanation}（${issue.code}）`)));
    if (work.observations.length > 1) $("manual-observations").append(node("p", "", "本次决定将处理本条费用下的所有来源观察；若实际包含不同费用，请拆分材料后重新提交。"));
    const select = $("manual-target"); select.replaceChildren(new Option("请选择要保留或修改的旧账", ""));
    const targets = [...work.targets].sort((a, b) => Number(work.candidate_ids.includes(b.record_id)) - Number(work.candidate_ids.includes(a.record_id)));
    targets.forEach((bill) => select.append(new Option(`${bill.document_info.reference_no || "无单据编号"} · ${bill.charge_details.charge_item} · ${bill.amount_display} ${bill.charge_details.currency} · ${bill.record_id}`, bill.record_id)));
    select.value = work.existing_record_id || (work.candidate_ids.length === 1 ? work.candidate_ids[0] : "");
    $("manual-action").value = work.existing_record_id || work.candidate_ids.length ? "update" : "create";
    populateManualFields(); $("manual-preview-button").disabled = false;
  } catch (error) { if (S.manual === session && session.epoch === epoch) manualError(error.message); }
}
async function openManualReview() {
  if (!S.result || !S.recordId) return;
  const job = S.result.job;
  S.manual = {jobId: S.jobId, recordId: S.recordId, preview: null};
  const bound = job.ledger_id || job.review_ledger_id;
  $("manual-ledger").replaceChildren(new Option("选择正式账本", ""));
  S.ledgers.forEach((book) => $("manual-ledger").append(new Option(`${book.name} · ${book.entity}`, book.id)));
  $("manual-ledger").value = bound || S.ledgerId || ""; $("manual-ledger").disabled = Boolean(bound);
  $("manual-reviewer").value = localStorage.getItem("billing-reviewer") || "";
  $("manual-reason").value = $("review-note").value;
  invalidateManualPreview(); $("manual-dialog").showModal(); $("manual-dialog").scrollTop = 0;
  await loadManualWorkspace();
}
async function openManualRecord(recordId) {
  const record = S.result?.records.find((item) => item.record_id === recordId);
  if (!record || !canReview(record)) return;
  S.recordId = recordId;
  $("review-note").value = S.result.reviews[recordId]?.note || "";
  await openManualReview();
}
$("start-pending-review").addEventListener("click", () => {
  const record = S.result?.records.find(canReview);
  if (record) openManualRecord(record.record_id);
});
$("manual-review-button").addEventListener("click", openManualReview);
$("download-current-ledger").addEventListener("click", async () => {
  const ledger = S.result?.job.ledger_id || S.result?.job.review_ledger_id;
  if (!ledger) return;
  try {
    const response = await fetch(`/api/ledgers/${ledger}/export`);
    if (!response.ok) throw new Error("当前账本下载失败，请刷新后重试。");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a"); link.href = url; link.download = `ledger-${ledger}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (error) { toast(error.message); }
});
$("close-manual").addEventListener("click", () => $("manual-dialog").close());
$("manual-dialog").addEventListener("close", () => { S.manual = null; });
$("manual-ledger").addEventListener("change", loadManualWorkspace);
$("manual-action").addEventListener("change", populateManualFields);
$("manual-target").addEventListener("change", populateManualFields);
$("manual-form").addEventListener("input", (event) => {
  if (event.target.id === "manual-confirm") return;
  invalidateManualPreview(); manualDirection();
});
$("manual-confirm").addEventListener("change", () => { $("manual-approve").disabled = !$("manual-confirm").checked || !S.manual?.preview || S.manual.submitting; });
$("manual-form").addEventListener("submit", async (event) => {
  event.preventDefault(); manualError("");
  const session = S.manual;
  if (!session?.workspace) return;
  invalidateManualPreview();
  const action = $("manual-action").value;
  const fields = {};
  if (["create", "update"].includes(action)) for (const [id, path] of manualFields) {
    const value = $(`manual-field-${id}`).value.trim() || null;
    if (value !== (session.baseline[path] ?? null)) fields[path] = value;
  }
  const body = {record_id: session.recordId, ledger_id: session.workspace.ledger.id, revision: session.workspace.ledger.revision,
    action, target_record_id: ["update", "duplicate"].includes(action) ? $("manual-target").value || null : null,
    fields, payer: $("manual-payer").disabled ? "" : $("manual-payer").value.trim(), payee: $("manual-payee").disabled ? "" : $("manual-payee").value.trim(),
    reviewer: $("manual-reviewer").value.trim(), reason: $("manual-reason").value.trim(), distinct: action === "create" && $("manual-distinct").checked};
  $("manual-preview-button").disabled = true;
  const generation = session.editVersion, epoch = session.epoch;
  try {
    const plan = await api(`/api/jobs/${session.jobId}/review-preview`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    if (S.manual !== session || session.editVersion !== generation || session.epoch !== epoch) return;
    session.preview = {body, token: plan.preview_token};
    $("manual-preview-summary").textContent = `${plan.action_label} · 当前账本版本 ${plan.base_revision}${plan.after ? ` · ${plan.after.amount_display} ${plan.after.charge_details.currency} · ${plan.after.financial_attributes.posting_type}` : " · 不新增、不修改有效费用"}`;
    $("manual-changes").replaceChildren();
    for (const change of plan.changes) $("manual-changes").append(node("p", "", `${fieldNames[change.path] || change.path}：${change.old_display} → ${change.new_display}`));
    if (!plan.changes.length) $("manual-changes").append(node("p", "", "有效账单字段保持不变；复审决定和原因将正式保存。"));
    show("manual-preview"); $("manual-preview").scrollIntoView({block: "nearest"});
  } catch (error) { manualError(error.message); }
  finally { if (S.manual === session) $("manual-preview-button").disabled = false; }
});
$("manual-approve").addEventListener("click", async () => {
  const session = S.manual;
  if (!session?.preview || !$("manual-confirm").checked) return;
  $("manual-approve").disabled = true;
  session.submitting = true; $("manual-form").inert = true;
  const {body, token} = session.preview;
  try {
    await api(`/api/jobs/${session.jobId}/review-approve`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({...body, preview_token: token, confirmed: true})});
    localStorage.setItem("billing-reviewer", body.reviewer);
    if (S.manual === session) { $("manual-dialog").close(); $("detail-dialog").close(); }
    await loadJob(session.jobId);
    const posted = ["create", "update"].includes(body.action);
    toast(posted ? "复核已保存，确认后的费用和修改记录已写入数据库。" : "复核决定已保存，当前账本费用保持不变。");
  } catch (error) {
    if (S.manual === session) { manualError(error.message); invalidateManualPreview(); }
  }
  finally { session.submitting = false; $("manual-form").inert = false; }
});
