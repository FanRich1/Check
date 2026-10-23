# 阶段 7 原始文件演示

这里全部为合成数据。`inputs/new.eml` 是 USD 40 的费用，`inputs/amendment.eml` 明确要求将金额从 40 改为 50。`invoice.doc` 是 Microsoft Word 生成的真实旧版 DOC，用于格式验收。

`replay-cache` 保存 2026-10-23 对上述两封合成邮件实际调用 DeepSeek / LangChain 得到的三个响应：两次费用提取、一次修改意图提取。它们是历史真实响应；回放不调用模型，不代表新的实时验证。档案包含合成原文及模型返回，不含密钥、真实客户材料或期望的 operation 标签。operation 由当前规则计算。

以下从仓库根目录运行，不需要密钥，`--output` 使用新的目录：

```powershell
.\.venv\Scripts\shipping-billing.exe run tests/fixtures/pipeline/inputs/new.eml --accounting-entity "Buyer Ltd" --storage tests/fixtures/pipeline/replay-cache --output outputs/demo-create
.\.venv\Scripts\shipping-billing.exe run tests/fixtures/pipeline/inputs/amendment.eml --accounting-entity "Buyer Ltd" --state outputs/demo-create/state.json --storage tests/fixtures/pipeline/replay-cache --output outputs/demo-update
.\.venv\Scripts\shipping-billing.exe run tests/fixtures/pipeline/inputs/amendment.eml --accounting-entity "Buyer Ltd" --state outputs/demo-update/state.json --storage tests/fixtures/pipeline/replay-cache --output outputs/demo-repeat
```

预期依次为 CREATE、UPDATE、NO_CHANGE。每次目录内有 records.json、state.json、manifest.json、audit.json 及中间报告。完整自动检查：

```powershell
.\.venv\Scripts\python.exe -m scripts.stage7_demo --mode replay --storage tests/fixtures/pipeline/replay-cache --output outputs/demo-checked
```

另外，`scripts/stage7_smoke.py` 与集成测试使用明确标记为 test_injected 的模型/OCR 替身来验证流程和故障；它们与本目录真实响应回放、以及需要显式配置的 live 调用分别计数。
