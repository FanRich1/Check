# 数据契约 v0.1.0

阶段 0 冻结业务语义和示例；阶段 1 实现基础模型与 schema，阶段 7 已串联原始材料到最终账单流程，详见 [阶段 7 交付记录](stage7-report.md)。版本变化需记录兼容性影响。

## 1. 请求

请求对象包含 `schema_version`、`documents`、`accounting_entity`、`existing_bills`、`party_aliases`。文档以 `source_id` 和本地 `path` 或可访问 `url` 引用，并标注角色：业务文件或附属材料。

- `accounting_entity`：当前记账法人名称，可为 null。缺失时仍解析，但不能自动输出 CREATE/UPDATE；有实际费用则 PENDING_REVIEW，posting_type 为 null。不能自动取邮件发件人作记账主体。
- `existing_bills`：旧记录数组，默认空。每条旧记录有唯一 `record_id`。输入格式错误或 ID 重复是请求级错误，不视为“没有旧账单”。
- `party_aliases`：显式提供的别名到法人映射，默认空。无配置时不能认定 SHA/TAR、域名和品牌对应哪个法人。
- 后续运行配置另含 OCR、模型、文件 URL 适配器和日期区域；密钥不进入请求示例、结果或日志。

解析本地 PDF/图片前必须通过 URL 映射或上传适配器获得指定 OCR 可访问的地址。本地文本提取只服务人工检查和离线调试，不替代 OCR 实际接入。

## 2. 输出与内部候选分离

主输出是 JSON 数组，每个实际费用一条。候选提取保留缺失/冲突状态；业务决策之后生成最终记录。文件级解析失败、无费用通知和签名图片进入独立运行清单，不伪造空费用行。

核心分组保持题目约定：

| 字段 | 类型与规则 |
|---|---|
| operation | CREATE / UPDATE / NO_CHANGE / PENDING_REVIEW |
| document_info | document_type、reference_no、document_date；未知值 null |
| transaction_party | issuer、recipient；不能用 shipper/consignee 或邮件收发人替代 |
| charge_details | charge_item、amount、currency、payment_term |
| financial_attributes | accounting_entity、posting_type，后者 AR/AP/null |
| audit_trail | ai_reasoning：简短结论和证据依据；补充 sources、field_evidence、issues、changes |

允许补充 `record_id`、`existing_record_id`、`shipping_info`、`extensions`；费用分组补充 `charge_item_raw`、`quantity`、`unit_price`、`payment_term_raw` 和 `currency_raw`；单据分组补充 `document_date_raw`。这些信息不能通过补充字段覆盖核心业务语义。

无已知对应旧记录时 `existing_record_id=null`。新行身份采用来源内容身份、局部行身份与记账实体关联；不得只拼接单据编号和费用名称。同名同额也不代表重复。旧记录的 record_id 必须保留。

字段证据以 JSON Pointer 指向字段，例如 `/charge_details/amount`。每条证据保存 source_id、locator 和 quote。locator 的种类为 email_body、email_header、pdf_page、image_region、sheet_cells、word_block 或 existing_record，明确页/行/表格或旧记录字段位置；不能只有“来自 OCR”的泛泛描述。继承字段使用 `existing:<record_id>` 作为可定位的旧记录来源。

## 3. 操作决策

| 情况 | 决策 |
|---|---|
| 有有效实际费用、无旧匹配、关键字段充分且校验通过 | CREATE |
| 唯一可靠匹配，材料明确请求修改，差异在授权集合内且校验通过 | UPDATE |
| 已有同一费用，内容相同；或应用更新后的同材料再次输入 | NO_CHANGE |
| 多候选、关键字段未知、缺主体、未授权冲突或校验失败 | PENDING_REVIEW |

自动 CREATE/UPDATE 至少要求可确定金额、规范币种、记账主体、收费方向及明确的实际费用身份。单据编号本来不存在时允许 null，不虚构编号。缺少非关键数量/单价，不阻止一笔有明确总金额的费用。

PENDING_REVIEW 保留可确定候选值及具体阻断原因，不应用到旧账单。NO_CHANGE 是相同业务事实的重现，缺失主体且无法确定记录归属时仍复核。

## 4. 匹配和更新

按优先级生成候选：①单据编号+标准费用名；②提单号+费用名+币种；③订舱号+费用名+币种；④箱号+交易双方+日期+金额。缺少组合中的字段时不能用 null==null 凑匹配。

优先级不是无条件信任：每一级还检查记账主体、币种和已知硬冲突；多个候选或与明确事实冲突时复核。同单据同名多行必须用行身份等证据消歧，不能任取第一条，也不能靠新金额反推应更新哪条。

变更清单为 `{path, old, new, evidence}`。只改材料明确要求的字段；“发现和旧值不同”本身不是修改授权。冲突但无授权返回复核，不静默 UPDATE 或 NO_CHANGE。

缺失字段与显式清空不同。提取不到的 null 不覆盖旧值；只有明确指出字段及清空意图，且该字段可空时才允许清空。金额、币种、主体等必要字段被请求清空时复核。旧记录未知扩展字段完整继承。审计追加不算业务字段修改。

## 5. 标准化、金额与财务方向

- 题目六类名称映射作为初始词表；H/C 在明确费用语境可映射 Handling Charge，不能把 40HC 箱型变成费用。其他未知名称先保留原文。
- 币种必须为有效三字码，单独 `$` 无额外证据时 currency=null。金额原文本保留，输出 amount 使用数字，不是字符串。
- PP/CC 只在适用范围内映射。FREIGHT PREPAID 明确运费预付，不自动决定邮件中所有杂费；COD 放入原始付款描述/结算条件，不能等同 CC。
- 内部 Decimal；按币种和标签预先指定的舍入规则比较，不用浮点相等或全币种统一 0.01 容差；禁止 NaN/Infinity。
- 小计、税、总计、已付和未付为单据级事实，不额外生成费用行。未标明税额时为 null，不默认为 0。不同币种不能无汇率直接相加。
- 正常正向收费：记账主体为明确付款方则 AP，为明确收款方则 AR。仅为货主/通知人或与双方无关时不能推断。
- 贷项/冲销保留原关系所对应的 AR/AP 类别，用有证据的贷方/冲销符号减少相应余额；金额只翻转一次。贷项标题、负数与原账单关系不一致或无法对应时复核。
- 日期有歧义时 document_date=null，并保留原文。明确配置地区或材料中有无歧义日期依据后才规范化。文档日期在未来不自动改成今天。

## 6. 运行清单与风险编码

每个输入及附件有 source_id、父来源、内容哈希、类型和 processed / no_charge / pending_review / error 状态。正常含复核项的批次不等同程序崩溃；退出码和错误模型在阶段 1/7 实现。

初始风险编码：MISSING_ACCOUNTING_ENTITY、AMBIGUOUS_CURRENCY、AMBIGUOUS_DATE、UNKNOWN_PARTY_ROLE、PAYMENT_SCOPE_UNCLEAR、MULTIPLE_MATCHES、UNAUTHORIZED_CHANGE、TOTAL_MISMATCH、INVALID_CURRENCY、OCR_UNCERTAIN_IDENTIFIER、SOURCE_ROLE_CONFLICT。每项有 severity、blocks_automation、field 和 message。

只要某项阻断风险成立，就不能凭模型自报的高置信度自动入账。未知的非关键字段不一律阻断，是否阻断依据场景标签和上述规则明确决定。

## 7. 当前待配置但不阻塞阶段 0 的项目

- 实际记账法人及别名：运行时显式输入；真实样本基准默认未提供。
- 大模型提供方、模型、密钥、OCR 文件 URL 的提供方式：阶段 2 验证，不虚构已可用。
- DOC/XLS：阶段 3 已验证 Windows Word 转换 DOC；XLS 使用 xlrd 直接读取，详见 [阶段 3 记录](stage3-report.md)。
- 真实样本发布日期及邮件签名日期不能作为版本时间自动改写。邮件日期、正文落款、装船日期和账单日期分字段保存。

## 8. 阶段 3 解析报告

`ParseReport` 是独立的来源证据输出，结构 schema 见 [parse-report.schema.json](../schemas/parse-report.schema.json)。它不包含已接受账单决策，`billing_pipeline_executed` 固定为 false。

`sources` 保留来源 ID、父 ID、哈希、类型、原始名称与声明 MIME；无法读取的文件哈希为 null。`fragments` 包含文本、位置和提取方法，`tables` 保留原始行与位置，`ocr_results` 单独存放供应方输出。`metadata.page_inventory/ocr_page_status` 分别保存页/帧登记与 OCR 结果状态。

原生 PDF/Office 文本、指定 OCR 文本、本地辅助 OCR 文本使用不同方法标记。供应方结构化字段不改写成原文引用；未知页码不猜测，逐页输入明确对应原文件页号。解析状态 parsed/pending_review/error 与最终 CREATE/UPDATE/NO_CHANGE/PENDING_REVIEW 分属不同阶段。

## 9. 阶段 4 候选提取报告

`ExtractionReport` 记录模式 live/replay/test_injected、提示词与证据规则版本、输入哈希、证据单元及每来源的 extracted/no_charge/pending_review/error。每来源保留候选、观察项、问题和模型尝试记录。`model_invocations` 计当次模型调用；回放为 0，尝试记录内的元数据是历史事实。结构契约见 [extraction-report.schema.json](../schemas/extraction-report.schema.json)。

`ExtractedCandidate` 扩展基础 `Candidate`，补充 source_id、shipping_info、逐字段 JSON Pointer 形式的 field_evidence。日期和付款规则暂存原文，`document_date` 与 `payment_term` 不在阶段 4 强行标准化。打印的三字币种只按字面复制，ISO 有效性在阶段 5 校验；单独 `$` 保留为 currency_raw，currency 为 null。

证据单元由解析层逐行生成；空间 OCR 行保留组合框和原始识别框，组合后的引文并非重新识别的供应方原文。每条引用都验证原文子串、定位和字段值支持。物理费用行身份避免仅凭同名合并；跨来源费用去重和最终账单身份留待阶段 6。`billing_pipeline_executed=false` 表示尚未执行完整账单决策，不表示没有执行模型。

## 10. 阶段 5 领域策略与报告

`NormalizationContext`：accounting_entity、显式 party_aliases/charge_aliases、rounding、按 source_line_id 指定的 lines 策略，以及明确来源、明细行和币种的 totals。金额策略使用带原文证据的 SupportedFact；settlement 必须能证明 payer/payee，adjustment 必须证明原参考号、原付款方/收款方及贷项/冲销/折扣性质。无配置不认定 SHA/TAR 等缩写为某法人。循环、歧义别名和引用不存在明细的策略明确拒绝。

`NormalizationReport` 包含输入/策略哈希、规则与币种快照版本、来源清单、NormalizedLine 和单据金额检查。每行保留 original 与 candidate、financial_attributes、issues、events、checks 和 ready/pending_review。original 不改写；candidate 仅保存已验证且按规则规范化的值。无依据字段被清空并阻断，而非补出猜测。上下文中的记账主体通过事件的 context_pointer 记录来源，不伪造文档引文。

`ready` 是进入阶段 6 的领域检查结果，不代表已匹配、去重、授权更新或自动入账。原始来源的 OCR/提取风险不因标准化而消失。正常 invoice/debit 的已验证签发方收款、被开票接收方付款；若字段证据只证明 shipper/consignee/bank 等角色则复核；其他方向需明确的结算角色。贷项、冲销和负数需可定位原账关系，AR/AP 类别不翻转，仅将金额记为减少余额，正负原值均只处理一次。原关系冲突时保留原金额并复核。

舍入默认 exact；仅显式 HALF_UP/HALF_EVEN 会对计算期望值按该币种小数位舍入，实际打印金额不静默修约。单价可多位小数；最终金额需符合币种精度。不存在全局 0.01 容差。单据税/折扣/已付/余额均是事实，不是费用行；百分比不能当成税额，不同币种不合并。历史币种不自动推测当期精度，进入复核。结构 schema 见 normalization-context.schema.json 与 normalization-report.schema.json。

## 11. 阶段 6 匹配、修改策略与状态

`ReconciliationContext.normalization_context` 必须与生成阶段 5 报告时完全一致；引擎从原始 ExtractionReport 重算并逐值比较规范化结果，拒绝拼接其他输入或伪造 ready。可选 ParseReport 也校验完整输入哈希，之后才能使用原始文件字节哈希去重。

`lines[source_line_id]` 可指定有证据的 selector（明确 record_id）及 edits。每项 edit 包含 path、set/clear、expected_old、value 与原文证据。只允许列举的单据、双方、费用、运输字段及 extensions 叶路径，不能直接修改身份、财务属性、来源或历史审计。空值不会自动清空；clear 必须显式，核心金额/币种/费用名不可清空。非空新旧冲突，没有有效 edit 时复核。expected_old 不一致时阻断，已经等于目标值则为重传已满足。

匹配遵循第 4 节优先级；同名多候选不使用新金额挑选。强键命中但冲突时不降级绕过。原账贷项/冲销与正向单据分开。来源身份由文档哈希、具体行/区域位置和记账主体构成，不使用费用名或金额充当行号。相同文档不同实际行保留；完全重复来源有逐行 decision 和 duplicate_of，主 records 只保存一次。

`LedgerState`：bills、identities、audit_log。bills 必须有唯一稳定 record_id，保留未提供字段的缺省状态及旧账所有自定义根/嵌套字段；identities 是来源指纹到旧记录的绑定；audit_log 只追加已接受的 CREATE/UPDATE。修改在副本上应用，不改变输入。UPDATE 清单之外的业务差异会拒绝，过期快照会拒绝。合并后重新检查金额及财务约束，不能用旧值继承绕过错误。

`ReconciliationReport`：records 是四类核心账单结果，decisions 为每个输入费用行的匹配、差异、拟议变更和问题，files 为来源清单，next_state 为可保存的新快照。PENDING_REVIEW 的拟议变更只存在 decision 中，不作为已应用的 audit changes；其业务快照保持旧值。完整 state 应用于下一次运行，单独核心账单数组不保留来源索引。结构 schema 为 ledger-state、reconciliation-context、reconciliation-report。

本阶段为离线组件，model_invocations=0，billing_pipeline_executed=false；完整原始文档流水线和自动意图提取在阶段 7 集成。

## 12. 阶段 7 流水线清单与意图报告

`run` 使用文件/目录、--accounting-entity 或 ReconciliationContext、旧账数组/完整 LedgerState 及显式模型/OCR 配置。BillingRequest 继续作为可验证的交换结构，当前 CLI 不将其中 url 自动下载为输入；OCR URL 通过 --ocr-map 显式提供。配置和旧账无效时为请求错误，不能当作空账本。

`PipelineManifest` 独立于早期 RunManifest：mode 为 live/replay/offline/test_injected，billing_pipeline_executed=true，记录 run_id、发现清单、所有来源的原始名称/哈希/类型/阶段状态/记录 ID、计数、实际调用数、版本、策略/旧状态哈希及产物哈希。errors 优先退出码 3，正常复核为 1，完成为 0；CLI 请求错误为 2。目录非支持文件和排除目录均显式记录 ignored，不伪造费用。failed OCR 来源仍保留，父邮件反映子来源未完成状态。

`IntentReport` 保存逐行 LineDirective、逐来源意图提取状态、尝试和本次调用数。模型草案的引文先转为登记的 Evidence，再经来源家族、行绑定、修改语义及字段白名单验证。未明确打印旧值时，仅唯一旧匹配可提供 CAS 基线；旧值明确打印时需与引文及实际旧账一致。失败不能默认为无修改。没有候选可绑定的修改请求进入复核。

主 records.json 保持六个核心分组及四种操作，JSON 数字使用 Decimal 精确写出，不混入运行模式等顶层元数据。独立 audit.json 列出源行、匹配/领域规则及已应用变化；state.json 提供下次输入。中间阶段报告仍保持各自原有 billing_pipeline_executed=false，表示其自身仅为组件，不否定最终 manifest 的完整流程事实。

输出为新目录，manifest 最后写入并列出其他产物 SHA-256；不存在完整 manifest 时不得消费半成品。每个 JSON 原子写入，但不承诺多个文件整体事务。原始副本位于该次输出 work，回放缓存只读。重传应携带完整 state，不提供旧状态时不是持久化账本。缓存按模型配置、提示词/schema、证据及各阶段规则版本区分，回放不会悄悄重新调用模型。

## 13. 阶段 8 评测契约

每个冻结场景使用 `input.json`、原始材料、可选 `ocr.json` 与独立 `expected.json`。input 只包含实际输入文件、显式上下文、旧账单及设计故障；标签包含来源定位、业务字段、预期操作、授权变化、完整旧账后快照和风险原因。运行器先保存预测，再读取标签，不向模型提供预期值。split 清单记录所有案例文件的 SHA-256，release 同时冻结应用/评分器源码与三组清单。

`outcome.json` 记录退出码、实际/注入调用、耗时及重传；`score.json` 记录逐项差异、严重错误、真实分子分母和业务投影。`evaluation.json` 汇总分轮、格式、预期操作案例分组和业务一致率。定位比较将省略的可选 null 与显式 null 视为同义，但来源、页/单元格/区域等非空值必须精确一致。文件指标要求一案中全部来源及退出状态正确。缺失预测和已知值为 null 仍保留预期分母；未知另列，严重错误直接阻断。

规范化明确 ISO 日期、无冲突的复合标题和显式行号标签时保留原始候选与规则事件。去除行号不能按金额猜费用名：斜线布局必须同时存在同一来源/位置的完整行号、描述、line total、币种和金额，且与已验证的金额/币种一致。不能把任意商用前缀模糊删掉。原 `charge_item_raw` 保留打印文本。
