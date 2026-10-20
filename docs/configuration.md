# 深入配置与行为说明

README 只保留上手必需的内容；完整的环境变量参考、配置生效机制、各工具的边界行为收在本文件。

## 环境变量全表

所有配置都在 `.env` 里改，相对路径按本文件夹解析。改完重启会话即生效。

### 配置库（数据源存哪）

| 变量 | 默认 | 作用 |
|---|---|---|
| `MCP_CONFIG_MODE` | `mysql` | `sqlite` = 配置存 `data/datasources.db` 文件（推荐）；`mysql` = 存 MySQL 配置库 |
| `MCP_CONFIG_SQLITE_PATH` | `datasources.db` | sqlite 模式的配置库文件路径，可写绝对路径 |
| `MCP_CONFIG_HOST` / `MCP_CONFIG_PORT` | `127.0.0.1` / `3306` | mysql 模式：配置库连接 |
| `MCP_CONFIG_USER` / `MCP_CONFIG_PASSWORD` / `MCP_CONFIG_DATABASE` | `root` / 空 / `mcp` | mysql 模式：配置库账号与库名 |
| `MCP_CONFIG_PROBE_TTL` | `2.0` | mysql 模式版本探测结果的缓存秒数；`0` = 每次查询都探测 |

mysql 模式初始化：在配置库执行 `docs/mysql_config.sql`，或双击 `start_datasource_manager.bat`
按提示一键初始化。

### 连接与查询

| 变量 | 默认 | 作用 |
|---|---|---|
| `MCP_CONNECT_TIMEOUT` | `10` | 连接超时（秒） |
| `MCP_READ_TIMEOUT` | `30` | 读取超时（秒） |
| `MCP_MAX_RESULT_ROWS` | `1000` | 无 LIMIT 时的最大返回行数（截断） |
| `MCP_MAX_RETRY_COUNT` / `MCP_RETRY_DELAY` | `3` / `1.0` | 失败重试次数与间隔（秒） |
| `MCP_MAX_POOL_SIZE` | `10` | 连接池最大连接数 |
| `MCP_POOL_IDLE_TIME` | `300` | 连接最大空闲时间（秒），定期清理 |
| `MCP_SUMMARY_THRESHOLD` / `MCP_SUMMARY_DISPLAY_ROWS` | `50` / `10` | 超过阈值自动摘要，只显示前 N 行 |

### 写操作确认

| 变量 | 默认 | 作用 |
|---|---|---|
| `MCP_WRITE_CONFIRM` | `on` | 写操作确认总开关；`off` = 不弹窗、不预估 |
| `MCP_WRITE_CONFIRM_UI` | `auto` | 确认通道：`auto`（弹窗优先，失败降级终端）/ `native` / `elicit` |
| `MCP_WRITE_CONFIRM_PREVIEW_LINES` | `8` | 弹窗里 SQL 最多显示几行，超出折叠（完整语句始终进服务端日志） |
| `MCP_WRITE_ESTIMATE` | `on` | 影响行数预估开关；`off` 后弹窗只显示 SQL |

只想让某个开发库不弹窗：把该数据源的 `write_confirm` 列改成 `0`（GUI 里取消打勾），
生产库照旧拦截。全局开关与数据源开关是"与"的关系——全局开着时，本列只能放宽单个数据源，
不能反向收紧。

### 事务

| 变量 | 默认 | 作用 |
|---|---|---|
| `MCP_TXN_IDLE_TIMEOUT` | `600` | 全局默认空闲超时（秒），超时自动回滚释放 |
| `MCP_TXN_MAX_IDLE` | `86400` | 单事务空闲超时的天花板（24 小时），`begin`/`extend` 传超过它的值会被拒绝 |

`begin_transaction(idle_timeout=)` 可在开事务时按需放宽本事务的阈值，不必调大全局默认
（那会让忘记提交的事务更久才放锁）；`extend_transaction(txn_id, extend_seconds=)` 在
运行中续期，只升不降。**必须在过期之前调用**——过期后 `txn_id` 已释放、连接已回滚关闭，
只能重新 `begin_transaction`。

### 导入导出与审计

| 变量 | 默认 | 作用 |
|---|---|---|
| `MCP_EXPORT_BATCH` / `MCP_IMPORT_BATCH` | `2000` / `500` | 每批从游标读取 / executemany 提交的行数 |
| `MCP_EXPORT_MAX_ROWS` | `5000000` | 导出/导入行数硬上限（防爆盘） |
| `MCP_AUDIT_LOG_DIR` | `logs` | 日志目录；`off` = 关闭审计 |
| `MCP_AUDIT_LOG_SCAN_DAYS` | `30` | `query_audit_log` 默认扫描最近几天的文件（按文件名日期判定，不看 mtime）；传 `0` 不限 |
| `MCP_AUDIT_LOG_RETAIN_DAYS` | `0` | `>0` 时启动时删除更早的日志文件；`0` = 永久保留 |

## 配置变更生效机制（秒级自动感知）

不需要调 `refresh_datasources`，也不需要重启——MCP 每次查询前轻量探测配置源是否变化：

| 模式 | 探测方式 | 开销 |
|------|---------|------|
| sqlite | stat `data/datasources.db`（含 `-wal`/`-shm`）的 mtime | 微秒级，纯本地 |
| mysql | `SELECT version FROM mcp_config_meta`（GUI 写完自动 +1） | 主键单行查询，结果按 `MCP_CONFIG_PROBE_TTL` 缓存 |

- 外部工具（GUI / DB Browser / Navicat）改完配置 → 最迟 `MCP_CONFIG_PROBE_TTL` 秒后自动生效
- `refresh_datasources` 永远强制全量重读，不受 TTL 限制
- MySQL 配置库账号无建表权限时自动降级为旧行为（每次全量查询），不影响使用

## 影响行数预估的边界

`execute` 弹窗里的"预计影响 N 行"是在专用连接上把语句**试执行一遍、读 rowcount、立即回滚**
得到的。之所以不改写成 `SELECT COUNT(*)`：改写要解析整句句型（JOIN、子查询、多表更新、
别名），正则漏一种就算错——而错的行数比没有行数更危险。要知道的代价：

- 只对**纯 DML** 生效。DDL（CREATE/ALTER/DROP/TRUNCATE）回滚不了，估了也是假数字，
  故直接标 `is_ddl` 不给预估
- **事务内执行不预估**（`execute` 传了 `txn_id`）——在别人事务里跑一次会打断事务语义
- 试执行的副作用：**自增/序列值会被消耗**（回滚不归还，号段出现空洞）、
  **触发器会真实触发一次**（带外部副作用的触发器会被点燃）。介意就设 `MCP_WRITE_ESTIMATE=off`
- 试执行就报错的语句，预估返回 `预执行未通过（<错误原文>`——等于提前替你做了一次
  语法/权限校验，不必等点完"接受执行"才看到同一条报错

## explain_query 与 slow_queries 的前置条件

`explain_query` 把五种方言的 EXPLAIN 语法差异吃掉，并顺手把计划翻译成结论
（`warnings` + `verdict`: ok / needs_attention / poor）。覆盖：全表扫描、有可用索引但
优化器没用上、`Using filesort`、`Using temporary`、Block Nested Loop Join、行数估算偏差
超 10×（提示该 `ANALYZE` 了）。

- `analyze=true` 会**真实执行一遍该 SELECT**，大表上有实际耗时成本，故默认关闭；
  只读守卫与 `query` 完全同一道
- 自动诊断只在表格格式（MySQL TRADITIONAL / PG TEXT）下进行；`FORMAT=JSON/TREE` 时
  `warnings` 为空并在 `note` 里说明跳过——**不是"没问题"**
- Oracle 无 `EXPLAIN ANALYZE` 等价物，降级为普通计划；达梦未经真机验证，降级并在 `note` 说明

`slow_queries` 按方言尽力而为，拿不到就在 `note` 里说清缺什么，并回落给出正在执行的语句：

| 方言 | 数据来源 | 前置条件 |
|---|---|---|
| MySQL | `mysql.slow_log` | `slow_query_log=ON` 且 `log_output=TABLE`；否则回落 `SHOW FULL PROCESSLIST` |
| PostgreSQL / GaussDB | `pg_stat_statements` + `pg_stat_activity` | 前者需 `CREATE EXTENSION pg_stat_statements` |
| Oracle / 达梦 | `v$session` / `v$sql` | 需 DBA 或 `SELECT ANY DICTIONARY` 类权限 |

MySQL 想让本工具读到慢日志：`SET GLOBAL slow_query_log=ON; SET GLOBAL log_output='TABLE';`
