# 阶段 4：结构化费用候选提取

2026-10-23 完成组件验收。提取链使用 LangChain 和真实 DeepSeek `deepseek-flash`，提示词 `stage4-evidence-v7`，证据规则 `stage4-evidence-checks-v1`。本阶段交付候选 JSON；标准化、金额勾稽、AR/AP、旧账单匹配与最终操作留待阶段 5–7。`run` 仍明确返回未实现。

## 实现与行为

- `extraction/contracts.py` 定义模型草稿、字段引用、候选、来源状态与提取报告；`schemas/extraction-report.schema.json` 导出结构契约。
- `units.py` 将原文按行构造稳定证据单元，保留页码、单元格、Word 块及图片区域。图片保留空间分组和原始识别框，按列解释费用名称、基数和金额。
- `engine.py` 对每个来源提取单据、双方、运输信息及费用明细。两行同名费用依靠物理行位置区分；相同位置重复输出会提示复核。关联附件只能通过共同运输标识补充缺失的字面运输信息，不复制费用、币种或交易方。
- 每个非空字段逐一验证：单元存在、来源允许、引文是原文子串、声明的值确实在引文内。无支持的字段清空并标记复核。名称与金额跨行拼接会标记风险；费用原名、付款原文和不明币种符号保留。
- 模型漏掉的明确一行邮件费用分摊，使用范围受限的 `PAYER pay PAYEE: CHARGE CURRENCY AMOUNT` 规则补充；只处理邮件正文、排除条件/指令文字、始终复核，不猜法人关系或把 `$` 转成 USD。费用一经模型提取，不重复补充。
- 合计、已付金额、一般条款保存为观察项，不输出为额外费用。材料内要求忽略规则、伪造费用或改变输出格式的文字不改变系统提示词。
- 仅结构错误允许再调用一次修复；认证、网络、空响应和截断不自动重试。原始输出、失败代码和尝试次数保存到本地归档。来源失败仍出现在清单中，不伪装成无费用成功。
- 回放验证提示词、schema、输入证据、来源哈希等指纹；当前规则重新执行，模型调用为 0。相同来源的模型响应归档与完整账单结果缓存是不同层次。

## 实际验收

| 检查 | 分子/分母 | 结果 |
|---|---|---|
| 真实材料费用 precision / recall | 7/7，7/7 | 通过 |
| 真实材料可判定关键字段 | 28/29，96.55% | 通过 ≥90% |
| 真实材料非空字段证据定位 | 109/109 | 通过 |
| 真实材料未知字段处理 | 13/13 | 通过 |
| 固定合成片段验证集费用 precision / recall | 18/18，18/18 | 通过 |
| 固定合成片段可判定关键字段 | 68/68 | 通过 |
| 固定合成片段非空字段证据定位 | 122/122 | 通过 |
| 开发环境 / 已有干净环境测试 | 各 172/172 | 通过 |

真实材料共 7 个来源。两条 Handling Charge 分别保留 65 和 35；1310.40 的独立费用保留；邮件 H/C 50 保留且 `$` 不自动确定币种。总计、已付金额、通用收费条款及宣传图未多报费用。当前真实费用候选仍因 OCR 风险等处于复核状态，不代表自动入账。

剩余 1 个字段差异：CMA 费用原名提取成 `OCA17 On Carriage Additional - US Em`，比基准多了相邻收费代码 `OCA17`。金额 245、USD 和物理行均正确，原文证据完整；差异保留并交给阶段 5 的名称标准化处理，没有改标签或放宽评分。

最终提示词有 7 次真实材料模型调用、16 次合成来源模型调用。真实最终评测使用这些真实模型响应并以当前规则回放；这是模型实时调用与确定性后处理回放的组合，不是重新执行指定 OCR。阶段 3 OCR 输出直接作为输入，未重新上传原始文件。早期真实样本漏行、币种遗漏、费用基数混入名称等结果保留在 `outputs/stage4/real-live-v*.json`，未混入最终成绩。

验证材料位于 `tests/fixtures/extraction-validation/`，15 个独立合成片段族，含单/多费用、同名同额、负数、多币种、未知 `$`、正文/附件关联、条件条款和指令干扰。它直接提供标明来源的合成解析片段，不是实际文件解析或 OCR 新验收，也不替代未来完整的 60 场景、重复调用稳定性或最终留出集。真实基准和合成标签均由助手编制，未独立业务人员签核；不能据此宣称生产总体准确率。

## 复现

从项目根目录运行。默认输出预算已调整为 4096 tokens；只显式加载 `.env`。

```powershell
# 通用用法：前一步 parse 生成 input-parsed.json
.\.venv\Scripts\shipping-billing.exe --env-file .env extract outputs/input-parsed.json --mode live --storage data/local/extraction --output outputs/candidates.json
.\.venv\Scripts\shipping-billing.exe extract outputs/input-parsed.json --mode replay --storage data/local/extraction --output outputs/candidates-replay.json

# 当前机器真实模型响应与解析材料回放（这些私有文件 Git 忽略）
.\.venv\Scripts\shipping-billing.exe extract outputs/stage4/real-input.json --mode replay --storage data/local/stage4/model --output outputs/stage4/real-final-replay.json
.\.venv\Scripts\python.exe scripts/evaluate_stage4_real.py --input outputs/stage4/real-final-replay.json --output outputs/stage4/real-evaluation.json

# 固定组件验证：线上模式读取本地 .env，回放无需密钥
.\.venv\Scripts\python.exe scripts/stage4_validation.py --mode live --output outputs/stage4/validation-v7-live
.\.venv\Scripts\python.exe scripts/stage4_validation.py --mode replay --output outputs/stage4/validation-v7-replay
.\.venv\Scripts\python.exe -m pytest -q --junitxml=outputs/stage4/pytest.xml
.\.venv\Scripts\python.exe scripts/stage4_acceptance.py
```

`extract` 退出码为 0 无阻塞完成、1 含复核、3 来源提取失败、2 输入/配置错误。每次均按来源登记状态并正常写出报告。最大输入为每来源 120000 字符，超过时明确报错；不截掉尾部。大文档分块、全面字段标准化及最终业务一致性仍有后续工作。

本地验收文件：`outputs/stage4/acceptance.json`、`real-evaluation.json`、`validation-v7-live/evaluation.json`、`validation-v7-replay/evaluation.json`、`pytest.xml`、`pytest-clean.xml`。响应归档在 `data/local/stage4/`，敏感材料、密钥和报告未纳入 Git。本阶段未提交或推送仓库。
