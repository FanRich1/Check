# 阶段 6：旧账单匹配、去重与授权更新

验收日期：2026-10-23。阶段 6 完成，下一阶段是将解析、OCR、模型提取、规则及账单决策串联到 `run`。本阶段为确定性组件验收，没有新增 OCR 或模型网络调用，不代表端到端准确率或生产总体准确率。

## 交付内容

- `reconciliation/contracts.py`：旧账快照状态、逐行选择与修改指令、四类操作及完整报告。
- `matching.py`：四级组合键、法人/币种/运输标识冲突检查与物理来源身份。
- `intents.py`：修改证据需来自已登记的原文与位置，必须同时支持字段、修改动作及新值；否定指令、单纯发现新值及材料内系统指令不能授权更新。
- `engine.py`：唯一匹配、授权字段变更、继承、合并后金额及财务检查、批内冲突与独立快照应用。
- `reconcile` CLI、3 个新结构 schema、[四类操作示例](../tests/fixtures/reconciliation/README.md)、64 个业务场景及额外的输入、防篡改和命令行测试。

## 业务行为

先按单据参考号和费用名匹配，再按主/分单号、订舱号、箱号与双方/日期/金额的组合匹配。缺失键不能相等。高优先级候选有冲突时不能绕到弱键选择另一条。记账法人、币种及明确的运输标识冲突均保留为复核原因。多个同名候选不能靠新金额挑选；只有有证据的明确记录指向才能在符合业务键的候选中消歧。

费用行身份与费用名称分离。同一文件的两条实际费用行即使同名同金额也分别创建；同物理来源、文件改名副本、唯一业务匹配的跨来源副本合并为一条主记录，每个输入行仍有独立 decision。来源字节哈希通过可选的 `--parse-report` 获取，且验证它确属该提取输入；未提供时使用原文证据摘要。只有 PDF 页码、没有行/区域定位时不自动创建。

匹配旧账后，缺少金额、币种或一方名称时，仅在唯一旧账可提供确定值且没有额外阻断问题的情况下继承。单独 `$`、缺少记账主体及无依据的原文仍阻断。已知新旧值不同必须有逐字段修改依据；即使旧字段为空，也不擅自补齐。空缺不等于清空，显式清空也不能删除金额、币种和费用名。

变更清单逐项保存旧值、新值和证据，并校验 expected_old。新值与提取到的已知字段矛盾时复核。合并后重新检查金额精度、原有数量×单价以及财务关系；金额单改会破坏旧账计算关系时不接受。同一批对同一旧账提出不同目标值时整条旧账暂不修改。

旧账根部及嵌套自定义字段、未提供的可选字段、历史审计字段均保留。新审计与来源身份放在 `LedgerState.audit_log/identities`，不注入调用方 extensions。快照应用检查旧值未过期、字段白名单和授权路径之外没有业务变化。所有工作在拷贝上完成，输入旧账文件不会原地修改；输出是可检查的新快照，不写外部财务系统。

## 实际验收

| 检查 | 结果 |
|---|---:|
| 独立业务场景 | 64/64 |
| 可自动处理场景 | 35/35 |
| 应复核场景 | 29/29 |
| 应用快照、JSON 往返后重传 | 64/64，无再次 CREATE/UPDATE，快照与审计不变 |
| 报告结构 schema | 64/64 |
| 开发环境完整测试 | 349/349 |
| 独立非编辑安装完整测试 | 349/349 |
| 真实回放 | 7/7 条费用、7/7 来源保留；缺记账主体均复核，不建账 |

64 个场景共 70 个输入行：12 CREATE、14 UPDATE、14 NO_CHANGE、30 PENDING_REVIEW。计数按逐行 decision；重复来源只输出一次主账单记录。未授权覆盖、错误选择旧账、同名误合并及重复创建在本验收集中的观察次数均为 0。自动场景包括新增、金额/单价/币种/参考号/费用名修改、显式清空、嵌套扩展修改和原值继承，并非全部返回待复核。

逐例实际输出、预期、差异及重传结果保存在本地 `outputs/stage6/check/summary.json`。真实材料报告是私有产物，不纳入 Git。

## 复现命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m scripts.stage6_smoke --portable --output outputs/stage6/portable
.\.venv\Scripts\python.exe scripts/export_schemas.py
.\.venv\Scripts\ruff.exe check src tests scripts/stage6_cases.py scripts/stage6_smoke.py scripts/stage6_acceptance.py scripts/export_schemas.py
```

不加 `--portable` 会读取开发机已归档的阶段 4/5 真实结果；公开仓库用 portable 模式即可，不要求真实文件或密钥。

干净环境验证使用 `UV_PROJECT_ENVIRONMENT=outputs/stage6/clean-env`，执行 `uv sync --frozen --no-editable --link-mode copy --reinstall-package shipping-billing-assistant --refresh-package shipping-billing-assistant`，然后使用该环境的 Python 运行完整 pytest 与 portable smoke。验证导入路径位于新环境 `Lib/site-packages`。首次尝试命中旧版本地 wheel 缓存，发现新增模块无法导入后，已改用上述强制重新构建命令；旧缓存尝试不计为通过。

开发及干净环境 pytest 分别使用 `--junitxml=outputs/stage6/pytest.xml` 和 `--junitxml=outputs/stage6/pytest-clean.xml`。完成本地真实回放与 `clean-check` 后，运行 `python scripts/stage6_acceptance.py` 汇总为 `outputs/stage6/acceptance.json`；本次 passed=true，11 个结构 schema 有效，可分发文件密钥扫描匹配数为 0。

## 边界及下一阶段

本阶段修改意图由显式 `ReconciliationContext.lines` 输入，并且必须有原文证据；尚未让模型自动生成这些指令。修改文字验证目前是受限的中英文规则，不承诺理解任意自然语言，无法证明时复核。阶段 7 接入完整工作流时需继续验证该边界。

应保存完整 `next_state`；仅保存核心账单数组会丢失物理来源索引，在多条同名旧账时可能重新进入多候选复核。没有清晰行定位的页级 OCR 片段、复杂税费变化、未经配置的法人缩写和重复的多行跨来源材料均优先保留复核，不擅自推断。

`billing_pipeline_executed=false` 表示尚未执行阶段 7 的原始文档到最终结果完整流程，不表示本阶段没有执行账单匹配。`run` 仍返回未实现；阶段 6 没有自动推送 Git。
