# 文档解析测试材料

这些文件全部是项目自行生成的合成开发材料，只验证格式解析，不属于最终业务验收集，也不含用户的真实账单。

- `fees.docx`、`fees.xlsx`、`fees.xls`：由 `tests/unit/test_parsers.py` 中的内存构造函数生成，包含两条独立的 Handling Charge（65 与 35）。XLSX 另含一个隐藏工作表。
- `fees.doc`：将合成 DOCX 用本机 Microsoft Word 转为真实 Word 97–2003 二进制格式，验证旧版 DOC 转换链路。
- `fees.jpg`：合成账单的 JPEG 扫描图。
- `fees.tiff`：两帧扫描图；单号分别 TEST-SCAN-1、TEST-SCAN-2。
- `fees-scan.pdf`：上述两张图片封装成两页 PDF，无原生文字层。

图像材料均为两条 Handling Charge（65.00、35.00）、总额 100.00、币种 USD。文本由 ReportLab 绘制，再使用 PDFium 渲染；这些值仅用于解析后的核对，不能作为 OCR 或提取器的输入标签。

公式无缓存、缓存值、编码、嵌套邮件、损坏文件、重复费用、尺寸和深度限制等用例在测试时生成。来源文件不会因解析而被覆盖。
