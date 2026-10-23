# 阶段 5：标准化、金额与 AR/AP 规则

2026-10-23 完成领域规则验收，规则版本 `stage5-domain-v1`。本阶段对阶段 4 提取报告执行确定性规则，不调用模型、OCR 或外部账本。单据匹配、去重、增量变更和最终操作留待阶段 6–7。

## 交付行为

- 费用名称：实现题目六类映射，包括 Handling Fee/H/D Fee、Doc Fee、THC、Trucking Fee、Customs Fee、Storage Fee；明确费用语境的 H/C 映射 Handling Charge。原文始终保留；未知名称不强行分类。只有已核对的 `OCA17 On Carriage Additional - US Em` 组合去掉相邻代码，不按通用“删除数字前缀”规则改名。单独箱型如 40HC 要求复核。
- 币种：检查代码和 minor unit，使用显式项目别名如 RMB→CNY、US$→USD。单独 `$`、`¥`、`£` 等不猜测。非结算精度代码、非法码、与原值冲突或历史代码进入复核。原始币种文本保留。
- 付款：PP/CC、PREPAID/COLLECT 等按明细、文档或运费范围解释。FREIGHT PREPAID 不自动施加到杂费；COD 保存为结算条件，不当成 CC；账期保留。无法映射用 null；矛盾付款描述会阻断。
- 金额：Decimal 精确计算，按币种小数位检查打印金额；单价可以有更多位。quantity×unit_price 可加有证据的税额、减有证据的折扣。未提供数量/单价不妨碍明确行金额，检查记录为 not_checkable。默认 exact，显式 HALF_UP/HALF_EVEN 仅用于计算期望值，不修约打印金额；不使用固定 0.01 容差。数值最大支持 128 位有效数字、指数绝对值 128，超限明确复核/拒绝。
- 合计：按来源、行集合和币种核对 subtotal、total、paid、balance；原文中明确且无歧义的合计可自动构造检查范围，复杂范围可在 context 明确提供。税额不能从税率百分号猜出，其他字段的数值不能冒充总计；明确不同币种的税/折扣也不跨币种相加。合计和已付不生成费用行，缺失/冲突行不从核对范围静默消失。
- AR/AP：采用项目数据契约的约定，结合主体、已验证的账单双方和收费方向判断。普通 invoice/debit 的收款方为 AR、付款方为 AP；其他单据可显式提供带证据的 payer/payee。货主、通知人、银行受益人不能直接替代交易双方。主体缺失、第三方主体、自交易、角色不明时不猜方向。
- 贷项/冲销：需原参考号、原付款/收款方以及性质证据；保留原 AR/AP 类别，将金额记为减少余额，正数原值和已为负数的原值均只处理一次。折扣与负数同样不靠符号自行猜原账关系。原账匹配在阶段 6 实现，当前支持材料中明确的关联事实。
- OCR：保留上游风险，不按字符串相似度猜编号。可显式提供受限的 0/O、1/I 纠正，但必须有同来源族的原生文本、正确字段标签与字面值佐证；保留原值和规则事件，不清除原有宽泛 OCR 风险。

每个规范化字段保留原文证据或显式策略引用；来源错误、成功无费用和待复核分别登记。输入候选不被修改；输出同时保存 original、candidate、规则事件、检查结果和金融属性。ready 表示领域检查通过，尚不是 CREATE/UPDATE，也未操作账本。

## 币种数据来源

178 个当前代码和小数位来自 [SIX 当前币种 XML](https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/lists/list-one.xml)，发布标记 2026-09-17；137 个历史代码来自 [SIX 历史币种 XML](https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/lists/list-three.xml)，发布标记 2026-01-01。SIX 是 ISO 4217 维护机构，相关说明见 [ISO 币种代码说明](https://www.iso.org/iso-4217-currency-codes.html)。项目打包 factual codes/minor units 的 JSON 快照，包含源 URL、下载日期和原 XML SHA-256；运行时无需联网查询。历史代码目前要求日期相关人工复核，不自动猜精度或使用替代币种。

## 验收结果

| 项目 | 实际结果 |
|---|---|
| 固定规则案例 | 63/63 通过 |
| 明确 AR/AP 方向案例，含贷项和冲销 | 8/8 正确 |
| 预设风险正确复核 | 19/19 |
| 信息充分的明确案例通过领域检查 | 44/44 |
| 开发环境完整测试 | 267/267 |
| 全新非 editable 安装完整测试 | 267/267 |
| 真实材料费用及规范化核心值 | 7/7 |
| 真实单据合计 | 26.90、245、1410.40 均正确 |

63 个案例独立写明期望值，覆盖名称、币种、付款范围、精度、舍入、贷项/冲销/负数、多币种、同名同额及上游风险；额外测试覆盖错误税率/字段、跨币种税额、伪造引用、角色冲突、旧格式证据转存和 CLI 退出码。两环境均由锁文件安装/验证；干净环境从 wheel 安装而非读取开发源码，确认币种数据随包安装。相同输入的重复运行和两环境真实样本输出一致。schema 校验覆盖 63 份规则报告、1 份真实报告和 1 份公开演示报告。

真实材料仍保持 7 条独立费用：两条 Handling Charge 的 65 与 35 不合并，H/C 50 标准化为 Handling Charge 且 `$` 仍未知，CMA 费用名已去掉 OCA17。由于当前真实运行未设置记账主体，且存在 OCR/交易方不确定项，全部保留复核、posting_type=null，没有编造 AR/AP。这些是风险处理的正确结果；明确合成案例通过说明系统没有靠全部复核绕过业务规则。

本阶段基准由助手编制，未独立业务人员签核。规则案例不是文档端到端留出集，也不证明生产总体准确率。前阶段 OCR 识别质量问题未被声明消失。

## 运行方式

从仓库根目录执行，无需模型密钥：

```powershell
# 完全公开的合成演示，预期 ready/AP/USD 40/Handling Charge/PP
.\.venv\Scripts\shipping-billing.exe normalize tests/fixtures/domain/example-input.json --context tests/fixtures/domain/example-context.json --output outputs/stage5/demo.json

# 实际候选：显式主体；不指定主体时仍输出费用并要求复核
.\.venv\Scripts\shipping-billing.exe normalize outputs/stage4/real-final-replay.json --accounting-entity "当前记账法人" --output outputs/stage5/normalized.json

# 规则组件验收：portable 不依赖本地真实样本
.\.venv\Scripts\python.exe scripts/stage5_smoke.py --portable --output outputs/stage5/portable
.\.venv\Scripts\python.exe scripts/stage5_smoke.py --output outputs/stage5/check
.\.venv\Scripts\python.exe -m pytest -q --junitxml=outputs/stage5/pytest.xml
.\.venv\Scripts\python.exe scripts/stage5_acceptance.py
```

`normalize` 的 0/1/2/3 分别表示领域检查通过、含复核项、输入/配置无效、有来源错误或运行失败。来源级失败不伪造成成功空数组。`--context` 和 `--accounting-entity` 不能同时提供；策略例子及 schema 在 `tests/fixtures/domain/`、`schemas/normalization-context.schema.json` 和 `schemas/normalization-report.schema.json`。

本地验收文件：`outputs/stage5/acceptance.json`、`check/evaluation.json`、`check/real-normalized.json`、`demo.json`、`pytest.xml`、`pytest-clean.xml`。原始文件、密钥、真实候选和运行报告继续 Git 忽略。Git 交付状态以仓库提交历史为准，阶段 4 提交保留。
