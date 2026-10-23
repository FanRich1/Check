# 阶段 5 离线规则演示

`example-input.json` 是人工构造的阶段 4 候选及证据，不是模型或 OCR 结果。`example-context.json` 明确提供记账主体；`expected-core.json` 是独立写出的预期核心字段。该案例应规范为 Handling Charge、USD 40、PP，Buyer Ltd 视角为 AP，并通过领域检查。

完整规则案例及独立期望值在 `scripts/stage5_cases.py`；用于规则组件验证，不替代文档解析、模型稳定性或最终留出验收集。所有数据均为合成内容。
