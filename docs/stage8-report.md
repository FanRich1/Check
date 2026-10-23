# 阶段 8：完整 harness 与质量验收

验收日期：2026-10-23。本阶段的内部质量门槛已通过。新增原始文档评测器、冻结数据清单、逐项差异、三轮独立模型验证及留出集验收。结果限定于本项目的小样本和明确配置；标注由助手编制，尚未经过独立业务人员签核。

## 范围与数据

最终评测集为开发 30、验证 15、最终留出 15，共 60 个场景；另保留已消耗的初版留出 15 个场景作为回归，总计 75 个原始文档场景。原阶段 0 的 12 个开发文件逐字保留，旧 scenario-catalog 仍是当时的规划记录；实际执行清单以 `tests/fixtures/harness/manifest-*.json` 为准。

各组按来源/版式家族隔离，金额和格式变体没有拆到不同组。覆盖 EML、HTML 邮件、附件、文本/扫描/多页 PDF、PNG/JPG/多帧 TIFF、XLS/XLSX、真实 DOC、DOCX、旧账单、重复费用、更新及故障。每案包含原始文件、运行配置、独立预期答案和适用的固定 OCR 输入，清单冻结所有案例文件的 SHA-256。

本轮使用真实 DeepSeek `deepseek-flash` 经 LangChain 提取，OCR 使用明确编写的固定响应，以隔离模型变化。固定 OCR 不是真实 OCR 服务测量，也不能证明图片识别质量。私有真实材料另作历史 OCR/模型归档回归，不与合成样例混合计分。标签只在预测保存后进入评分器，不传给模型。

## 最终结果

最终代码 SHA-256：`88251d84b0a4b798b41e93d4b908488a88ff106c1d72eb5ae484f6aa19d714c4`。冻结记录见 [release-final.json](../tests/fixtures/harness/release-final.json)，完整分子分母、分组及失败历史见 [机器可读验收记录](stage8-results.json)。

源码和冻结案例由 `.gitattributes` 禁止 Git 行尾改写，保证普通克隆和归档保留本轮哈希对应的实际字节。代码指纹只覆盖应用/评分器，文档、报告收集脚本和输出不进入该指纹。

| 指标 | 验证集每轮，三轮相同 | 最终留出集 |
|---|---:|---:|
| 场景行为符合预期 | 15/15 | 15/15 |
| 费用行 precision | 26/26，100% | 24/24，100% |
| 费用行 recall | 26/27，96.30% | 24/24，100% |
| 可判定关键字段 | 154/160，96.25% | 143/143，100% |
| operation | 26/27，96.30% | 24/24，100% |
| 自动正确完成 | 20/20，100% | 17/17，100% |
| 已接受结果核心正确 | 20/20，100% | 17/17，100% |
| 关键风险拦截 | 5/5 | 5/5 |
| 非空关键字段证据可定位 | 154/154 | 143/143 |
| 文件清单及退出状态，全案检查 | 15/15 | 15/15 |
| 重传幂等 | 15/15 | 15/15 |
| 明确 AR/AP 方向 | 26/27 | 24/24 |
| 缺失字段保持未知 | 2/2 | 1/1 |

验证集的 1 条漏行来自事先设计的无效模型 JSON 故障。它仍计入 27 条费用、160 个可判定字段和 27 个操作分母；故障被明确登记并阻止写账，所以场景行为符合预期，提取指标低于 100%。不能将“场景通过”读成“所有费用提取成功”。最终留出中的 OCR 超时保留了可从 PDF 原生文字提取的费用，并输出错误及待复核状态。

三轮验证共 57 次真实模型调用，另有 6 次明确注入的无效 JSON 尝试（每轮包含一次有限修复），三轮业务一致率 27/27。最终留出发起 18 次真实调用，未使用已有预测。各轮均无代码变动，未授权覆盖、重复建账、误选旧账单和错误接受核心字段均为 0。

开发集最终行为 30/30，费用 33/34、关键字段 197/203、操作 33/34、自动完成 21/21、风险 11/11；同样保留一项故障漏行。开发成绩不参与验证/留出门槛计算。

## 失败、修复与留出轮换

开发过程中修复了同一费用行的短 PP/CC 引用、相邻行修改邮件的证据关联、中文金额边界、明确 ISO 日期，以及“没有修改”被误判为修改意图的问题。增加数字证据边界，防止把单号 `INV-40` 中的数字当作金额。

验证失败轮次均保留。先后修复了复合单据标题、明确行号前缀污染费用名、Windows 并发重复写缓存索引，以及评测器把定位中的省略 null 和显式 null 判为不同位置的问题。冲突类型仍复核，原始文本和规范化事件均保留。开发标签有两项修订，见 [开发修订审计](../tests/fixtures/harness/development-annotation-revisions.json)；验证和留出标签未根据预测改写。

初版留出 `HOLD-*` 首次仅 12/15 场景通过，5 条自动接受费用名称错误，已接受核心正确率 18/23，验收失败。根据反馈修复斜线分栏中的行号/描述/金额标签后，这 15 案只计回归，不再作为最终未见数据。其原始文件、[原清单](../tests/fixtures/harness/consumed-manifest-holdout-v1.json)、首次冻结记录和失败报告均保留。新增完整的描述优先发票面板家族 `H2-*`，在任何预测前冻结材料和标签，再重新完成三轮验证、冻结代码并执行新留出。详见 [轮换记录](../tests/fixtures/harness/holdout-rotation.json)。

## 评分与复现

按来源 ID 和具体位置做一对一对齐，再比较字段；金额不参与挑选匹配行。无法唯一对齐不给有利匹配。模型缺失、错误和 null 不缩小已知字段分母。检查完整旧账单快照、扩展字段、准确目标记录、授权变更、最终账本与输出记录一致性、关键字段引用及产物哈希。严重错误直接阻止放行。零分母写为 N/A；格式和操作分组是案例子集，同一多格式案例可出现在多组，不能将分组计数相加。

以下公开回放重新解析原始文件，并使用真实模型的原始响应归档；不读取预期 JSON 来生成业务结果。无需密钥，DOC 仍要求 Windows 桌面 Microsoft Word。输出目录必须未使用过。

```powershell
.\.venv\Scripts\shipping-billing.exe benchmark --split development --mode replay --storage tests/fixtures/harness/replay-cache/development --output outputs/stage8/demo-development
.\.venv\Scripts\shipping-billing.exe benchmark --split validation --mode replay --rounds 3 --storage tests/fixtures/harness/replay-cache/validation-final --output outputs/stage8/demo-validation
.\.venv\Scripts\shipping-billing.exe benchmark --split holdout --mode replay --storage tests/fixtures/harness/replay-cache/holdout --output outputs/stage8/demo-holdout
.\.venv\Scripts\python.exe -m pytest -q
```

真实重新评测会产生模型调用：在 `benchmark` 前加 `--env-file .env`，改为 `--mode live`，使用新的本地 `--storage` 和 `--output`。验证需 `--rounds 3`。留出 live 额外需要 `--release tests/fixtures/harness/release-final.json`；代码或案例变化时冻结校验拒绝执行。已看过的公开留出仅能作为回归，后续模型调优必须另建留出，不能重复宣称未见验收。

## 检查与限制

- 完整测试 428/428；包含超过 40 个独立确定性财务规则及匹配、故障和流程测试。
- 新的非 editable 虚拟环境从锁文件安装，导入位置确认为该环境 `site-packages`，完整测试仍为 428/428。
- 新环境公开回放开发/验证/留出共 60 案，并检查归档回放与原评测业务结果一致。
- 真实原始文件回归仍为 4 个顶层文件、7 个来源、7/7 费用的标准名称/金额/明确币种保留。未知记账主体仍复核。邮件内嵌图 `src-mail/part-1.1.2` 的历史 `OCR_EMPTY_CONTENT` 仍是明确错误；这项 OCR 质量问题没有被本阶段解决。
- 本轮新增/修改的 Python 文件通过 Ruff 检查及格式检查，13 类结构化 schema 已重新导出。旧阶段 0 两个脚本的既有全仓 Ruff 问题不在本轮改动范围，未宣称全仓格式检查通过。

小样本、助手标注、有限版式家族和固定 OCR 都限制了结论，不能宣称生产总体准确率。阶段 9 继续完成交付文档、补充思考、ZIP 打包及从交付包安装验收；本阶段不自动提交或推送 Git。
