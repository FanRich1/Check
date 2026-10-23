# 安装、演示与交付说明

## 交付内容

源码仓库和 `Check-stage9-v1.zip` 提供相同的应用、网页、模拟样本、公开模型响应回放、测试、schema 和说明。ZIP 根目录为 `Check/`，内有 `release-manifest.json`，列出版本、每个文件的字节数和 SHA-256；旁边的 `.zip.sha256` 校验整个 ZIP。

私人原件、`.env`、上传材料、OCR 私有响应、开发虚拟环境、运行日志和 cloudflared 可执行程序均不进入交付包。归档中的公开回放使用明确标记的合成材料，不能当作真实客户数据。

## 从空环境安装

已验证平台是 Windows x64、Python 3.12；本项目要求 Python `>=3.12,<3.13`。离线演示指运行时不访问模型或 OCR；首次安装仍需访问 Python 包索引，或由使用方提前准备离线包缓存。

1. 完整解压 ZIP，再进入 `Check`。不要在压缩文件预览窗口内运行。
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
.\.venv\Scripts\python.exe -m scripts.package_release --output dist/Check-stage9-v1.zip --version stage9-v1
.\.venv\Scripts\python.exe -m scripts.package_release --verify dist/Check-stage9-v1.zip
```

回放不发起模型或 OCR 网络请求；Word 转换为本机依赖。已公开的验证、留出材料现在是回归集，不能重复称为未见测试。最终统计和未解决的外部限制见 [阶段 9 交付报告](stage9-report.md)。
