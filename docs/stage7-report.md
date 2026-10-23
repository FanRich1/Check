# 阶段 7：完整工作流与 CLI

验收日期：2026-10-23。`run` 已实现，阶段 7 完成。质量评测集、三次独立模型稳定性测试与最终验收集留到阶段 8。本次结果不代表生产总体准确率。

## 功能与结果

`run` 以明确的 parse → extract → intents → reconcile → write 状态串联既有模块。支持多文件和目录、显式记账主体或策略、旧账数组/完整 state、指定 OCR 的 URL/页归档映射、可选临时 URL 传入及本地辅助 OCR。首次旧账不合法时拒绝整个请求，不能当成空账本。

输出包含：

| 文件 | 用途 |
|---|---|
| records.json | 题目核心账单数组及逐字段审计，金额为精确 JSON 数字 |
| manifest.json | 模式、每来源状态、计数、版本、缓存命名空间及产物 SHA-256 |
| audit.json | 记录身份、输入费用行、匹配/标准化规则、字段证据与已应用变更 |
| state.json | 可供下一次运行的业务快照、来源去重身份和历史审计 |
| parse/extraction/normalized/reconciliation/intents.json | 每个中间阶段的证据及失败原因 |
| context.json / ocr-replay-map.json | 实际使用的策略、可用的 OCR 页回放索引 |
| work/ | 本次保留的原始来源及必要的转换材料 |

每个费用行有 decision，重复来源有 duplicate_of；核心主记录只输出一次。没有费用的文件和解析失败的文件进入清单。无完成 manifest 的目录视为未完成输出，不能消费零散产物。manifest 最后写入，其他 JSON 逐文件原子写出，产物哈希可核对；这不是跨文件数据库事务。已有结果不覆盖，需选择新输出目录，旧账文件不原地修改。

## 修改意图与授权

`pipeline/intents.py` 在存在修改文字时提取逐行指令。模型只见规范化候选和同一来源家族的原文，不接收期望 operation，也不靠模型选择任意旧账。修改字段、新值、动作、引用及明确打印的旧值都需校验。未打印旧值时，只有唯一匹配的旧账才能提供本次 CAS 旧值；已经等于目标值时重传不再修改。

修改引用必须属于登记原文及具体位置，并通过阶段 6 白名单、逐字段修改语义和变更后金额/财务检查。多费用必须有行/费用名/明确记录指向；跨附件引用需要绑定相应费用或唯一单据。否定、条件式修改、伪造引文、系统规则干扰、无法定位费用的孤立指令均复核。没有旧账时，修改材料不能直接当新账创建。自动与手工指令冲突时复核。

修改意图单独使用版本化提示词/schema 和精确输入归档，结构错误最多修复一次。调用失败不当成“没有修改”，会阻断对应来源，保留其他独立来源。金额仅出现不同值、没有修改意图时仍为未授权冲突。

## 模式、缓存与退出码

- live：显式在线配置；费用和意图通过真实 LangChain 调用，指定 OCR 使用明确 URL 传入。OCR 可命中既有内容哈希缓存，调用计数与模式保留。
- replay：重新解析原始文件，只读精确匹配的模型/意图及 OCR 页归档，无自动在线回退。模型缓存带 provider/model/base_url、提示词、schema 与规则版本，并保留归档响应模式。
- offline：不调用 OCR，模型只读归档。PDF/图片保留未识别风险，缺少精确回放时为错误。
- test_injected：仅内部测试 API 使用，模型/OCR 替身明确标记，CLI 不提供此模式。

退出码：0 全部完成；1 至少一项业务待复核；2 输入/配置无效；3 存在来源执行失败或程序失败。错误优先于待复核。OCR 空响应、超时和归档损坏均为来源错误。live 的失败请求无法准确确认网络尝试数时记录 null；replay 的网络调用计数为 0。不能将图片识别风险清掉来获得自动入账。

目录发现保留被忽略文件及原因，排除缓存、输出、Git、虚拟环境和符号链接目录。显式重复路径只选一次，不同路径的同内容文件仍有独立来源。顶层文件最多 100 个，发现条目最多 10000 个；附件数量、大小、页数、深度继续使用阶段 3 预算。输出与输入/策略冲突、可写缓存覆盖输入均在模型调用前拒绝。回放缓存只读，原始文件副本放在输出 work，不污染公开 fixture。

## 实际验收证据

| 检查 | 结果 |
|---|---:|
| 确定性端到端必过场景 | 17/17 |
| 真实格式 | EML/PDF/PNG/JPG/TIFF/XLS/XLSX/DOC/DOCX，9/9 |
| 来源登记 | 19/19 |
| 已接受主记录的核心字段证据 | 12/12 |
| 开发环境完整测试 | 381/381 |
| 独立非编辑安装完整测试 | 381/381 |
| 合成原始邮件真实模型调用 | CREATE、UPDATE、NO_CHANGE 全符合，3 次真实 LangChain 调用 |
| 公开响应归档回放 | 3/3，0 模型及 OCR 网络调用 |
| 真实原始材料回放 | 4 个根文件、7 个来源、7/7 条费用与阶段 5 已确认字段一致 |

17 个流程用例使用实际文件，模型/OCR 明确为注入替身；DOC 使用 Windows Microsoft Word 实际转换。覆盖新增、自动授权更新、重传、未授权冲突、缺主体、同名独立行、邮件附件去重、混合失败和模型失败。额外 pytest 覆盖目录、篡改缓存、孤立修改、条件/否定/系统指令、修改引用造假、歧义作用范围和输入保护。schema、模型校验、产物哈希均通过。

真实模型演示只使用合成邮件：先提取 USD 40，第二封邮件提取 USD 50 及“Update amount from 40 to 50”，得到 UPDATE，再应用 state 后回放得到 NO_CHANGE，state 与审计不再增加。共两次费用提取、一次意图提取，均为真实调用；该演示不需要 OCR，不冒充新的 OCR 测试。三个响应归档已分发到 `tests/fixtures/pipeline/replay-cache`。

真实材料沿用阶段 3 OCR 与阶段 4 模型的真实历史响应，从原始文件重新解析后完整回放。本次未新增 OCR 或真实材料模型调用。7 条费用全部保留且因缺主体等原因复核；一张邮件内嵌图片的历史指定 OCR 返回空内容，明确记为 OCR_EMPTY_CONTENT、该来源 error、邮件批次退出码 3。其他来源继续处理，无新增账单。这是已知失败的正确登记，不能称为全部来源成功。它与既有识别质量问题一起留待后续质量工作。

## 复现

```powershell
# 离线公开演示，无密钥；请使用新输出目录
.\.venv\Scripts\python.exe -m scripts.stage7_demo --mode replay --storage tests/fixtures/pipeline/replay-cache --output outputs/stage7/demo
# 明确注入模型/OCR的流程验收，包含 Word 实际转换
.\.venv\Scripts\python.exe -m scripts.stage7_smoke --output outputs/stage7/check
.\.venv\Scripts\python.exe -m pytest -q --junitxml=outputs/stage7/pytest.xml
.\.venv\Scripts\python.exe scripts/export_schemas.py

# 已配置 DeepSeek 的真实合成邮件演示，会产生模型费用
.\.venv\Scripts\python.exe -m scripts.stage7_demo --mode live --output outputs/stage7/live-demo
# 真实输入 PDF/图片：显式提供该来源每页的 URL 映射；先配置记账主体和 OCR 文件可达性
.\.venv\Scripts\shipping-billing.exe --env-file .env run "input/bill.pdf" --source-id bill --mode live --ocr-map data/local/page-urls.json --accounting-entity "当前记账法人" --output outputs/live-bill
```

本机私有原始材料回放：`python -m scripts.stage7_real_replay --output outputs/stage7/real-accepted`，需要此前归档和可选 local-ocr 依赖。不存在这些私有文件时无需运行此命令；公开演示完全独立。

独立安装采用 `UV_PROJECT_ENVIRONMENT=outputs/stage7/clean-env`，`uv sync --frozen --no-editable --link-mode copy --reinstall-package shipping-billing-assistant --refresh-package shipping-billing-assistant`。使用该环境的 Python 跑完整 pytest 与公开回放，避免旧版本地 wheel 缓存造成假通过。Windows DOC 转换仍依赖桌面 Microsoft Word，其他系统未完成该格式验收。

页映射以明确的来源 ID 为键，例如 `bill:page-1`，值包含该页实际传入字节的 sha256 与 url（live）或 archive（replay）。多页 PDF 传入的是逐页派生文件，不能拿整个原 PDF 的哈希代替某页哈希。可沿用阶段 3 的显式文件 URL 准备方式；未知 URL 不能自动上传。完整归档和新输出目录准备就绪后，`python scripts/stage7_acceptance.py` 汇总验收与可分发文件密钥扫描。

本地证据：`outputs/stage7/check/summary.json`、`live-demo/summary.json`、`public-demo/summary.json`、`real-accepted/summary.json`、`pytest.xml`、`pytest-clean.xml` 及 `acceptance.json`。原始材料、密钥、真实回放和运行产物均不纳入 Git。本阶段未自动提交或推送。
