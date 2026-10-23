# 阶段 0 测试资料

`scenario-catalog.json` 规划 60 个场景槽位：开发 30、验证 15、最终验收 15。当前只物化 12 个开发场景，其余 48 个仍是计划，不得计入已运行测试或完整数据集。

每个已物化目录含 `input.eml`、`request.json` 和 `expected.json`。全部是人工编写式合成资料，由当前助手编制，域名使用 example.invalid，与真实样本无内容继承关系；不是待测提取链的预测，也尚未经过业务人员独立签核。

请求里的文件路径相对于案例目录。expected_records 是预期核心业务输出，不能把它传给提取模型。技术 record_id 可按具体身份算法变化，计分前做对应映射；更新/重传场景的旧记录 ID 必须继承，不能因为忽略新记录 ID 评分而放过身份错误。ai_reasoning 比较含义和证据，不要求逐字相同。

expected_existing_snapshot_after 只校验已有记录受到的变更；新 CREATE 记录不追加到该字段。新记录是否成功创建由 expected_records 校验。可自动处理的定义与评测规范一致。

复现资料生成（会覆盖这些已编写的合成案例）：

```powershell
python scripts/prepare_stage0_fixtures.py
python scripts/check_stage0.py --portable
```

冻结后修改生成器或材料必须更新资料版本和校验哈希。check_stage0 只检验资料完整性和预期之间的自洽性，不运行应用，也不是正式 JSON Schema 验证器。正式模型、schema 验证、断言运行器在阶段 1 及后续实现。

来源家族先按槽位声明，实际物化后还要审查模板和来源血缘，不能只靠不同 family_id 就宣称没有泄漏。留出集目前仅保存覆盖目标，没有提前编写材料和答案；以后物化时必须与开发用模板/原材料隔离。
