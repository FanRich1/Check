# 阶段 1 交付记录

日期：2026-10-22。目标为工程骨架、运行契约、配置、CLI 与最小评测，不包含后续业务流水线。

## 交付物

| 类别 | 位置 | 已实现内容 |
|---|---|---|
| 安装与锁定 | pyproject.toml、uv.lock、.python-version | Python 3.12；哈希锁定运行/开发依赖，固定构建依赖 |
| 模型 | src/shipping_billing/models.py | 请求、文档片段、候选、旧快照、最终记录、证据、变更和运行清单 |
| JSON | src/shipping_billing/io.py | Decimal 无损读写、拒绝 NaN/Infinity/重复 key、原子替换 |
| 配置 | src/shipping_billing/config.py、.env.example | 显式 dotenv、离线默认、在线配置必填、SecretStr 与安全摘要 |
| CLI | src/shipping_billing/cli.py | validate、schema、config-check、evaluate、保留 run 入口 |
| 评测骨架 | src/shipping_billing/evaluation.py | 独立预测与标签比较、数量/分子分母/差异、非零失败码 |
| 结构 schema | schemas/ | request、records、candidate、manifest 的 JSON Schema |
| 模块边界 | parsers、ocr、extraction、domain、pipeline | 待后续实现的包边界；未用空模块伪称功能完成 |
| 测试 | tests/unit、tests/integration | 契约、精度、身份、配置、错误、CLI、失败比较等 |

运行依赖固定为 Pydantic 2.14.1、pydantic-settings 2.16.1、LangChain 1.4.7、simplejson 4.2.0，具体传递依赖及哈希以 uv.lock 为准。安装 LangChain 不代表已完成真实模型接入。

## 实际验证

本次主环境和独立空虚拟环境均按锁定依赖验证，两边各 65/65 测试通过；CLI 校准演示 7/7；Ruff 检查和格式检查通过；`uv lock --check` 通过。执行证据保存在 outputs/stage1 中的 pytest.xml、pytest-clean.xml、smoke/smoke-report.json 和 acceptance.json。

```powershell
.\.venv\Scripts\python.exe -m pytest --junitxml=outputs/stage1/pytest.xml
.\.venv\Scripts\python.exe scripts/stage1_smoke.py
.\.venv\Scripts\ruff.exe check src tests/unit tests/integration tests/conftest.py scripts/export_schemas.py scripts/stage1_smoke.py
.\.venv\Scripts\ruff.exe format --check src tests/unit tests/integration tests/conftest.py scripts/export_schemas.py scripts/stage1_smoke.py
.\.venv\Scripts\uv.exe lock --check
```

空环境复现方式（不删除或替换原开发环境）：

```powershell
$stage1PreviousEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
  $env:UV_PROJECT_ENVIRONMENT = 'outputs/stage1/clean-env'
  & .\.venv\Scripts\uv.exe sync --frozen --link-mode copy
  if ($LASTEXITCODE -ne 0) { throw 'Clean environment sync failed' }
} finally {
  $env:UV_PROJECT_ENVIRONMENT = $stage1PreviousEnvironment
}
.\outputs\stage1\clean-env\Scripts\shipping-billing.exe --help
.\outputs\stage1\clean-env\Scripts\python.exe -m pytest --junitxml=outputs/stage1/pytest-clean.xml
```

这里是同一检出目录的独立虚拟环境安装，验证依赖隔离和可安装性；最终 ZIP 解压后的跨目录复现安排在阶段 9，不能把本次验证夸大为完整交付验收。

## 阶段门槛对应

- 新环境安装、包导入、控制台入口和模块入口均可用。
- 12 个已编写案例的请求与预期结果通过运行模型及结构 schema 检查。这是标签契约检查，不是模型预测正确率。
- 字符串/布尔金额、非有限值、错误日期/枚举、重复旧 ID、缺少关键事实的自动操作均被拒绝。
- 旧快照的根级未知字段和 extensions 不丢失；已知分组仍严格校验。未实现实际增量合并，不声称旧字段继承业务已经验收。
- 证据来源必须登记；UPDATE 有匹配身份和非空变更证据；复核有阻断原因；NO_CHANGE 不绕过记账主体校验。
- 离线无密钥可运行，在线配置缺项明确失败；错误输出不回显原始输入中的测试密钥。
- 相同的合成预测返回 0；人为改错金额返回 1；漏行、无效预测也失败并保留分母；错误请求返回 2。
- `run` 返回 4 / PIPELINE_NOT_IMPLEMENTED，不写伪造 CREATE 或成功空数组。

## 当前限制与后续接口

1. 结构 JSON Schema 不能表达全部跨字段业务约束；必须配合 Python 模型。三字母币种形状检查不等于完整 ISO 4217 成员校验，后者在阶段 5。
2. 最小评测按行顺序对齐、比较业务内容及风险编码/变更值；不评估完整证据真实性、旧快照应用或所有阶段 8 指标。最终 provenance 对齐与全套指标在后续扩展。
3. 演示预测由合成标签构造以校准比较器，报告明确 pipeline_executed=false，不能视为端到端预测或模型准确率。
4. 当前日志只记录安全错误码；完整文件运行清单的业务生成、阶段追踪和部分失败处理在阶段 7。
5. 缺少真实模型密钥、提供方、可访问文件 URL 不阻塞阶段 1；阶段 2 必须真实验证 OCR 和模型，不能靠 config-check 宣称连通。
6. 运行环境 Windows/Python 3.12.10 已验证，其他操作系统尚未验证。测试不需要任何 OCR/模型网络请求。
