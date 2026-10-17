# db-auto-router — 多数据源 MCP 查询路由 (v4.7)

AI 客户端（Claude Code / Claude Desktop / Cursor / Continue 等）通过 MCP 协议调用本地脚本，
根据当前工作目录自动加载数据库配置，实现多项目、多数据库的安全查询路由。

支持的数据库类型：MySQL / 达梦(DM) / PostgreSQL / GaussDB(华为高斯) / Oracle

## 目录结构

```
db-auto-router/
├── db_mcp_router.py            # MCP 启动入口（.claude.json 引用此文件）
├── .env                        # 全部配置（模式切换 / MySQL 连接 / 路径覆盖）
├── requirements.txt            # 依赖清单
├── start_datasource_manager.bat # 双击启动数据源管理 GUI
├── db_auto_router/             # MCP 核心包（server/tools/连接池/SQL守卫/事务/导出导入…）
├── tools/                      # 数据源维护工具
│   ├── manage_datasources_gui.py   # 可视化管理（双后端：跟随 .env 编辑 MySQL 或 SQLite）
│   ├── manage_datasources.py       # 命令行维护
│   └── migrate_datasources.py      # MySQL 配置库 → SQLite 一键迁移
├── docs/
│   ├── sqlite_template.sql         # SQLite 配置表字段说明
│   └── mysql_config.sql            # MySQL 模式建表 SQL
├── data/
│   └── datasources.db              # SQLite 配置库（sqlite 模式的数据源存储）
└── logs/                       # 审计日志（每条 SQL 一行 JSON）
```

## 数据源配置存哪？（.env 的 MCP_CONFIG_MODE 切换）

| 模式 | 配置存放 | 适合谁 | 额外要求 |
|------|---------|--------|---------|
| `sqlite`（推荐） | `data/datasources.db` 文件 | **拿到文件夹直接用的人** | 无（Python 自带，零安装） |
| `mysql` | 独立 MySQL 服务的 `mcp` 配置库 | 已有全套环境的老用户 | 本地 MySQL 服务 |

### SQLite 模式怎么维护数据源？

三种方式任选：

1. **可视化小工具**（推荐，双击即用）
   - 双击 `start_datasource_manager.bat`（或 `python tools/manage_datasources_gui.py`）
   - 图形界面：列表 / 添加 / 编辑（双击行）/ 删除 / 测试连接 / 刷新数据源
   - **双后端**：GUI 自动跟随 `.env` 的 `MCP_CONFIG_MODE`——
     sqlite 模式编辑 `data/datasources.db`，mysql 模式直接编辑 MySQL 配置库，
     **弹窗顶部会标注当前写入目标**（蓝色 SQLite / 橙色 MySQL），改的永远是 MCP 正在读的那个库
   - 也可 `python tools/manage_datasources_gui.py --backend sqlite|mysql` 强制指定后端
   - tkinter 实现，Python 自带零依赖
   - v4.4 起表单多一项 **Schema/Owner**，列表也多对应一列（见上文《db_schema 列》）

2. **DB Browser (SQLite)**（图形界面，直接编辑文件）
   - 免费下载：https://sqlitebrowser.org/ （或搜 "DB Browser for SQLite"）
   - 打开 `data/datasources.db` → 「浏览数据」标签 → 选中 `mcp_datasource_config` 表 → 「插入新记录」
   - 必填 4 格：`db_type`（mysql/dm/postgres/gaussdb/oracle 之一）、`conn_url`、`db_user`、`db_password`
     —— **`conn_url` 的格式随方言而变，见下方《conn_url 填写格式》**
   - 其余格子：`dir_path` 保持 `*`（=任意目录可用）、`is_default` 填 `1`、`db_name`/`db_code` 随意、
     `db_schema` 留空（除非默认 schema 不是 public / 登录用户）
   - 点「写入更改」保存即可——**MCP 通过文件 mtime 秒级感知，无需任何刷新操作**

### 📐 conn_url 填写格式（五种方言不同，填错连不上）

`conn_url` 不是统一格式的——它被原样交给对应驱动，各方言要的东西不一样：

| db_type | conn_url 格式 | 示例 | 数据库/schema 从哪来 |
|---|---|---|---|
| `mysql` | `主机:端口` | `10.0.0.1:3306` | **`db_code`**（见下方说明） |
| `dm` | `主机:端口`（dmPython DSN） | `10.0.0.1:5236` | DM 用户的默认 schema（`db_code` 仅作标签） |
| `postgres` | libpq 连接串，**必须带 dbname** | `host=10.0.0.1 port=5432 dbname=mydb` | `conn_url` 里的 `dbname` |
| `gaussdb` | 同 PostgreSQL（兼容 PG 协议） | `host=10.0.0.1 port=15432 dbname=mydb` | `conn_url` 里的 `dbname` |
| `oracle` | Easy Connect / TNS 别名 / 完整描述符 | `10.0.0.1:1521/orcl` | `conn_url` 里的服务名 |

**几个容易踩的点：**

- **MySQL 的库名取自 `db_code`，不是 `conn_url`**。虽然 `10.0.0.1:3306/mydb` 这种写法能被解析，
  但斜杠后面那段会被忽略——要连哪个库就改 `db_code`。
- **DM 的 schema 由 DM 用户账号决定**，`db_code` 对它只是个标签（用于连接池 key 和工具参数）。
  要换 schema 就换 `db_user`，或在 SQL 里写 `schema名.表名`。
- **PostgreSQL / GaussDB 的 `conn_url` 必须带 `dbname`**，否则连上的是驱动默认库。
  这两种方言的库名**只**认 `conn_url`，`db_code` 对它们同样只是标签。
- **Oracle 的 `conn_url` 三种写法都行**：`主机:端口/服务名`（Easy Connect，最常用）、
  `tnsnames.ora` 里配好的别名、或 `(DESCRIPTION=(ADDRESS=...))` 完整描述符。
- libpq 连接串（PG/GaussDB）也可以用 URI 形式：`postgresql://10.0.0.1:5432/mydb`
  （用户名密码仍单独填，不要写进 URI）。

### 🏷️ db_schema 列：表结构查询按哪个 schema 过滤（v4.4）

配置表多了一个可空列 `db_schema`（**通常留空**）。它只影响
`get_schema_info` / `search_tables` 两个工具的过滤条件，不影响连接、不影响 SQL 执行。

| 方言 | 留空时的默认 | 什么时候需要填 |
|---|---|---|
| `mysql` | `db_code`（就是库名） | 基本不用填 |
| `dm` | 当前登录用户的 `USER_*` 视图 | 要内省**别的** schema 时填该 owner |
| `postgres` / `gaussdb` | `current_schema()`（跟随 search_path） | 目标表不在默认 schema 时填 |
| `oracle` | `USER`（当前登录用户） | 要内省**别的** owner 时填 |

优先级：**SQL 里显式写的 `schema.表名` 前缀 > `db_schema` 列 > 上表的方言默认**。

```sql
-- 临时看一眼别的 schema（不必改配置）
get_schema_info("reporting.daily_summary")
```

> 为什么需要这一列：v4.3 及以前，表结构查询把 `db_code` 当 schema 用。
> 这对 MySQL 成立，但对另外四个方言不成立——`db_code` 只是业务标签，
> 于是 PG 上按 `nspname='report'` 过滤（真实 schema 是 public）、Oracle 上按
> `OWNER='REPORT'` 过滤（真实 owner 是登录用户），**永远返回空且不报错**，
> 表现为"表结构查不出来"。v4.4 起按上表取默认值，老配置库自动补列（见下文迁移）。

### ☑️ write_confirm 列：写操作免人工审核（v4.6，开发库专用）

配置表多了一个列 `write_confirm`（**默认 1 = 需审核**）。它决定该数据源的
`execute` / `import_csv` / `dump_database` 执行前**弹不弹确认窗**：

| 值 | 行为 |
|---|---|
| `1`（默认） | 写操作/导入/导出前弹服务端确认窗，人工点「接受执行」才执行 |
| `0` | 三者全部免确认，同时跳过影响行数试执行预估——**开发库随便造数据用** |

维护入口（改完秒级生效，无需重启）：

- **GUI**（`start_datasource_manager.bat`）：添加/编辑弹窗里的
  **「写操作需人工审核」勾选框**；列表多一列「审核」，免审核的数据源整行橙色
- **CLI**（sqlite 模式）：`python tools/manage_datasources.py add` 时会问
  "写操作需人工审核? (y/n，开发库造数据填 n 免确认)"
- **DB Browser**（sqlite 模式）：直接把该行的 `write_confirm` 改成 `0`

几条边界：

- 与全局开关是**"与"的关系**：`MCP_WRITE_CONFIRM=off` 仍然全量跳过；
  本列只能把**单个**数据源放宽到免确认，不能反向收紧（全局开着时，生产库照旧弹窗）
- 免确认只去掉弹窗，**审计日志照旧记录**每一次写操作，事后可 `query_audit_log` 追溯
- `list_datasources` 每个数据源带 `write_confirm` 字段，模型能看出哪个是开发库
- 缺列期间（老配置库没补列）全部数据源按"需审核"处理，**不会悄悄放宽**

### 🔀 跨库查询语法（`db.table`）在各方言下的实际行为

工具支持 `db_code.table_name` / `database.table_name` 写法，但**能不能真的跨库取决于方言**：

| 方言 | 行为 |
|---|---|
| `mysql` | ✅ 完整支持。识别到限定库名会**切换连接的默认库**再执行 |
| `oracle` / `dm` | ✅ 可用。`db.table` 被解释为**同一实例内的 `schema.table`**，不需要切换连接即可解析 |
| `postgres` / `gaussdb` | ❌ **不支持跨数据库**。PostgreSQL 单连接内无法引用其它数据库（需 postgres_fdw/dblink），查询会报"关系不存在" |

> 对 PG/GaussDB：要查另一个库，就**再注册一条数据源**（新的 `db_code` + 带对应 `dbname` 的 `conn_url`），
> 然后用 `db_code` 参数指定，而不是指望 `db.table` 语法。
>
> 另外 `information_schema`、`pg_catalog` 这类系统 schema 作前缀时**不会**触发切库
> （MySQL）——它们本身已是完全限定名，在任意默认库下都能执行。

3. **命令行小工具**（在项目根目录执行）
   ```
   python tools/manage_datasources.py add        # 交互式添加，一路填写
   python tools/manage_datasources.py list       # 查看已注册数据源
   python tools/manage_datasources.py template   # 插入一条占位示例，再手改
   python tools/manage_datasources.py del mydb   # 删除
   python tools/manage_datasources.py set-pwd mydb   # 改密码
   ```

> 💡 `data/datasources.db` 不存在也没关系——**首次运行 MCP 或 GUI 时会自动创建**空配置库。

### v4.4 迁移：补 db_schema 列

SQLite 模式**零操作**：MCP / GUI / CLI / 迁移脚本打开配置库时都会自动 `ALTER TABLE ADD COLUMN`。

MySQL 模式在 `mcp` 配置库上执行一次（语句在 `docs/mysql_config.sql` 末尾）：

```sql
USE mcp;
ALTER TABLE mcp_datasource_config
  ADD COLUMN db_schema varchar(128) NULL DEFAULT NULL
  COMMENT 'PG/GaussDB的schema或Oracle/达梦的owner；MySQL留空(库名取db_code)'
  AFTER db_password;
```

然后调一次 `refresh_datasources` 工具（或重启会话）即生效。
**不补也能用**：MCP 会探测到缺列并自动降级——按方言默认 schema 过滤，
只在日志里留一条 warning，不会报错。

> ⚠️ 务必在 `mcp` 配置库上执行。MCP 的 `execute` 工具默认路由到当前目录的数据源
> （比如业务库），在那边执行只会给业务库添一张没用的空表。

### v4.6 迁移：补 write_confirm 列

SQLite 模式**零操作**：MCP / GUI / CLI / 迁移脚本打开配置库时都会自动 `ALTER TABLE ADD COLUMN`。

MySQL 模式在 `mcp` 配置库上执行一次（语句在 `docs/mysql_config.sql` 末尾）：

```sql
USE mcp;
ALTER TABLE mcp_datasource_config
  ADD COLUMN write_confirm tinyint(4) NULL DEFAULT 1
  COMMENT '写操作是否需人工审核：0-否（开发库免确认），1-是'
  AFTER db_schema;
```

然后调一次 `refresh_datasources` 工具（或重启会话）即生效。
**不补也能用**：MCP 会探测到缺列并自动降级——全部数据源按"需人工审核"处理
（安全默认，与 v4.6 之前行为一致），只在日志里留一条 warning，不会报错。
补完后到 GUI 里把开发库那条**取消打勾**即可。

### 从 MySQL 配置库迁移到 SQLite

已有 mysql 模式老用户想切到本地文件模式，一条命令（在项目根目录执行）：

```bash
python tools/migrate_datasources.py --dry-run   # 先预览要搬哪些
python tools/migrate_datasources.py             # 执行：自动备份现有文件 → 全量搬运 → 核对
```

- 迁移前若 `data/datasources.db` 已存在会自动备份为 `.bak-时间戳`
- MySQL 配置库保持原样不动，随时可切回 mysql 模式
- 迁移完把 `.env` 的 `MCP_CONFIG_MODE` 改为 `sqlite`，重启 AI 会话即生效

### ⚡ 配置变更生效机制（秒级自动感知）

**不需要调 `refresh_datasources`，也不需要重启**——MCP 每次查询前会轻量探测配置源是否变化：

| 模式 | 探测方式 | 开销 |
|------|---------|------|
| sqlite | stat `data/datasources.db`（含 `-wal`/`-shm`）的 mtime | 微秒级，纯本地 |
| mysql | `SELECT version FROM mcp_config_meta`（GUI 写完自动 +1） | 主键单行查询，结果按 `MCP_CONFIG_PROBE_TTL`（默认 2s）缓存 |

- 外部工具（GUI / DB Browser / Navicat）改完配置 → 最迟 `MCP_CONFIG_PROBE_TTL` 秒后自动生效，仍属秒级
- `refresh_datasources` 工具永远强制全量重读，不受 TTL 限制
- `MCP_CONFIG_PROBE_TTL=0` 可关闭缓存，恢复「每次查询都探测」
- GUI 的「刷新数据源」按钮用于人工核对当前状态与变更指纹（mtime/版本号）
- MySQL 配置库账号无建表权限时自动降级为旧行为（每次全量查询），不影响使用

### 目录匹配规则（MySQL / SQLite 模式均适用）

- `dir_path = '*'`（或空串）：**任意目录兜底数据源**。当前目录没有精确配置时自动启用。
  只有一个项目 → 用这个就够了。
- `dir_path = 'D:\myproject'`：只有在该目录下打开会话才生效（正反斜杠均可）。
  多个项目连不同库时按目录区分。

> 注：`'*'` 兜底自 v4.7 起两种配置模式都支持。此前只有 SQLite 模式实现了回退，
> MySQL 模式（`MCP_CONFIG_MODE=mysql`）只做目录等值匹配，配 `'*'` 行会静默失效。

---

## 通用安装步骤（两种模式都适用）

### 📦 步骤 1：安装 Python 依赖

按需安装——用什么数据库装什么驱动：

```bash
# 必装（SQLite 配置库模式下仅此一项 + 用到啥装啥）
pip install python-dotenv

# 按需：业务数据库驱动
pip install mysql-connector-python    # 查 MySQL
pip install dmPython                  # 查达梦
pip install psycopg2-binary           # 查 PostgreSQL / GaussDB
pip install oracledb                  # 查 Oracle
```

> 所有驱动均为懒加载：**没装的驱动不影响 MCP 启动**，只有真正查询该类型数据库时才需要。
> 驱动缺失时报错信息会带上对应 pip 安装命令。

### 🗄️ 步骤 2：配置数据源

- SQLite 模式：见上节（GUI / DB Browser / 命令行工具任选）
- MySQL 模式：初始化配置库执行 `docs/mysql_config.sql`，**或双击 `start_datasource_manager.bat`，启动时若提示缺配置表可一键初始化**；然后用 GUI / Navicat 维护 `mcp_datasource_config` 表

### 🐍 步骤 3：保存脚本

将整个文件夹放到一个**固定目录**（不要放在会频繁移动或带中文/空格的路径）。

### ⚙️ 步骤 4：配置客户端 `.claude.json` / `claude_desktop_config.json`

**1. 获取 Python 绝对路径**：

```bash
# Mac/Linux
which python3
# Windows CMD
where python
```

**2. 写入客户端配置**：

```json
{
  "mcpServers": {
    "db-auto-router": {
      "type": "stdio",
      "command": "替换为上一步查到的 python 绝对路径",
      "args": ["本文件夹绝对路径/db_mcp_router.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": "本文件夹绝对路径",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

📍 **路径格式注意**：
- Windows 路径在 JSON 中需用双反斜杠 `\\` 或正斜杠 `/`，例如：`"args": ["D:/mcp/db-auto-router/db_mcp_router.py"]`
- Mac/Linux 直接填输出路径即可

### ✅ 步骤 5：验证

重启 AI 客户端后，让它调用 `list_datasources` 工具。能看到你配置的数据源即成功。

### 🛡️ 写操作确认与影响行数预览（v4.4）

`execute` / `import_csv` 在真正碰数据库之前，服务端先弹原生中文确认窗
（「✓ 接受执行 / ✗ 拒绝」，Enter/Esc 快捷键，关窗视为拒绝），tkinter 不可用时
降级为 MCP 终端确认。**这条拦截在服务端，模型无法绕过。**

v4.4 起，弹窗里多一行**影响行数预估**：

```
预计影响 1234 行 · 试执行后已回滚（自增值可能被消耗）
```

实现方式是在专用连接上把语句**试执行一遍、读 rowcount、立即回滚**，不提交。
之所以不把 SQL 改写成 `SELECT COUNT(*)`：改写要解析整句句型（JOIN、子查询、
多表更新、别名），正则漏一种就算错——而**错的行数比没有行数更危险**，
它会给出虚假的信心。试执行对任何句型都精确。

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `MCP_WRITE_CONFIRM` | `on` | 写操作确认总开关；`off` 退回 v2.x 行为（不弹窗、不预估） |
| `MCP_WRITE_CONFIRM_UI` | `auto` | 确认通道：`auto`（弹窗优先，失败降级终端）/ `native` / `elicit` |
| `MCP_WRITE_CONFIRM_PREVIEW_LINES` | `8` | 弹窗里 SQL 最多显示几行，超出折叠（完整语句始终进服务端日志） |
| `MCP_WRITE_ESTIMATE` | `on` | 影响行数预估开关；`off` 后弹窗只显示 SQL |

**只想让开发库不弹窗？** 用数据源级的 `write_confirm` 列（默认 1=需审核，
改成 0 = 该数据源免确认，GUI 里就是取消打勾），生产库照旧拦截——
详见上文《write_confirm 列》。全局开关与数据源开关是"与"的关系。

**预估的边界（要知道的代价）：**

- 只对**纯 DML** 生效。DDL（CREATE/ALTER/DROP/TRUNCATE）回滚不了，估了也是假数字，
  故直接标 `is_ddl` 不给预估。
- **事务内执行不预估**（`execute` 传了 `txn_id`）——在别人事务里跑一次会打断事务语义。
- 试执行的副作用：**自增/序列值会被消耗**（回滚不归还，号段出现空洞）、
  **触发器会真实触发一次**（带外部副作用的触发器会被点燃）。
  介意就设 `MCP_WRITE_ESTIMATE=off`。
- 试执行就报错的语句，预估会返回 `预执行未通过（<错误原文>`——等于提前替你做了一次
  语法/权限校验，不必等点完"接受执行"才看到同一条报错。

### 🧾 审计日志（`query_audit_log`）
每条 SQL 操作都按天落一行 JSON 到 `logs/audit-YYYYMMDD.jsonl`，用于事后追溯"到底执行了什么"。

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `MCP_AUDIT_LOG_DIR` | `logs` | 日志目录；相对路径按本文件夹解析；设 `off` 关闭审计 |
| `MCP_AUDIT_LOG_SCAN_DAYS` | `30` | `query_audit_log` 默认扫描最近几天的文件（按文件名日期判定，不看 mtime）。传 `0` 不限天数 |
| `MCP_AUDIT_LOG_RETAIN_DAYS` | `0` | `>0` 时 MCP 启动会删除更早的日志文件；`0` = 永久保留 |

`query_audit_log` 工具参数：`limit`（返回条数）、`tool`（按工具名过滤，如 `execute`）、
`db_code`（按数据源过滤）、`days`（覆盖默认扫描天数，`0` = 扫全部）。

> 审计日志会记录完整 SQL 文本（截断到 4000 字符），`WHERE` 子句里的业务值也会落盘。
> 分发文件夹前记得按下方清单清空 `logs/`。

### 🔍 执行计划（`explain_query`）

一条 SELECT 慢，先看它打算怎么执行。本工具把五种方言的 EXPLAIN 语法差异吃掉，
并**顺手把计划翻译成人能读的结论**——拿回来的不只是一堆 `type=ALL, key=NULL` 的原始行。

| 方言 | 普通计划 | `analyze=true` |
|---|---|---|
| MySQL | `EXPLAIN [FORMAT=JSON\|TREE]` | `EXPLAIN ANALYZE`（需 8.0.18+，不足自动降级） |
| PostgreSQL / GaussDB | `EXPLAIN (FORMAT TEXT\|JSON)` | `EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)` |
| Oracle | `EXPLAIN PLAN FOR` + `DBMS_XPLAN.DISPLAY` | 无等价物，降级为普通计划并在 `note` 说明 |
| 达梦 | `EXPLAIN` | 未经真机验证，降级并在 `note` 说明 |

**自动诊断**（`warnings` + `verdict`）覆盖：全表扫描、有可用索引但优化器没用上、
`Using filesort`、`Using temporary`、Block Nested Loop Join、行数估算偏差超 10×
（提示统计信息过期该 `ANALYZE` 了）。`verdict` 汇总为 `ok` / `needs_attention` / `poor`。

参数：`sql`（要分析的 SELECT）、`analyze`（默认 **false**）、`fmt`（输出格式）、`db_code`。

**两个要知道的边界：**

- `analyze=true` 会**真实执行一遍该 SELECT**（PG 的 `EXPLAIN ANALYZE` 对 DML 也生效）。
  大表上有实际耗时成本，故默认关闭。只读守卫与 `query` 工具完全同一道，
  `EXPLAIN ANALYZE DELETE ...` 这类会被直接拦下。
- 自动诊断只在表格格式（MySQL TRADITIONAL / PG TEXT）下进行。
  `FORMAT=JSON/TREE` 是单列文本，行级判读无从下手——此时 `warnings` 为空并在
  `note` 里说明跳过，**不是"没问题"**。

### 🐢 慢查询（`slow_queries`）

查慢查询与正在执行的语句。**按方言尽力而为，不承诺一定有历史慢查询**——
各方言前置条件差异很大，拿不到就在 `note` 里说清缺什么，并回落给出正在执行的语句。
诊断工具的可用性不该阻塞主流程，故失败不抛异常。

| 方言 | 数据来源 | 前置条件 |
|---|---|---|
| MySQL | `mysql.slow_log` | `slow_query_log=ON` **且** `log_output=TABLE`；否则回落 `SHOW FULL PROCESSLIST` |
| PostgreSQL / GaussDB | `pg_stat_statements` + `pg_stat_activity` | 前者需 `CREATE EXTENSION pg_stat_statements` |
| Oracle / 达梦 | `v$session` / `v$sql` | 需 DBA 或 `SELECT ANY DICTIONARY` 类权限 |

参数：`limit`（默认 20，上限 200）、`include_running`（是否含在跑语句，默认 true）、`db_code`。

> MySQL 想让本工具读到慢日志：`SET GLOBAL slow_query_log=ON; SET GLOBAL log_output='TABLE';`
> （`log_output` 可同时含 `FILE`，逗号分隔即可）

### 🔁 事务续期（`extend_transaction`）

事务默认空闲 600 秒就回滚放锁（`MCP_TXN_IDLE_TIMEOUT`，全局默认**未改**）。
v4.5 补上两件事，让长流程不必重来：

- `begin_transaction(idle_timeout=)` —— **开事务时**就按需放宽本事务的空闲阈值
  （如 `idle_timeout=3600`），不必把全局默认一起调大（那会让忘记提交的事务更久才放锁）。
  上限受 `MCP_TXN_MAX_IDLE` 约束，默认 24 小时。
- `extend_transaction(txn_id, extend_seconds=)` —— **运行中**续期：重置空闲计时，
  并可用 `extend_seconds` 把阈值提升到至少这么多秒（只升不降）。
- `list_transactions` 每条新增 `idle_remaining_seconds` / `expires_soon`——
  没有可见的剩余时间，就没人知道该续期。

**必须在过期之前调用**：过期后 `txn_id` 已释放、连接已回滚关闭，此时只能重新
`begin_transaction` 从头再来。

```python
begin_transaction(idle_timeout=1800)        # 开事务就放宽到 30 分钟
list_transactions()                          # 看 idle_remaining_seconds 剩多少
extend_transaction("txn-1a2b3c4d", 3600)     # 用户去吃个饭，临走前续到 1 小时
```

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `MCP_TXN_IDLE_TIMEOUT` | `600` | 全局默认空闲超时（秒）。保持不变 |
| `MCP_TXN_MAX_IDLE` | `86400` | 单事务空闲超时的天花板，`begin`/`extend` 传超过它的值会被拒绝 |

### 💻 换电脑迁移

1. 整个文件夹拷到新电脑——**所有路径均相对本文件夹解析，无写死路径**
2. 新电脑装 Python（勾选 Add to PATH）+ `pip install python-dotenv`（按需装数据库驱动）
3. `.claude.json` 里把 `db_mcp_router.py` 的绝对路径改成新位置（客户端配置，每次换机必改）
4. 双击 `start_datasource_manager.bat` 核对数据源（首次无 `data/datasources.db` 会自动建库）

---

## 🔐 给别人分发时的检查清单（给维护者本人）

1. `data/datasources.db` 用 DB Browser 打开，**删掉自己所有的数据源行**（或平时就维护一份干净模板副本）
2. `.env` 确认 `MCP_CONFIG_MODE=sqlite`
3. `.env` 中 mysql 模式专属的 `MCP_CONFIG_PASSWORD=root` 等敏感行可以清掉（sqlite 模式下无用）
4. `logs/` 目录里是自己历史的查询审计日志，建议清空
5. 整个文件夹打包发送

## ⚠️ 注意事项

- `dump_database`（整库导出）依赖本机 `mysqldump` 命令——只装了 MySQL Server 的通常自带；没有的装个 MySQL Client
- `data/datasources.db` 中密码为明文，文件本身不要外传（分发给他人前按上面清单清理）
- MCP 运行中用 DB Browser 同时编辑是安全的（WAL 模式），改完下一次查询自动生效（秒级感知，无需 refresh_datasources）
