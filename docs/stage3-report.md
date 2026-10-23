# 阶段 3：多格式文档解析验收

日期：2026-10-23。阶段 3 的解析层验收通过。已有指定 OCR 真实调用和逐页回放证据；本阶段输出带位置的原文片段、表格、供应方字段及复核原因，尚不输出最终账单决策。

## 实现范围

| 类型 | 已实现行为 | 实际验证 |
|---|---|---|
| EML | 邮件头、地址、日期；plain/HTML 选择一个正文；保留另一版本；附件递归、嵌套邮件及 base64 编码 | 真实邮件、中文标题、HTML 表格、嵌套与深度限制 |
| PDF | 每页原生文字、词语坐标、线框表格；每页生成独立 OCR 输入，保留原页对应关系 | 真实 6 页、两页无文字层扫描 PDF |
| PNG/JPG/TIFF | 格式签名、尺寸、方向、多帧清单；逐帧调用指定 OCR；保留辅助识别框与置信度 | 真实 PNG/JPG、合成 JPEG 与两帧 TIFF |
| XLS/XLSX | 多工作表、行和单元格位置、同名行、合并信息；隐藏工作表保留；公式与已有结果分离 | 两种真实格式的合成工作簿、缓存缺失和缓存值用例 |
| DOCX | 按顺序读取段落和表格、嵌套表格、页眉页脚、合并单元格；内嵌图片作为子来源 | 合成费用表、嵌套和合并用例 |
| DOC | 独立隐藏 Word 实例转换成 DOCX，再进入同一解析器；记录转换哈希；超时与失败明确报错 | 真正的 Word 97–2003 `.doc`，开发及干净虚拟环境均成功 |

旧版 DOC 当前依赖 **Windows 桌面版 Microsoft Word**，Python 的 Windows 条件依赖 `pywin32` 已锁定。转换使用独立 COM 实例，禁用宏和打开时更新链接，输入只读；结束后关闭自建实例。Linux/macOS 的 DOC 转换尚未实现，明确返回依赖错误；不宣称跨平台 DOC 验收通过。

XLS 使用 xlrd 读取，额外检查 BIFF 公式位置；公式表达式不重新求值。XLSX 同时读取表达式与已有缓存，无缓存不补 0，有缓存也标记未经新鲜度验证。依据见 [openpyxl 读取选项](https://openpyxl.readthedocs.io/en/stable/api/openpyxl.reader.excel.html) 和 [xlrd 格式支持](https://xlrd.readthedocs.io/en/latest/)。

## 来源与错误处理

- 按文件签名识别格式，EML 中 `application/octet-stream` 的 PDF 正确分流；原名仅保存为元信息，附件按哈希保存，不将不可信文件名拼入写出路径。
- 每个来源包含父来源、原名、MIME、实际格式、哈希和大小；每个文本片段包含来源与页/图像区域/单元格/Word 块位置。
- PDF、图像每页都有 `page_inventory` 和 `ocr_page_status`。多帧 TIFF 转为逐帧 PNG，原文件继续保留；不猜供应方 `page_index` 的含义。
- 来源文件限制 25 MiB、批次累计 100 MiB、100 个来源、附件递归 5 层、单文件 100 页/帧；Office 展开大小、单元格数、图像像素和 MIME 深度亦有限制。错误保留为报告条目，其他有效输入继续处理。
- `parse` 输出 `parsed / pending_review / error` 属于解析状态，与最终账单四类 operation 不同。`pending_review` 不会被转换成已接受账单。

## 真实 OCR 与原文交叉核对

指定 API 仍按题目参数调用。`PageOCR` 支持显式 live/replay，以及内容哈希校验后的缓存。一个批次复用同一临时通道，每页使用新随机链接，处理结束撤销该页链接；批次结束关闭通道。缓存命中不创建隧道，也不重新提交 OCR。

初次联调发现频繁新建通道会偶发建链/下载失败，以及部分调用超时。修改为批次复用后复测未成功的页面；成功页使用缓存。历史失败保留在 `outputs/stage3/parse-live.json`，复测证据在 `outputs/stage3/batch-check/`。读取超时后没有自动重发 POST。

13 个 PDF 页/图像帧全部取得指定 OCR 响应归档：12 个具有业务字段；真实邮件中的宣传 JPG 返回空业务内容，明确保留 `OCR_EMPTY_CONTENT`。该图片的本地文字、文件和父邮件关系仍保留，分类仅为辅助图片候选，没有直接判定“零费用”或丢弃。

由于指定接口不提供逐字原文，可选 `local-ocr` 扩展提供 **RapidOCR 本地辅助识别**。这一路输出标记 `local_ocr_supplement`，带像素框、置信度、引擎版本及模型哈希，不能冒充指定 OCR；安装依赖包后使用包内模型，不发送原图给其他识别服务。主依赖不强制安装此扩展，启用时需明确指定 `--local-ocr`。

PDF 的两条 Handling Charge（65 与 35）在原文中分别保留，供应方合并后的 100 也原样留存，并产生合并风险提示。图片中指定 OCR 的部分编号与辅助文本不一致，会产生不一致提示，不擅自改写供应方结果。它们为阶段 4/5 提取与校验提供证据，尚未证明最终费用候选和财务决策正确。

## 验收结果

| 检查 | 结果 |
|---|---|
| 要求中的 9 种格式 | 全部有实际文件解析证据 |
| 真实来源及附件登记、内容哈希、父来源关系 | 7/7 |
| 已标注费用原文行保留 | 7/7；两条同名费用独立保留 |
| 冻结基准中的关键文本点 | 31/31 |
| 页/帧清单 | 13/13，含成功或明确错误状态 |
| 单元与 CLI 测试 | 138/138（开发环境及独立干净环境） |
| 真实旧版 DOC 转换 | 开发环境及干净环境均通过 |
| 逐页回放 | 13 个归档可对应；12 个有效内容、1 个空内容错误保留；0 次 OCR 网络调用 |

原文行核对按费用名及同一文本行的金额进行匹配；图像辅助文字按邻近水平坐标分组，重复行分别计数。31 个文本点仅进行大小写及空白归一后比较，涵盖基准中明确的单号、日期原文、双方、运输标识和币种。评测在解析之后读取基准；基准答案不送入 OCR。基准仍未经过独立业务人员签核。

**这些数值是解析层的材料保留率，不是最终业务准确率。** 完整四类操作、增量更新、AR/AP、模型候选提取及正式 harness 仍属后续阶段。

## 复现入口

```powershell
# 核心安装，不含辅助 OCR 模型
.\.venv\Scripts\uv.exe sync --frozen --link-mode copy

# 若需图片原文辅助证据
.\.venv\Scripts\uv.exe sync --frozen --extra local-ocr --link-mode copy

# 无网络的 Office 解析；输出为来源证据
.\.venv\Scripts\shipping-billing.exe parse tests/fixtures/parsing/fees.xlsx --output outputs/parsed.json

# 本地 PDF + 辅助 OCR，指定接口未运行会明确标记
.\.venv\Scripts\shipping-billing.exe parse "..\PDF+示例账单2.pdf" --local-ocr --output outputs/pdf-parsed.json

# 显式开启指定 OCR 与临时文件传入
.\.venv\Scripts\shipping-billing.exe parse "..\图片示例.png" --source-id src-zim --mode live --local-ocr --cloudflared outputs/stage2/tunnel/cloudflared.exe --output outputs/image-live.json

# 当前机器的已归档结果回放；包含本地真实样本，未纳入 Git
.\.venv\Scripts\python.exe scripts/stage3_smoke.py --mode replay --ocr-map outputs/stage3/batch-check/replay-map.json --local-ocr --output outputs/stage3/replay-check

# 固定的开发测试与材料保留率评测
.\.venv\Scripts\python.exe -m pytest --junitxml=outputs/stage3/pytest.xml
.\.venv\Scripts\python.exe scripts/evaluate_stage3.py --input outputs/stage3/final/parse-replay.json --output outputs/stage3/retention.json
```

`parse` 退出码：0 全部解析完成，1 含复核项，3 存在来源解析错误；输入参数错误仍为 2。离线图片/PDF 的 `OCR_NOT_RUN` 会使命令返回 1，报告仍正常写出。

最终证据见本地 `outputs/stage3/acceptance.json`、`retention.json`、`pytest.xml`、`pytest-clean.xml`、`final/parse-replay.json`。原始响应、样本和报告均 Git 忽略。合成解析文件位于 `tests/fixtures/parsing/`，不属于最终业务验收集。

当前局限：扫描识别可能有误字；文字保留不等于角色/币种/费用归属已确定；复杂 Word 修订、文本框或嵌入对象会要求复核；公式缓存不保证最新；临时通道不具备生产可用性保证。阶段 2 的业务质量问题继续跟踪，阶段 3 已具备进入结构化提取所需的来源证据。
