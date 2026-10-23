# 航运智能账单助手

基于 Python 和 LangChain，解析航运邮件及附件中的费用信息，匹配已有账单，输出经过校验的结构化 JSON。

当前状态：阶段 4 结构化候选提取已通过组件验收，真实材料 7/7 条费用保留，15 个合成片段验证案例中的 18/18 条费用正确检出。支持逐字段证据、原始响应归档、有限结构修复与精确输入回放，见 [阶段 4 验收记录](docs/stage4-report.md)。多格式解析已完成；OCR 质量问题继续跟踪。财务规则、旧账单匹配和最终账单工作流尚待后续阶段，`run` 返回未实现。样本基准未经过独立业务人员签核。

## 项目位置

- 项目根目录：`D:\Desktop\笔试项目\Check`
- 远程仓库：[FanRich1/Check](https://github.com/FanRich1/Check)
- [原始项目要求](https://ks.wjx.com/vm/h46aF6R.aspx)
- 现有真实样本保留在项目的上一级目录，未纳入版本控制。

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
| 4 | 工作流未实现，不生成伪造账单结果 |

`parse` 命令另有明确语义：0 全部来源解析完成，1 含复核项（离线 PDF/图片因 OCR 未运行也属于此类），3 有来源解析错误；每个来源仍登记并写出报告。live 模式通过显式 `--cloudflared` 或 `--ocr-map` 启用文件传入，replay 模式需要按页匹配内容哈希的归档映射。完整示例见 [阶段 3 记录](docs/stage3-report.md)。

`extract`：0 提取完成且无复核阻塞，1 含复核项，3 有来源提取错误，2 参数/配置错误。成功无费用、复核和调用失败分别登记；失败不会写成成功空数组。候选尚非最终账单。模型输出预算默认 4096 tokens，已有 `.env` 可设置 `BILLING_MODEL_MAX_TOKENS=4096`。超出输入上限或输出截断会明确失败。

错误消息和日志不输出请求正文、API 密钥或 Pydantic 原始输入。完整参数见 `--help`。原始数据、环境文件、虚拟环境和运行报告均被 Git 忽略；仓库只分发合成资料和结构 schema。
