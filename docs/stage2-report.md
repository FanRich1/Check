# 阶段 2 接入记录（文件传入已解决，识别质量待补齐）

日期：2026-10-22。使用用户提供的 DeepSeek 配置；密钥只保存于被 Git 忽略的 `.env`，不进入本文、示例或报告。

最新结论：已通过临时文件通道完成指定 OCR 的真实 PDF/PNG 调用，不再需要用户自行提供下载直链。两份本地样本均返回非空业务内容，但存在费用行被合并、编号误读和币种缺失，业务可用性尚未放行。下文初次空响应与超时记录保留为排查历史。

阶段 3 更新（2026-10-23）：已实现逐页原文、图像辅助文字、同名行与编号冲突提示，并验证批次复用临时通道。解析层已通过验收，供应方原始识别的业务质量仍不视为自动合格，详见 [阶段 3 记录](stage3-report.md)。

## 已解决：本地文件传入 OCR

使用 [Cloudflare Quick Tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) 将临时、仅监听回环地址的单文件服务映射为下载链接。官方工具 `cloudflared 2026.10.3` 下载自 Cloudflare 的 GitHub 发布地址，保存在 Git 忽略的本地输出目录。Quick Tunnel 无需账号和域名，但没有可用性保证，适合开发验收；生产应换成稳定的受控文件服务。

- `TemporaryFileServer` 只提供调用者指定的文件字节；随机路径、最长有效期、25 MiB 上限，无目录浏览和其他文件路由，不输出链接访问日志。
- 请求 OCR 前经公网下载并核对 SHA-256；调用期间统计完整 GET 响应次数。链接经 Cloudflare 转发，持有完整随机链接者在有效期内可读取该文件。
- 每次完成或异常都关闭文件服务和隧道。三次成功诊断的报告均确认隧道已结束；没有常驻服务，也没有把样本提交到 Git。
- 新增脚本 `scripts/probe_ocr_tunnel.py`。默认仅使用已校验的公开样本；使用本地文件需显式传入 `--local-file` 和 `--source-id`。该脚本仍是诊断入口，完整业务 `run` 未实现。

| 文件 | 指定 OCR 结果 | 耗时 | 下载记录 |
|---|---|---:|---:|
| Livingston 公开货运 PDF，经原网站链接 | 300 秒窗口内返回空列表 | 267.12 秒 | 原网站侧不可观测 |
| 同一公开 PDF，经临时链接 | 返回发票号及 5 条费用 | 59.52 秒 | 2 次 |
| 本地 `PDF+示例账单2.pdf` | 返回费用、提单和箱号等字段 | 30.81 秒 | 2 次 |
| 本地 `图片示例.png` | 返回发票号、2 条费用、提单和箱号 | 17.12 秒 | 3 次 |

两条路径结果不同，支持“下载路径影响识别”的判断，但无法仅凭客户端证据确定服务端具体网络故障。PNG 第一次建隧道收到提供商 HTTP 409，尚未提交 OCR；重新建立通道后成功。没有在已提交 OCR 后自动重发。

## 真实内容核对发现的问题

- PNG 的两条费用及金额 5.00、21.90 与样本基准一致；发票号和箱号存在字符误读。
- PDF 原有三行费用，其中两行 `HANDLING CHARGE` 分别 65.00 和 35.00。服务返回合并后的 100.00，以及 1310.40 的另一行；总额虽一致，却丢失了行身份，不能据此自动生成合格账单。
- 两份响应均未提供费用币种和原始逐字文本。不得根据基准答案回填生产结果，也不得把供应方字段冒充原文。
- 对这两份样本按“费用名（仅统一大小写/空白）+金额”保留重复行进行比较，正确匹配 3 行：precision 为 3/4，recall 为 3/5。仅为 OCR 层小样本诊断，不是完整业务准确率。
- 公开样本的五笔金额正确，但一处费用名称有拼写错误；原始 `page_index` 仍不擅自解释为单页定位。

后续工作转为解析与校验：保留指定 OCR 原始结果，文本 PDF 补充原文证据以检查同名行；图片需增加可靠的识别交叉核对或进入复核，不能静默修正编号、补币种或将同名费用相加。文件下载问题已解除，识别质量问题仍需解决，阶段 2 保持未全面放行。

本地证据：`outputs/stage2/tunnel/probe.json`、`tunnel/src-debit/probe.json`、`tunnel/src-zim/probe.json`、`ocr-local-comparison.json`、`ocr-full-timeout-probe.json`；原始响应在 `data/local/ocr/raw/`。全套离线测试现为 100/100，新增 6 项验证单文件访问、哈希、过期、关闭及输入限制；它们不计作识别准确率。

```powershell
# 默认公开样本诊断；先将官方 cloudflared.exe 放到指定位置
.\.venv\Scripts\python.exe scripts/probe_ocr_tunnel.py --cloudflared outputs/stage2/tunnel/cloudflared.exe

# 显式选择一份本地文件，经临时链接交给指定 OCR
.\.venv\Scripts\python.exe scripts/probe_ocr_tunnel.py --cloudflared outputs/stage2/tunnel/cloudflared.exe --local-file "..\PDF+示例账单2.pdf" --source-id src-debit
```

## 已获得的真实调用证据

1. `GET https://api.deepseek.com/models` 返回 HTTP 200，包含用户指定的 `deepseek-flash`。官方当前模型与配置见 [模型文档](https://api-docs.deepseek.com/quick_start/pricing/)。
2. 通过 LangChain ChatOpenAI、JSON 模式和 Pydantic 契约完成一次真实调用。单次合成输入的单号、金额、币种全部正确；不是从 fixture 标签读取的预测。结果和用量保存在 `outputs/stage2/model-probe.json`。
3. OCR 的 `/openapi.json` 返回 HTTP 200，实际 `/process` 要求题目规定参数。响应结构为 `data/success/message/error_code/task_id`；data 中含 `list` 航运业务记录，不是通用文字接口的 pages/text 结构。
4. 对公开测试 PDF 和 PNG 各做一次真实 POST，两次均 HTTP 200、success=true。但返回只有空业务字段、空 chargeList 等模板，没有可用识别内容。原始响应和摘要保存在 `outputs/stage2/ocr-public-*-response.txt` 与 `ocr-public-probes.json`。

公开材料为 W3C 的 [测试 PDF](https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf) 和 [标志 PNG](https://www.w3.org/Icons/w3c_home.png)，用于连通性测试，并非本项目航运账单；没有向文件托管服务上传用户的原始样本。不能根据非账单材料的空模板就推断航运账单一定不可识别，也不能把 HTTP 200 算作识别门槛通过。

## 已实现的代码

- DeepSeek：官方 HTTPS 端点校验、LangChain JSON 调用、严格结构校验、超时和安全错误码。使用 [JSON 模式](https://api-docs.deepseek.com/guides/json_mode/)；按 [thinking 参数](https://api-docs.deepseek.com/guides/thinking_mode/) 显式关闭思考模式进行最小提取，未保存内部推理。不自动改模型，不隐藏重试。
- OCR：题目四个参数的 POST；原始响应本地归档；非成功、非法 JSON、空模板均失败；只对连接建立失败做有上限重试，读超时、HTTP 错误和响应错误不自动重发。
- 响应适配器：保留供应方结构化字段及原始 page_index，不能冒充原文 OCR 文字或未经确认把其页码规范化。纯文本备用结构有离线测试，但尚未在指定服务上观察到。
- 缓存与回放：显式 live/cache/replay，记录网络尝试数；缓存需要文件内容 SHA-256；回放要求成功版本归档及相同 source_id/hash。空模板不写入可用缓存。
- URL 映射：本地文件 source_id 映射到下载 URL 与 SHA-256；本地文件变更则拒绝映射。此校验不独立证明远端字节相同，文件提供方须确保 URL 指向该版本。
- 日志：错误不输出服务返回体、密钥或原文；SDK 网络 DEBUG 日志被压低，模型追踪上下文关闭。原始 OCR 响应仍只保存在 Git 忽略的本地目录。

## 初次验证结果与门槛（历史记录）

94/94 离线测试通过，涵盖原有工程测试及 29 项新增服务/入口测试；Ruff 和依赖锁检查通过。它们验证响应处理与故障控制，不是实时 OCR 准确率。机器报告见 `outputs/stage2/pytest.xml` 和 `acceptance.json`。

| 门槛 | 状态 |
|---|---|
| 指定模型认证与模型标识 | 真实验证通过 |
| 一次真实 LangChain 结构化调用 | 通过 |
| 指定 OCR 网络连通与响应结构 | 已确认 |
| 实际 PDF/PNG 返回可用识别内容 | 未通过，目前只收到空模板 |
| 原有本地账单的 OCR 核对 | 未执行，尚无服务可访问的样本直链 |
| 超时、错误、空结果、缓存和回放处理 | 离线验证通过 |

当时结论：阶段 2 部分完成，不能标记全部通过；需要可下载文件和有效输出。此项文件传入阻塞已由上文临时通道解除，无须再要求用户自行准备链接。

## 补充排查：文件传入与对照请求

2026-10-22 再次读取服务根路径及 OpenAPI，均 HTTP 200。公开接口只声明 JSON 请求，`file` 为字符串；没有声明 multipart 文件上传入口或 base64 传入约定。题目明确要求服务可访问的下载地址，因此不能把“字符串类型”解释为支持本地文件或任意编码，也不能断言服务不存在未公开的上传能力。

新增两次 `/process` 对照请求，均使用题目规定的四个参数，每个仅提交一次：

| 输入 | 本机确认 | OCR 结果 |
|---|---|---|
| `https://billing-ocr-negative-control.invalid/missing.pdf` | 特意构造的无效地址 | 等待约 90 秒后 ReadTimeout，未取得响应 |
| [Livingston 公开货运发票样本](https://www.livingstonintl.com/livingston-content/uploads/2015/08/Freight-Forwarding-Sample.pdf) | 下载 HTTP 200，PDF 文件头有效，384217 字节 | 等待约 90 秒后 ReadTimeout，未取得响应 |

这次诊断使用 90 秒读取超时，短于题目示例和正式客户端的 300 秒；结果只说明诊断等待窗口内没有响应，不能宣称服务在 300 秒内也必定失败。本机能下载公开样本也不证明 OCR 服务器能下载。无效地址请求没有返回可比较的业务结果，不能据此判定接口是固定模板或模拟服务。未自动重发，服务端是否继续处理未知。

证据保存于本地 `outputs/stage2/ocr-followup-probes.json`、`ocr-openapi-recheck.json`、`ocr-health-recheck.json`；公开 PDF 另存为 `ocr-public-freight-sample.pdf`。用户原始样本仍未上传，阶段 2 仍未放行。

可转给接口提供方的排查信息（尚未发送）：

> 按题目向 `/process` POST `file` 下载直链、`task=orderChange`、空 `callBack` 和 `taskType`。此前公开 PDF/PNG 返回 success=true，但业务字段和 chargeList 均为空；本次公开货运发票样本在 90 秒诊断窗口内没有返回。能否提供一份已验证可识别的 PDF/图片直链、完整请求及预期响应，并确认是否有本地文件上传方式、服务器下载限制和通常处理耗时？

## 使用方法

```powershell
# 显式读取本地密钥；一次真实、有限输出的合成模型调用
.\.venv\Scripts\shipping-billing.exe --env-file .env probe-model --report outputs/stage2/model-probe.json

# 真实 OCR，URL 需由服务直接下载，不能是需登录的预览页面
.\.venv\Scripts\shipping-billing.exe ocr --file-url "https://example.invalid/bill.pdf" --source-id bill-1 --report outputs/ocr-result.json

# 本地样本配合 URL 映射；不执行自动上传
.\.venv\Scripts\shipping-billing.exe ocr --local-file "..\PDF示例账单1.pdf" --url-map data/local/file-urls.json --source-id src-cma --cache --report outputs/ocr-result.json
```

URL 映射文件形状如下，哈希和 URL 必须换成实际值，example.invalid 仅为占位，不可运行识别：

```json
{
  "src-cma": {
    "sha256": "实际本地文件的64位小写SHA-256",
    "url": "https://example.invalid/bill.pdf"
  }
}
```

回放：`ocr --file-url <原URL> --source-id <原来源> --sha256 <原内容哈希> --replay <raw归档.json> --report <输出.json>`。回放也校验可用内容，不能让空模板回放变成成功。

当前缓存不提供自动过期/后台清理；raw 与 cache 保存在指定本地目录，由调用者管理。阶段 3 会基于实际有效服务输出继续做多页与表格适配，不提前宣称已完成。
