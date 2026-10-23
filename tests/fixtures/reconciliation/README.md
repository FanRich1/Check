# 阶段 6 合成示例

这里全部是人工构造的组件数据，不含真实客户材料。

`create-records.json`、`update-records.json`、`no-change-records.json`、`pending-review-records.json` 展示四类最终操作。`example-*` 是更新示例的输入：OLD1 的金额从 USD 40 改为 USD 50，原始邮件证据明确要求修改金额，其他字段及 ERP 扩展保持原值。

从仓库根目录运行：

```powershell
.\.venv\Scripts\shipping-billing.exe reconcile tests/fixtures/reconciliation/example-normalized.json --extraction tests/fixtures/reconciliation/example-extraction.json --context tests/fixtures/reconciliation/example-context.json --state tests/fixtures/reconciliation/example-old-state.json --output outputs/reconciliation.json --records-output outputs/records.json --state-output outputs/next-state.json
```

将 `--state` 换为 `outputs/next-state.json`，再次执行应得到 `NO_CHANGE`，业务快照和历史审计均不增加。保留完整 state 才能同时保留物理来源去重索引。
