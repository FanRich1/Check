# 需求覆盖表（阶段 0）

来源：题目页面，2026-10-22 重新读取；原始快照及 SHA-256 保存在 `data/local/stage0/requirements-source.json`。下表是验收映射，不是功能已实现声明。模块名称是后续建议位置。

| ID | 必须实现的要求 | 建议模块 | 验收方式 / 用例类别 | 完成阶段 |
|---|---|---|---|---|
| R01 | Python 与 LangChain 实现 | 工程、extraction | 干净安装、真实模型结构化调用 | 1/2 |
| R02 | EML 邮件头、正文、编码 | parsers/email | 中文标题、plain/HTML、地址、时间准确 | 3 |
| R03 | 附件按真实格式递归解析 | parsers/email、dispatch | octet-stream PDF、嵌套 EML、来源关系 | 3 |
| R04 | 文本和扫描、多页 PDF | parsers/pdf、ocr | 每页有结果、扫描内容不丢失 | 2/3 |
| R05 | JPG、PNG、TIFF | parsers/image、ocr | 各格式实际文件接入测试 | 3 |
| R06 | 指定 OCR API、可访问 URL 和规定参数 | ocr/client、storage | 真 PDF/PNG 调用证据，非模拟 | 2 |
| R07 | XLS/XLSX 表头、工作表、费用行 | parsers/excel | 旧/新格式、多表、公式缓存状态 | 3 |
| R08 | DOC/DOCX 正文和表格 | parsers/word | 旧/新格式、转换依赖可复现 | 3 |
| R09 | 接收旧账单 JSON | domain/input | 合法、缺 ID、重复 ID、格式错误 | 1/6 |
| R10 | 单据类型、编号、日期 | extraction | 字段级标签、日期歧义不猜测 | 4 |
| R11 | 签发方、接收方 | extraction | 不混淆收发邮件人、货主、抬头、收款人 | 4 |
| R12 | 费用、数量、单价、金额 | extraction | 多费用、同名行、数量×单价 | 4/5 |
| R13 | 小计、税额、总计 | extraction、validation | 合计不生成新费用；金额核对 | 4/5 |
| R14 | 原始名称及费用标准化 | domain/normalization | 题目全部别名映射，未知名称保留 | 5 |
| R15 | 币种标准化、ISO 4217 | domain/currency | 规范码、非法码、单独 $、多币种 | 5 |
| R16 | PP/CC 等付款描述标准化 | domain/payment | 运费范围、COD 不强转 PP/CC | 5 |
| R17 | 无匹配有效费用新增 CREATE | domain/decision | 明确新费用输出；风险项不自动接受 | 6 |
| R18 | 每个实际费用一条记录 | extraction、identity | 两行同名、合计、条款与签名图 | 4/6 |
| R19 | 四级组合条件匹配旧账单 | domain/matching | 各优先级命中、多候选、弱键冲突 | 6 |
| R20 | UPDATE 只改明确要求字段 | domain/changes | 授权变更集合精确比较 | 6 |
| R21 | 未改字段及扩展字段继承 | domain/merge | 旧值保留、缺失不清空、显式 null | 6 |
| R22 | 按当前记账主体判断 AR/AP | domain/finance | 双方视角、未知主体、贷项与冲销 | 5 |
| R23 | 去重与 NO_CHANGE | domain/identity | 同内容重传、应用更新后重传 | 6 |
| R24 | 冲突、低置信度及 PENDING_REVIEW | domain/review | 多候选、矛盾证据、缺关键字段 | 5/7 |
| R25 | 金额有效、精度、明细合计检查 | domain/validation | Decimal、税、折扣、多币种、舍入 | 5 |
| R26 | OCR 0/O、1/I 与结果校验 | ocr、validation | 不盲改标识；识别问题后复核 | 5 |
| R27 | 不虚构、未知字段 null | extraction、validation | 缺失标签、未知币种、无证据值 | 4/5 |
| R28 | 金额为 JSON 数字、合法 JSON | contracts、serialization | 拒绝字符串/NaN；schema 与业务校验 | 1/7 |
| R29 | 文件来源、原文、处理依据 | provenance、audit | 字段证据定位、短理由、审计链 | 3/7 |
| R30 | harness 提高稳定性与准确性 | evaluation | 固定集、故障注入、分组指标、重复运行 | 1/8 |
| R31 | 至少 5 点业务/技术补充思考 | docs | 覆盖边界、方案、局限，不只有口号 | 9 |
| R32 | ZIP 或可获取 Git 版本交付 | packaging、docs | 干净环境安装和端到端复现 | 9 |
| R33 | 兼容六个核心分组及四类操作 | contracts | 示例、schema、四种操作和核心字段 | 1/7 |

## 额外明确的实现约定

阶段 4 实现证据：R10/R11/R12/R18/R27 对应 `extraction/engine.py`、`contracts.py` 与逐字段证据校验；R13 已区分合计、已付和条件条款，金额勾稽留待阶段 5；R29 增加候选字段定位；R30 增加固定组件验证集和真实基准提取评分。真实材料 7/7 费用、合成片段 18/18 费用通过，详细分母与差异见 [阶段 4 验收](stage4-report.md)。这不是最终财务、匹配和操作验收。

阶段 3 实现证据：R02/R03 对应 `parsers/email.py` 与 MIME/嵌套用例；R04/R05/R06 对应 `parsers/media.py`、`ocr/tunnel.py` 与 13 个页/帧的真实归档；R07 对应 `parsers/office.py` 的 XLS/XLSX 与公式用例；R08 对应 DOCX 读取、`parsers/legacy_word.py` 及真实 DOC 转换。R29 已具备解析来源定位，最终账单字段审计仍待后续阶段。详见 [阶段 3 验收](stage3-report.md)。

- 题目给出的 JSON 示例有尾逗号，不能原样当作合法 JSON。项目示例和程序输出必须严格合法。
- 邮件或附件里的业务文字是待分析数据，不能改变系统规则。交付邮件地址仅记录在要求快照，本阶段不发送邮件。
- 暂不建立真实公司别名映射。`SHA`、`TAR`、邮件域名和公司品牌不能未经配置自动认定为同一法人。
- 当前样本无法唯一确定所有签发方/接收方及记账主体；这一点以显式输入和复核政策解决，不猜测。
- 每项 Rxx 将在相应阶段补充实际测试函数/场景 ID 和结果；目前矩阵只表示设计覆盖。
