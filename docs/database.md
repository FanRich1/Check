# MySQL 账本

网页支持 MySQL 作为正式账本存储。记账公司、账本、有效费用、来源去重标识、任务、核对备注及版本历史统一保存在数据库。原文件、OCR / 模型归档和每次任务的处理产物仍保存在本机文件夹，JSON 继续作为导入、导出和证据快照格式。

## 本机使用

当前开发机器已安装并配置 MySQL Community 8.4.13，只监听 `127.0.0.1:3307`，没有安装系统服务，也没有修改已有系统数据库。`.env` 已配置数据库连接。双击 `启动账单工作台.cmd` 会先检查并启动这个实例，再打开网页。

1. 在右侧点击「新建账本」，填写账本名称和真实记账公司全称。记账主体由所选账本确定。
2. 已有旧账单时，在「查看 / 导入 / 导出账本」选择 `state.json` 或有效账单数组。只允许导入新建空账本，防止覆盖或合并不兼容的历史分支。
3. 选择账本并上传业务材料，系统自动读取该账本最新有效费用。后续使用同一账本即可匹配重复、冲突和授权修改，无需再上传上一批 JSON。
4. 结果页显示本次同步的账本版本与有效费用总数。账本管理中可查看前 100 条费用并下载完整 JSON。任务里的 `state.json` 表示该任务当时的版本；查看最新全量状态请从账本管理导出。
5. 待复核费用留在任务结果中。「已核对」仅保存备注；正式处理请在费用详情点击「正式复审 / 修正入账」，确认新增、修改旧账、重复或拒绝，预览校验后明确提交。操作与边界见 [人工复审说明](manual-review.md)。

不同账本相互隔离。同一家公司的不同账本也不会自动互相匹配，实际业务应选定一个连续使用的账本。示例任务只用于体验，不写入真实账本。

## 新机器安装或外部数据库

先运行 `安装运行环境.cmd`，然后运行 `安装MySQL账本.cmd`。安装脚本下载官方 Windows ZIP，校验其官方 MD5，数据和密码保存在 Git 忽略的本地目录。首次下载约 281 MB，需要网络。脚本不会替换现有 `.env` 模型设置或已有数据库连接。

若已有 MySQL，手动创建 UTF-8 数据库和专用用户，将连接写到本机 `.env`：

```dotenv
BILLING_DATABASE_URL=mysql+pymysql://billing_app:REPLACE_WITH_URL_ENCODED_PASSWORD@127.0.0.1:3306/shipping_billing?charset=utf8mb4
```

密码中的 URL 特殊字符需要编码。用户需要该库内的 SELECT、INSERT、UPDATE、DELETE、CREATE、INDEX、REFERENCES、ALTER 权限。当前结构版本为 1，首次启动创建缺失的表；遇到不兼容版本会停止，不会自行清空或降级。后续结构变更需单独提供迁移。

```powershell
.\.venv\Scripts\python.exe -m shipping_billing.storage.cli --env-file .env init
.\.venv\Scripts\python.exe -m shipping_billing.storage.cli --env-file .env list
.\.venv\Scripts\python.exe -m scripts.local_mysql status
```

手动启动托管 MySQL 用 `python -m scripts.local_mysql start`，等待服务就绪后再启动网页。Windows 中文路径通过 `%LOCALAPPDATA%/CheckBilling/` 下的目录联接适配，实际数据仍在项目本地目录。移动项目时，应先正常停止实例并备份；不要在运行时移动或复制数据库目录。

未配置 `BILLING_DATABASE_URL` 的环境保留原文件工作流。**已配置但连接失败时会明确报错，不会悄悄改用空 JSON 账本。** 传统 `shipping-billing run --state ...` 仍是文件命令，不会写入 MySQL；数据库账本处理请使用网页。

## 数据与事务

| 表 | 内容 |
|---|---|
| `billing_ledgers` | 账本、记账公司及当前版本 |
| `billing_bills` | 有效费用，含精确金额列、查询字段和完整扩展数据 |
| `billing_identities` | 物理来源标识与账单对应关系 |
| `billing_jobs` | 任务进度、账本版本和提交回执 |
| `billing_observations` | 每次任务观察到的费用，包含待复核项 |
| `billing_documents` | 来源文件及哈希元数据 |
| `billing_reviews` | 人工核对标记和备注 |
| `billing_review_decisions` | 正式人工复审回执、修改前后值及依据 |
| `billing_source_reviews` | 来源材料用途确认、撤销及逐次历史，不修改费用或账本版本 |
| `billing_revisions` | 每次提交的完整账本版本与哈希 |
| `billing_schema` | 数据结构版本 |

任务排队期间不冻结旧账；真正开始处理时读取最新版本。提交时锁定账本行，检查开始时的版本，并在同一事务中写入账单、来源标识、版本快照、任务回执和观察记录。写入失败则整体回滚。重复提交同一个任务回执不会再次入账。

若处理期间另一个进程先更新了账本，本次返回 `LEDGER_CHANGED`，显示未入账，禁止下载该任务的建议账本作为正式状态。用户核对后可重新提交材料；系统不会自动重复模型或 OCR 调用。待复核项不改动旧金额；同名的多条费用仍分开保存，不使用「发票号＋费用名称」作为唯一键。

金额列使用 `DECIMAL(65,30)`，超出 35 位整数或 30 位小数的值拒绝写入；完整 JSON 使用 Decimal 精确序列化。网页金额显示为后端文本，导出直接下载字节，避免 JavaScript 浮点舍入。旧账单的未知扩展字段保留。

当前面向本机和中小账本，仍加载整个所选账本交给已有匹配规则，并在事务中更新整本有效状态；尚未做海量账本的 SQL 候选检索和分批更新。没有向量数据库，也没有更改财务匹配或授权规则。网页保持仅本机访问，尚无多用户权限体系。

## 历史迁移与备份

开启数据库后的第一次启动，会将 `outputs/web/jobs` 中已有任务元数据、哈希验证通过的观察结果和核对备注迁入数据库历史。历史结果文件保留。**不会自动把每个历史任务的分支账本合并进正式账本。** 请确认一份正确的历史 `state.json`，导入新建空账本；这样可避免将测试数据或旧分支重复入账。

账本管理导出的 JSON 可恢复有效费用、来源标识和账本审计，但不包含完整任务历史或原附件。完整备份应包括 MySQL 的逻辑备份（例如使用 `mysqldump`，交互输入密码）以及 `data/local/web`、`data/local/web-cache` 和 `outputs/web/jobs`。不要直接复制运行中的 MySQL 数据目录。恢复 JSON 时导入新建空账本；完整实例恢复则使用对应 MySQL 备份工具。

托管实例数据位于 `data/local/mysql/server`，管理凭据位于 `data/local/mysql/instance.json`，应用连接位于 `.env`；这些文件和下载的运行时均不纳入 Git 和交付 ZIP。不能只备份代码仓库。

## 复现验收

```powershell
# 所有离线回归，无需数据库和外部模型
.\.venv\Scripts\python.exe -m pytest -q
# 独立 shipping_billing_test 数据库：SQLite + 真实 MySQL 相同验收
.\.venv\Scripts\python.exe -m scripts.database_checks
# 真实 Edge + MySQL，隔离的合成 EML 与注入模型，不产生外部调用
.\.venv\Scripts\uv.exe run --frozen --extra local-ocr --with playwright python -m scripts.database_browser_smoke
```

外部 MySQL 环境需先创建 `shipping_billing_test` 并授予专用用户相应权限；脚本不会使用生产账本做测试。SQLite 只用于显式启用的测试夹具，生产配置仅接受 `mysql+pymysql`。

上述测试验证存储、事务和网页链路，不是新的 OCR / 模型准确率评测。真实 OCR 空响应、原文缺失和付款关系不明等既有限制仍需复核。
