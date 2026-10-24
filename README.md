# 航运智能账单助手

基于 Python 和 LangChain，解析航运邮件及附件中的费用信息，匹配已有账单，输出经过校验的结构化 JSON。

当前版本：**stage9-v1 交付版**，提供本地网页、完整 CLI、锁文件安装器和带文件校验清单的 ZIP。安装、干净环境验收、真实材料结果及已知限制见 [阶段 9 交付记录](docs/stage9-report.md)。首次使用建议先读 [安装与操作说明](docs/delivery-guide.md)。

**新增 MySQL 账本支持**：网页选择账本后，自动读取已有费用，完成新增、重复检查和授权修改，并将有效结果事务入库。无需每批手动传递 JSON。本机已准备独立 MySQL；其他 Windows 环境可在安装 Python 依赖后运行 `安装MySQL账本.cmd`。账本管理、旧数据迁移和备份见 [数据库说明](docs/database.md)。阶段 9 ZIP 和报告代表此前冻结版本。

**本地网页**：首次双击 `安装运行环境.cmd`，然后双击 `启动账单工作台.cmd`。地址为 **http://127.0.0.1:8765**，可上传材料、查看费用及原文依据、保存核对备注和下载结果。无密钥时可体验合成示例。PDF/图片需指定 OCR 配置；本地辅助识别可独立启用。详见 [网页使用说明](docs/web-workspace.md)。

**评测边界**：历史阶段 8 在其冻结版本下完成三轮真实模型和留出验收，详见 [原始验收记录](docs/stage8-report.md)。OCR 为固定合成输入，标签未经独立业务签核；阶段 9 对当前代码做公开案例回归和真实样本检查。OCR 无原文、外部服务错误、交易方或币种不明仍保留错误/复核，不宣称任意真实文件都能自动记账。

## 项目位置

- 项目根目录：解压或克隆后包含 `pyproject.toml` 的 `Check` 文件夹。
- 远程仓库：[FanRich1/Check](https://github.com/FanRich1/Check)
- [原始项目要求](https://ks.wjx.com/vm/h46aF6R.aspx)
- 现有真实样本保留在项目的上一级目录，未纳入版本控制。
- [架构说明](docs/architecture.md)、[11 点业务与技术思考](docs/design-considerations.md)。

## 目录约定

```text
src/shipping_billing/    业务实现
tests/fixtures/          明确标注的模拟测试数据及预期结果
docs/                   实施计划、架构与验收说明
data/local/             本地输入、OCR 原始响应等数据（Git 忽略）
outputs/                运行结果及评测报告（Git 忽略）
```

## 实施顺序

1. 确定数据模型、现有样本基准答案与验收用例。
2. 验证指定 OCR 接口，完成多格式文档解析。
3. 用 LangChain 实现带来源证据的结构化提取。
4. 实现费用标准化、账单匹配、增量更新、AR/AP 判断和数据校验。
5. 完成自动评测、运行说明、补充思考与交付打包。

逐阶段工作、产出和放行条件见 [实施计划](docs/implementation-plan.md)，指标公式和数据集规则见 [评测规范](docs/evaluation.md)。

## 安装与环境

当前支持 Python 3.12，已在 Windows / Python 3.12.10 验证。以下命令从仓库根目录执行，无需激活环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install uv==0.13.5
.\.venv\Scripts\uv.exe sync --frozen --link-mode copy
.\.venv\Scripts\shipping-billing.exe --help
```

`uv.lock` 固定运行和开发依赖及分发哈希；构建依赖 setuptools 同样固定版本。首次安装需要包索引网络。已有 uv 时也可直接 `uv sync --frozen`。Mac/Linux 使用对应的 `.venv/bin/` 命令；这些平台尚未实测。

旧版 `.doc` 转换需要 Windows 桌面版 Microsoft Word；`.docx`、`.xls`、`.xlsx` 可由 Python 库直接读取。可选辅助文字识别安装命令为 `uv sync --frozen --extra local-ocr`；使用时显式加 `--local-ocr`，其输出与指定 OCR 区分保存。

## 已可运行的命令

```powershell
# 完整离线演示：真实模型历史响应回放，无密钥、无网络，依次新增、更新、重传
.\.venv\Scripts\python.exe -m scripts.stage7_demo --mode replay --storage tests/fixtures/pipeline/replay-cache --output outputs/demo-stage7

# 一次处理原始文件，结果、状态、审计及来源清单写入新的目录
.\.venv\Scripts\shipping-billing.exe run tests/fixtures/pipeline/inputs/new.eml --accounting-entity "Buyer Ltd" --storage tests/fixtures/pipeline/replay-cache --output outputs/run-new
# 保存完整 state；重传已应用更新的材料应得到 NO_CHANGE
.\.venv\Scripts\shipping-billing.exe run tests/fixtures/pipeline/inputs/amendment.eml --accounting-entity "Buyer Ltd" --state outputs/run-new/state.json --storage tests/fixtures/pipeline/replay-cache --output outputs/run-update
.\.venv\Scripts\shipping-billing.exe run tests/fixtures/pipeline/inputs/amendment.eml --accounting-entity "Buyer Ltd" --state outputs/run-update/state.json --storage tests/fixtures/pipeline/replay-cache --output outputs/run-repeat

# 默认离线，不需要密钥，不发送网络请求
.\.venv\Scripts\shipping-billing.exe config-check
.\.venv\Scripts\shipping-billing.exe validate request tests/fixtures/synthetic/DEV-001/request.json

# 解析文档为原文、表格、位置与风险标记；尚非最终账单 JSON
.\.venv\Scripts\shipping-billing.exe parse tests/fixtures/parsing/fees.xlsx --output outputs/parsed.json
.\.venv\Scripts\shipping-billing.exe parse "..\PDF+示例账单2.pdf" --output outputs/pdf-parsed.json

# 从解析报告提取费用候选；需要显式加载已配置的 DeepSeek 环境
.\.venv\Scripts\shipping-billing.exe --env-file .env extract outputs/pdf-parsed.json --mode live --storage data/local/extraction --output outputs/candidates.json
# 精确输入、相同提示词对应的模型响应回放，不需要密钥，不发模型请求
.\.venv\Scripts\shipping-billing.exe extract outputs/pdf-parsed.json --mode replay --storage data/local/extraction --output outputs/candidates-replay.json

# 阶段 5 离线规则：本例应输出 Handling Charge / USD 40 / PP / AP / ready
.\.venv\Scripts\shipping-billing.exe normalize tests/fixtures/domain/example-input.json --context tests/fixtures/domain/example-context.json --output outputs/normalized.json
# 实际提取报告可直接处理；记账主体需要显式指定，缺失时保留费用并复核
.\.venv\Scripts\shipping-billing.exe normalize outputs/candidates.json --accounting-entity "当前记账法人" --output outputs/normalized-real.json
.\.venv\Scripts\python.exe scripts/stage5_smoke.py --portable --output outputs/stage5/portable

# 阶段 6 离线更新示例：OLD1 金额 40→50，仅修改有原文指令的金额字段
.\.venv\Scripts\shipping-billing.exe reconcile tests/fixtures/reconciliation/example-normalized.json --extraction tests/fixtures/reconciliation/example-extraction.json --context tests/fixtures/reconciliation/example-context.json --state tests/fixtures/reconciliation/example-old-state.json --output outputs/reconciliation.json --records-output outputs/records.json --state-output outputs/next-state.json
# 应用新快照后重传应为 NO_CHANGE；输入及输出状态文件必须分开
.\.venv\Scripts\shipping-billing.exe reconcile tests/fixtures/reconciliation/example-normalized.json --extraction tests/fixtures/reconciliation/example-extraction.json --context tests/fixtures/reconciliation/example-context.json --state outputs/next-state.json --output outputs/reconciliation-repeat.json
.\.venv\Scripts\python.exe -m scripts.stage6_smoke --portable --output outputs/stage6/portable

# 仅测试提取组件：合成片段直接输入，不代表文件解析/OCR 验收
.\.venv\Scripts\python.exe scripts/stage4_validation.py --mode live --output outputs/stage4/validation-live
.\.venv\Scripts\python.exe scripts/stage4_validation.py --mode replay --output outputs/stage4/validation-replay

# 导出结构 schema；业务上的跨字段限制还需 Python 模型验证
.\.venv\Scripts\shipping-billing.exe schema records --output outputs/records.schema.json
.\.venv\Scripts\python.exe scripts/export_schemas.py

# 运行测试及 CLI 校准演示（使用人工编写式合成预测，非账单工作流预测）
.\.venv\Scripts\python.exe -m pytest --junitxml=outputs/stage3/pytest.xml
.\.venv\Scripts\python.exe scripts/stage1_smoke.py

# 真正比较时 --actual 必须是另行提供的 JSON 记录数组
.\.venv\Scripts\shipping-billing.exe evaluate --case tests/fixtures/synthetic/DEV-001 --actual outputs/stage1/smoke/authored-prediction.json --report outputs/stage1/comparison.json
```

`validate records <文件>` 检验结果数组，`--output <文件>` 可写规范化 JSON。金额内部为 Decimal，写出为精确 JSON 数字，不转成字符串或二进制 float。

最小 evaluate 按费用行顺序比较，不执行解析/OCR/提取，不应用旧账单快照，不是最终完整 harness。报告显示分子、分母、差异和 `pipeline_executed=false`；漏行及结构无效的预测不能被当作通过。详细边界见 [阶段 1 交付记录](docs/stage1-report.md)。

## 配置和退出码

配置环境变量使用 `BILLING_` 前缀，字段见 [.env.example](.env.example)。默认不自动加载当前目录的 `.env`；显式使用 `--env-file .env`。离线命令不需要密钥。

`config-check --mode online` 要求提供方、模型名和 API 密钥齐全，但只检查配置、不发请求，也不能证明服务可用。阶段 2 已增加 `probe-model` 和 `ocr` 真实调用入口，详见 [接入记录与 URL 映射用法](docs/stage2-report.md)。默认不上传本地文件。

| 退出码 | 含义 |
|---|---|
| 0 | 校验/配置通过或离线比较一致 |
| 1 | 离线比较不一致，包含无效结构的预测 |
| 2 | 输入/配置无效，文件不可读或命令参数错误 |
| 3 | 写出失败、在线服务/识别失败或内部错误 |
| 4 | 保留给尚未实现的入口；当前 run 已实现 |

`parse` 命令另有明确语义：0 全部来源解析完成，1 含复核项（离线 PDF/图片因 OCR 未运行也属于此类），3 有来源解析错误；每个来源仍登记并写出报告。live 模式通过显式 `--cloudflared` 或 `--ocr-map` 启用文件传入，replay 模式需要按页匹配内容哈希的归档映射。完整示例见 [阶段 3 记录](docs/stage3-report.md)。

`extract`：0 提取完成且无复核阻塞，1 含复核项，3 有来源提取错误，2 参数/配置错误。成功无费用、复核和调用失败分别登记；失败不会写成成功空数组。候选尚非最终账单。模型输出预算默认 4096 tokens，已有 `.env` 可设置 `BILLING_MODEL_MAX_TOKENS=4096`。超出输入上限或输出截断会明确失败。

`normalize` 不调用模型或 OCR，不需要密钥。0 领域检查完成、1 有复核项、2 输入/策略无效、3 上游来源错误或运行失败；结果始终保留每个来源和原始候选。`ready` 表示领域检查通过，尚不是 CREATE/UPDATE。`--context` 可提供别名、带证据的原账关系、舍入规则及明细范围明确的合计；不能与 `--accounting-entity` 同用。完整策略和边界见 [阶段 5 说明](docs/stage5-report.md)。

`reconcile` 同样离线运行。0 全部完成、1 含复核、2 输入/策略无效、3 上游来源错误或运行失败。必须提供阶段 5 报告及其原始提取报告，context 内的 normalization_context 必须完全一致。首次可用 `--existing-bills` 提供旧账数组，后续应保存并传入完整 `--state`，保留来源去重索引。`--records-output` 是核心结果数组，`--state-output` 是新业务快照及身份/审计，不原地修改旧账。详见 [操作示例](tests/fixtures/reconciliation/README.md)。

`run` 默认 replay，从原始文件重新解析并读取精确匹配的模型/OCR 归档，缓存缺失明确报错；不会自动转为在线调用。`--mode live` 配合 `--env-file .env` 启用真实 LangChain，PDF/图片还需显式 `--ocr-map` URL 映射或 `--cloudflared`；`--local-ocr` 是可选补充。`--mode offline` 不运行 OCR，模型仍只读回放，缺归档或未识别页面不会伪装成功。0 完成，1 含业务复核，2 输入/配置无效，3 含来源执行失败或程序错误；部分失败仍输出其他来源的结果。

目录输入递归选择 EML/PDF/JPG/PNG/TIFF/XLS/XLSX/DOC/DOCX，其他文件和排除目录登记在 discovery，附件按真实格式解析。最多选择 100 个顶层文件，解析仍受大小/页数/递归预算限制。`--context` 接收 ReconciliationContext，不能与 `--accounting-entity` 合用。输出目录应为新目录，防止覆盖旧状态。模型缓存按模型与规则版本隔离；可用 `--cache-profile` 选择历史命名空间。完整说明及在线示例见 [阶段 7 文档](docs/stage7-report.md)。

错误消息和日志不输出请求正文、API 密钥或 Pydantic 原始输入。完整参数见 `--help`。原始数据、环境文件、虚拟环境和运行报告均被 Git 忽略；仓库只分发合成资料和结构 schema。
