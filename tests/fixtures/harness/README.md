# 阶段 8 冻结评测资料

`manifest-development.json`、`manifest-validation.json`、`manifest-holdout.json` 是当前 30/15/15 清单。`DEV-*`、`VAL-*`、`H2-*` 是当前案例。`HOLD-*` 为初版留出反馈已用于修复的 15 个回归案例，其首次材料和答案不改写，原清单为 `consumed-manifest-holdout-v1.json`。

每案的 `input.json` 指定原始文件、旧账单、记账主体/显式业务政策及事先设计的故障；`expected.json` 为助手在预测前编制的答案，不是待测引擎生成的预测。`ocr.json` 是明确编写的受控 OCR 输入，不能当作指定 OCR 服务的真实识别记录。所有文件均为合成材料，不包含用户真实原始账单或 API 密钥。

`release.json` 是初版失败留出的冻结记录；`release-final.json` 是最终代码和当前三组数据的冻结记录。后续修改不能直接覆盖旧版本记录或继续将这批公开案例宣称为未见留出。

`replay-cache/development`、`replay-cache/validation-final`、`replay-cache/holdout` 保存对应原始模型响应及精确请求指纹。`replay-cache/validation` 是修复期间一版通过验证的历史快照，最终验收使用 `validation-final`。缓存含故障响应和有限修复尝试，不是手写预期候选；回放不访问网络。请勿在此目录写 live 缓存或预测结果。

数据划分、指标、失败记录和命令见 [阶段 8 验收记录](../../../docs/stage8-report.md)。标注尚未经过独立业务人员签核。真实材料的历史回归独立保存于本地忽略目录，不参与本合成集的百分比。
