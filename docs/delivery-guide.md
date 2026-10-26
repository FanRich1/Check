# 安装、演示与交付说明

## 交付内容

当前补验版本为 `shipping-review-v2-20261026`，交付包名为 `Check-shipping-review-v2-20261026.zip`。旧的 `Check-stage9-v1.zip`、`Check-mysql-v1.zip` 和 `Check-manual-review-v1.zip` 保持历史状态，不代表当前源码。ZIP 根目录为 `Check/`，内有 `release-manifest.json`，列出版本、每个文件的字节数和 SHA-256；旁边的 `.zip.sha256` 校验整个 ZIP。

该 ZIP 发布后的源码已追加图片 OCR 空内容状态优化：符合条件的图片转为用途待确认，真实执行故障仍报错。当前本地工作台已更新；上述 ZIP 尚不含这次追加修改，其原有安装验收结论仅适用于归档版本。

私人原件、`.env`、上传材料、OCR 私有响应、开发虚拟环境、运行日志和 cloudflared 可执行程序均不进入交付包。归档中的公开回放使用明确标记的合成材料，不能当作真实客户数据。

## 从空环境安装

已验证平台是 Windows x64、Python 3.12；本项目要求 Python `>=3.12,<3.13`。离线演示指运行时不访问模型或 OCR；首次安装仍需访问 Python 包索引，或由使用方提前准备离线包缓存。

1. 完整解压 ZIP，再进入 `Check`。建议使用 `D:\Check` 等短路径；较深目录可能在解压公开回放归档时触发 Windows 路径长度限制。不要在压缩文件预览窗口内运行。
2. 安装 Python 3.12 后，双击 `安装运行环境.cmd`。它创建本目录的 `.venv`，用固定版本 uv 和锁文件安装应用及本地辅助 OCR，不覆盖已有 `.env`。
3. 双击 `启动账单工作台.cmd`，浏览器打开 http://127.0.0.1:8765 。保持启动窗口打开；按 Ctrl+C 可停止该窗口内的服务。
4. 点击「先体验示例」。它回放已归档的合成 Excel 提取结果，得到两笔 21、31 的费用，不需要密钥。

命令行安装方式：

```powershell
py -3.12 scripts/install_windows.py --local-ocr
# 不需要本地辅助识别时，可以省略 --local-ocr。
.\.venv\Scripts\python.exe -m shipping_billing.web.server --project-root . --env-file .env --open-browser
```

安装脚本采用非 editable 安装。修改源码后需再次执行安装脚本或 `uv sync --frozen --no-editable --reinstall-package shipping-billing-assistant --extra local-ocr --link-mode copy`；开发时可按 README 使用 editable 安装。

## 在线模型与指定 OCR

安装脚本只在 `.env` 不存在时从模板复制。在线处理需自行填写配置：

```dotenv
BILLING_MODE=online
BILLING_MODEL_PROVIDER=deepseek
BILLING_MODEL_NAME=deepseek-flash
BILLING_MODEL_BASE_URL=https://api.deepseek.com
BILLING_MODEL_API_KEY=在本机填写自己的密钥
BILLING_MODEL_MAX_TOKENS=4096
```

该模型名称是在本项目环境实测的配置；其他账号应以其服务实际提供的模型为准。修改后重启服务并刷新页面。「模型已配置」只表示配置齐全，不代表网络和额度均可用。不要将 `.env` 发给他人或提交到 Git。

指定 OCR 接口、任务和参数沿用题目。它通过 URL 下载页面，`file` 不能填写本地盘符。两种传入方式任选一种：

- 从 [Cloudflare 官方下载页](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) 获取 cloudflared，放入 `tools/cloudflared.exe`。网页勾选「为 PDF / 图片启用指定 OCR」，允许为相应页面生成临时链接，处理后关闭。旧开发目录 `outputs/stage2/tunnel/cloudflared.exe` 仍兼容。
- 已有服务可直接下载的页面链接时，在「更多选项」上传 OCR 映射 JSON。结构和内容哈希匹配规则见 [阶段 2 接入记录](stage2-report.md) 和 [阶段 3 记录](stage3-report.md)。预览页、登录地址或已经失效的临时链接不可使用。

临时链接在提交 OCR 前最多进行三次只读下载校验，以容忍短暂网络及边缘通道就绪延迟。内容哈希不一致立即失败；该校验不提交 OCR，也不重发已提交但结果不确定的 OCR 请求。

图片、扫描 PDF 或邮件内嵌图片若需要补充原文，另行勾选「为图片 / 扫描件补充本地文字识别」。它依赖 `local-ocr` 可选依赖，与指定 OCR 分开记录，不替换题目要求的调用。识别结果仍需核对。

旧 `.doc` 需要 Windows 桌面 Microsoft Word；`.docx` 不需要 Word。完整测试含一次真实旧 DOC 转换，未安装 Word 时不能宣称完整格式验收通过；可先运行不含该依赖的合成 EML 演示。

## 日常使用和复核

第一次选材料，填写真实记账公司全称，旧账单可空。材料按业务批次上传；EML 已含的附件不必再单独上传。完成后核对费用数量、金额、币种、交易双方及原文依据。

四类结果分别为 `CREATE`、`UPDATE`、`NO_CHANGE`、`PENDING_REVIEW`。记账主体并非任意提示词：它决定从哪家公司的视角判断 AR/AP。测试占位公司不能验证真实记账方向；收货人、通知人、邮件发件人或银行收款人也不能直接等同账单交易方。

「已核对」「需补材料」及备注只用于跟进，不批准待复核项目，也不修改核心账本。确认业务关系后，应提供有原文依据的明确业务配置并重新处理；不要通过改结果 JSON 绕过校验。下一批可点击「接续账本处理下一批」，或上传已下载的 `state.json` 保留去重信息。

下载文件分别为：

| 文件 | 用途 |
|---|---|
| `records.json` | 六个核心分组、四类操作的账单数组，金额为精确 JSON 数字 |
| `state.json` | 已接受账单、来源身份和重复处理信息，用于后续批次 |
| `audit.json` | 操作规则、字段证据及旧值/新值 |
| `manifest.json` | 每个输入及附件的状态、错误、调用次数、产物校验值 |
| `reviews.json` | 独立的人工核对标记和备注 |

标准四类操作样例位于 `tests/fixtures/reconciliation/*-records.json`，均为明确合成示例。实际公开响应回放的新增/更新/重传演示：

```powershell
.\.venv\Scripts\python.exe -m scripts.stage7_demo --mode replay --storage tests/fixtures/pipeline/replay-cache --output outputs/demo-delivery
```

依次应得到 USD 40 新增、仅金额 40→50 更新，以及重传无变化。重复演示需改用新的输出目录，不能覆盖旧证据。

## 自带真实材料的导入

私人样本不随 ZIP 分发。可从本地选择 PDF、EML、图片、Excel、Word，不需要移动到源码目录；网页会在 `data/local/web/<任务编号>/` 保存该次上传副本。单文件最多 20 MB、合计 60 MB，每批最多 20 个文件。

命令行在线示例：

```powershell
.\.venv\Scripts\shipping-billing.exe --env-file .env run "C:\业务材料\邮件.eml" "C:\业务材料\账单.pdf" --mode live --accounting-entity "实际记账公司全称" --cloudflared tools/cloudflared.exe --local-ocr --storage data/local/my-batch --output outputs/my-batch
```

在线会将文本文档内容发送给配置的模型；指定 OCR 会下载对应页面。仅在业务授权范围内选择材料。运行结果和日志保存在本机，不会随 Git 提交。

## 故障处理

| 现象 | 处理 |
|---|---|
| 浏览器未弹出 | 等启动窗口显示已就绪，手动打开本机地址；查看 `outputs/web/startup-error.log` |
| 无法连接本地工作台 | 确认服务仍运行；重新启动后刷新页面，获取新会话令牌。先看历史任务再重提，避免重复调用 |
| 页面会话已失效 | 刷新页面后重新选择材料；不要关闭来源校验或手动伪造令牌 |
| 8765 被其他软件占用 | 手动启动时指定 `--port 8766`，访问对应地址 |
| 模型连接、余额或额度错误 | 检查账号、网络、模型名及额度，服务不会自动换模型；不确定请求结果时先查任务状态 |
| OCR 超时或空响应 | 该来源保留错误，其他成功结果仍保留；超时不会自动重发，不将空模板当作成功 |
| OCR 无原文、仅有结构化字段 | 结合 PDF 原生文字或显式本地辅助识别，对照原件复核；不保证仅凭其字段自动入账 |
| 交易方不明、币种只有 `$` | 补充真实业务依据；不能把未知币种、开票方或付款方猜成确定值 |
| 缺 Word 或本地 OCR 组件 | 按上面的依赖说明安装；不能将未运行的格式测试计为通过 |

## 复现测试和生成交付包

```powershell
.\.venv\Scripts\python.exe -m pytest -q --junitxml=outputs/delivery-pytest.xml
.\.venv\Scripts\shipping-billing.exe benchmark --split development --mode replay --storage tests/fixtures/harness/replay-cache/development --output outputs/delivery-development
.\.venv\Scripts\shipping-billing.exe benchmark --split validation --mode replay --storage tests/fixtures/harness/replay-cache/validation-final --output outputs/delivery-validation
.\.venv\Scripts\shipping-billing.exe benchmark --split holdout --mode replay --storage tests/fixtures/harness/replay-cache/holdout --output outputs/delivery-holdout
.\.venv\Scripts\python.exe -m scripts.package_release --output dist/Check-shipping-review-v2-20261026.zip --version shipping-review-v2-20261026
.\.venv\Scripts\python.exe -m scripts.package_release --verify dist/Check-shipping-review-v2-20261026.zip --compare-root .
```

回放不发起模型或 OCR 网络请求；Word 转换为本机依赖。已公开的验证、留出材料现在是回归集，不能重复称为未见测试。最终统计和未解决的外部限制见 [阶段 9 交付报告](stage9-report.md)。

## 2026-10-26 当前版本补验结果

本轮应用代码指纹为 `c501552f6b4915b606b8c8c010e03d7880dd7e280452357c0b946bfd88920062`。应用及静态资源共 69 个文件；打包清单另包含脚本、测试、公开合成回放资料和文档。内部 ZIP 校验与源码一致性是两项独立检查，使用上面的 `--compare-root` 同时验证。

| 检查 | 本轮实际结果 |
|---|---|
| 历史真实回放差异 | 少出的两条为 PNG 中的 WHARFAGE ADMINISTRATION FEE 5.00 USD 和 WHARFAGE 21.90 USD。旧映射使用原图片字节哈希，当前小图预处理生成 2 倍图像，页面哈希不同；后续模型归档也无法匹配。邮件宣传图的旧映射同样缺失。历史失败记录未改写。 |
| 最终整批在线 | 4 个顶层原件、7 个含附件来源、8 个页面，实际 OCR 调用 8 次、模型调用 7 次；已知 7 条费用的名称、金额、明确币种与原件归属逐项对应。原始材料缺少确定的完整记账关系，7 条均保留待复核。 |
| OCR 页面与响应 | 7 页返回可用内容，宣传图返回空业务内容；8 页都有真实调用记录和可回放响应归档。本轮没有下载或通道失败，`complete_online_success` 仍为 false，不宣称所有页面内容识别成功。 |
| 当前版本完整回放 | 从最终在线归档重新解析，7 条费用保留；模型调用 0、OCR 网络调用 0，业务字段及字段证据与对应在线轮一致，宣传图空内容错误也如实复现。 |
| 3 轮现有材料模型重复验证 | 最终整批在线轮加上 2 轮使用固定真实 OCR 响应、独立空模型缓存的真实模型调用，共 21 次模型调用；每轮均为 7 条费用，业务字段一致。个别证据引用范围在独立轮之间不同；固定归档回放的证据完全一致。 |
| 费用角色修正 | 发现模型偶尔把“余额收款对象”当作签发方。增加确定性证据校验：仅有银行、收款或运输角色依据时，签发方 / 接收方留空，原文和收款候选保留供复审；不会将其直接作为确定的记账关系。 |
| 工程回归 | 源码完整测试 600 通过、1 项仅在 SQLite 环境跳过；独立 MySQL + SQLite 验收 81 通过、1 项 SQLite 不适用跳过，真实 MySQL 行锁测试实际执行。全项目 Ruff 通过。 |
| 公开原始文档回放 | 开发 30、验证 15、历史留出 15 个场景均通过行为检查及适用评分门槛，模型网络调用为 0；这些已公开场景仅作为回归。 |
| 候选包干净安装 | 在项目外新建虚拟环境，按锁文件非 editable 安装，确认 69 个应用 / 静态文件均从 site-packages 加载且字节匹配；完整测试 600 通过、1 项 SQLite 不适用跳过。 |
| 候选包数据库与浏览器 | 隔离数据库验收 81 通过、1 项 SQLite 不适用跳过；Edge 数据库工作流 15 项、邮件关联 4 项、材料用途确认 4 项通过，JS 错误为 0。未使用正式账本做验收。 |

本轮包括发现角色歧义前的探索轮与修正后的最终轮，共实际发起 OCR 16 次、模型 42 次。上表的 8 / 21 次分别属于最终整批 OCR 及最终三轮模型验证，不能当作本轮全部调用成本。

最终 ZIP 的哈希、源码差异、独立安装与最新校验记录保存在本机 `outputs/remediation-20261026/`；私人原件及 OCR / 模型归档位于 Git 忽略目录，不包含在 ZIP 中。重新执行真实验收时使用新的输出和缓存目录，不能覆盖既有报告或混用历史响应：

```powershell
# 所有真实页重新在线调用，先准备本机 .env 与 cloudflared。
.\.venv\Scripts\python.exe -m scripts.stage9_real_acceptance --mode live --output outputs/my-live --storage data/local/my-live
# 精确回放本轮归档，包括真实空内容响应；不会自动转在线。
.\.venv\Scripts\python.exe -m scripts.stage9_real_acceptance --mode replay --output outputs/my-replay --storage data/local/my-live --ocr-map outputs/my-live/ocr-replay-map.json
# 对比业务字段与证据，不调用外部服务。
.\.venv\Scripts\python.exe -m scripts.real_material_audit --run outputs/my-replay --compare outputs/my-live --output outputs/my-replay-comparison.json
# 独立模型重复轮使用固定真实 OCR，模型缓存必须是新目录。
.\.venv\Scripts\python.exe -m scripts.stage9_real_acceptance --mode live --fixed-ocr outputs/my-live/ocr-replay-map.json --output outputs/my-model-round-2 --storage data/local/my-model-round-2
```

本次独立业务签核、新未见真实样本与生产总体准确率验收仍未完成，已按用户要求保留待办。现有 4 个原件的 7 条费用及重复运行结果不能替代它们。待复核并不等于自动正确入账，未知币种、付款关系、AR/AP 仍须提供业务依据。
