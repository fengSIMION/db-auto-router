# db-auto-router — 多数据源 SQL 查询路由 MCP Server

AI 客户端（Claude Code / Claude Desktop / Cursor / Continue 等）通过 MCP 协议调用本地脚本，
**根据当前工作目录自动加载数据库配置**，实现多项目、多数据库的安全查询路由。

支持五种方言：**MySQL / 达梦(DM) / PostgreSQL / GaussDB(华为高斯) / Oracle**。

## ✨ 功能特性

- **按目录自动路由**：在哪个目录开会话，就连哪个库；`dir_path='*'` 一行配置兜底所有项目
- **17 个工具**：查询 / 写入 / 事务 / 表结构内省 / CSV 导入导出 / 整库导出 / 执行计划诊断 / 慢查询 / 审计追溯
- **写操作服务端拦截**：`execute` / `import_csv` / `dump_database` 执行前弹原生确认窗，附影响行数预估，**模型无法绕过**
- **全量审计**：每条 SQL 按天落 JSON 日志，事后可查"到底执行了什么"
- **只读守卫**：`query` 工具在服务端校验语句，非 SELECT/SHOW/DESC/EXPLAIN 直接拒绝
- **零依赖起步**：SQLite 配置库模式，拿到文件夹 `pip install python-dotenv` 就能跑；数据库驱动全部懒加载，用哪个装哪个

## 📋 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | **≥ 3.10** | mcp SDK 2.x 的要求；实测 3.12 正常 |
| mcp SDK | **>= 2, < 3** | `pip install mcp`；2.x 起 `FastMCP` 已改名 `MCPServer`，本仓库按 2.x 编写，**请勿降级 1.x** |
| python-dotenv | >= 1.0 | 必装，加载 `.env` |
| 数据库驱动 | 按需安装 | 查什么装什么，见下表；**没装的驱动不影响启动** |
| tkinter | 可选 | Python 自带；缺失时写确认自动降级为终端确认，功能不受影响 |
| 操作系统 | Windows / macOS / Linux | 路径均相对本文件夹解析，无写死路径 |
| MCP 客户端 | 任一支持 MCP 的客户端 | Claude Code / Claude Desktop / Cursor / Continue… |

```bash
# 必装
pip install "mcp>=2,<3" python-dotenv

# 按需：业务数据库驱动
pip install mysql-connector-python    # 查 MySQL
pip install dmPython                   # 查达梦
pip install psycopg2-binary            # 查 PostgreSQL / GaussDB
pip install oracledb                   # 查 Oracle
```

> 驱动全部懒加载：缺失时报错信息会带上对应的 pip 安装命令。

## 🚀 快速开始

### 1. 配置数据源

双击 `start_datasource_manager.bat`（或 `python tools/manage_datasources_gui.py`），
图形界面里添加数据源：选 `db_type`、填 `conn_url`、用户名、密码，点测试连接，保存。

- 首次运行自动创建 `data/datasources.db` 空配置库
- **第一次双击后，桌面会自动出现「数据源管理.lnk」**——以后点它启动更快（无终端黑窗）；
  `.bat` 留在文件夹里当保底。换机器拷贝文件夹后，第一次双击即按新路径自动重建
- `dir_path` 填 `*` = 任意目录都能用这条数据源（只有一个项目的话用这个就够了）
- 也可以用 DB Browser (SQLite) 直接编辑，或 `python tools/manage_datasources.py add` 命令行添加
- `conn_url` 格式随方言而变，填错连不上——**务必看下面《conn_url 填写格式》**

### 2. 把文件夹放到固定位置

放到一个**不会频繁移动、不带中文和空格**的目录，记住绝对路径。

### 3. 配置客户端

查询 Python 绝对路径：

```bash
# Mac/Linux
which python3
# Windows CMD
where python
```

写入客户端的 MCP 配置（Claude Code 是 `.claude.json`，Claude Desktop 是 `claude_desktop_config.json`）：

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

📍 Windows 路径在 JSON 里要用双反斜杠 `\\` 或正斜杠 `/`，例如 `"D:/mcp/db-auto-router/db_mcp_router.py"`。

### 4. 重启客户端，验证

重启 AI 客户端后，让它调用 `list_datasources` 工具——能看到你配置的数据源即成功。

## 🗄️ 数据源配置

### 配置存哪？（`.env` 的 `MCP_CONFIG_MODE` 切换）

| 模式 | 配置存放 | 适合谁 | 额外要求 |
|------|---------|--------|---------|
| `sqlite`（默认） | `data/datasources.db` 文件 | **拿到文件夹直接用的人** | 无（Python 自带，零安装） |
| `mysql` | 独立 MySQL 服务的 `mcp` 配置库 | 已有全套环境的老用户 | 本地 MySQL 服务 |

改完 `.env` 重启会话即生效。老用户从 mysql 模式切到 sqlite：
`python tools/migrate_datasources.py --dry-run` 先预览，去掉 `--dry-run` 执行（自动备份原文件、全量搬运、核对）。

### 📐 conn_url 填写格式（五种方言不同，填错连不上）

`conn_url` 被原样交给对应驱动，各方言要的东西不一样：

| db_type | conn_url 格式 | 示例 | 数据库/schema 从哪来 |
|---|---|---|---|
| `mysql` | `主机:端口` | `10.0.0.1:3306` | **`db_code`** 字段 |
| `dm` | `主机:端口`（dmPython DSN） | `10.0.0.1:5236` | DM 用户的默认 schema |
| `postgres` | libpq 连接串，**必须带 dbname** | `host=10.0.0.1 port=5432 dbname=mydb` | `conn_url` 里的 `dbname` |
| `gaussdb` | 同 PostgreSQL（兼容 PG 协议） | `host=10.0.0.1 port=15432 dbname=mydb` | `conn_url` 里的 `dbname` |
| `oracle` | Easy Connect / TNS 别名 / 完整描述符 | `10.0.0.1:1521/orcl` | `conn_url` 里的服务名 |

**几个容易踩的点：**

- **MySQL 的库名取自 `db_code`，不是 `conn_url`**。`10.0.0.1:3306/mydb` 里斜杠后面那段会被忽略——要连哪个库就改 `db_code`
- **DM 的 schema 由用户账号决定**，`db_code` 只是个标签；要换 schema 就换 `db_user`，或在 SQL 里写 `schema名.表名`
- **PostgreSQL / GaussDB 的 `conn_url` 必须带 `dbname`**，否则连上的是驱动默认库；这两种方言的库名**只**认 `conn_url`
- **Oracle 三种写法都行**：`主机:端口/服务名`（最常用）、`tnsnames.ora` 里配好的别名、或 `(DESCRIPTION=(ADDRESS=...))` 完整描述符
- libpq 连接串也可以用 URI 形式：`postgresql://10.0.0.1:5432/mydb`（用户名密码仍单独填，不要写进 URI）

### 🗂️ 目录匹配规则

- `dir_path = '*'`（或空串）：**任意目录兜底数据源**，当前目录没有精确配置时自动启用
- `dir_path = 'D:\myproject'`：只有在该目录下打开会话才生效（正反斜杠均可）

多个项目连不同库时按目录区分；只有一个项目 → 用 `'*'` 就够了。

### 🔀 跨库查询语法（`db.table`）的实际行为

工具支持 `db_code.table_name` 写法，但**能不能真的跨库取决于方言**：

| 方言 | 行为 |
|---|---|
| `mysql` | ✅ 完整支持。识别到限定库名会**切换连接的默认库**再执行 |
| `oracle` / `dm` | ✅ 可用。解释为同一实例内的 `schema.table`，不需要切换连接 |
| `postgres` / `gaussdb` | ❌ **不支持跨数据库**（PG 单连接内无法引用其它数据库，需 postgres_fdw/dblink） |

> 对 PG/GaussDB：要查另一个库就**再注册一条数据源**，用 `db_code` 参数指定。
> 另外 `information_schema`、`pg_catalog` 这类系统 schema 作前缀时不会触发切库（MySQL）。

### 两个可选列（用 DB Browser / GUI 维护）

| 列 | 默认 | 作用 |
|---|---|---|
| `db_schema` | 空 | 表结构查询按哪个 schema 过滤。留空时按方言取默认：mysql→`db_code`，dm→登录用户视图，pg/gaussdb→`current_schema()`，oracle→`USER`。SQL 里显式写的 `schema.表名` 优先级最高 |
| `write_confirm` | `1` | `0` = 该数据源（开发库）写操作免确认，审计日志照旧记录；`1` = 需人工审核。与全局开关是"与"的关系，只能放宽不能收紧 |

## 🧰 工具一览

| 工具 | 干什么 | 写操作需确认 |
|---|---|---|
| `query` | 只读查询（自动 LIMIT 截断、大结果自动摘要） | — |
| `execute` | 任意 SQL，支持多语句批量（全部成功才提交） | ✅ |
| `list_datasources` | 列出当前目录可用的数据源 | — |
| `refresh_datasources` | 强制全量重读数据源配置 | — |
| `search_tables` | 按表名/注释模糊搜索表 | — |
| `get_schema_info` | 查表结构：字段、索引、约束 | — |
| `begin_transaction` | 开事务（可设 `idle_timeout` 放宽本事务空闲阈值） | — |
| `commit_transaction` | 提交事务 | — |
| `rollback_transaction` | 回滚事务 | — |
| `list_transactions` | 列活跃事务（含剩余空闲时间、是否即将过期） | — |
| `extend_transaction` | 事务续期：重置空闲计时、提升阈值（只升不降） | — |
| `export_csv` | 查询结果导 CSV（utf-8-sig，Excel 直接打开） | — |
| `import_csv` | CSV 导入表（按表头映射列名，分批插入，失败整批回滚） | ✅ |
| `dump_database` | 整库导出为 .sql（MySQL 走本机 `mysqldump`） | ✅ |
| `query_audit_log` | 查询审计日志（按工具名/数据源/天数过滤） | — |
| `explain_query` | 执行计划 + 自动诊断（全表扫描/索引未用/filesort/统计信息过期） | — |
| `slow_queries` | 慢查询与正在执行的语句（按方言尽力而为） | — |

事务默认空闲 600 秒自动回滚放锁（`MCP_TXN_IDLE_TIMEOUT` 可调）；必须在过期前
`commit` / `rollback` / `extend_transaction`，过期后只能重开事务。

## 🔐 安全设计

**写操作确认（服务端强制，模型无法绕过）**

`execute` / `import_csv` / `dump_database` 真正碰数据库之前，服务端先弹原生中文确认窗
（「✓ 接受执行 / ✗ 拒绝」，Enter/Esc 快捷键，关窗视为拒绝），tkinter 不可用时降级为
MCP 终端确认。弹窗里附**影响行数预估**——在专用连接上试执行一遍、读 rowcount、立即回滚，
不提交（自增值可能被消耗、触发器会真实触发一次；介意可设 `MCP_WRITE_ESTIMATE=off`）。

**审计日志**

每条 SQL 操作按天落一行 JSON 到 `logs/audit-YYYYMMDD.jsonl`：工具名、数据源、完整 SQL
（截断到 4000 字符）、状态、耗时、影响行数。`MCP_AUDIT_LOG_DIR=off` 可关闭。

**只读守卫**

`query` 工具在服务端校验语句，只放行 SELECT/SHOW/DESC/EXPLAIN/WITH；
`explain_query` 走同一道守卫，`EXPLAIN ANALYZE DELETE ...` 这类会被直接拦下。

> ⚠️ `data/datasources.db` 里密码是明文，文件本身不要外传。

## 📁 目录结构

```
db-auto-router/
├── db_mcp_router.py            # MCP 启动入口（.claude.json 引用此文件）
├── .env                        # 全部配置（模式切换 / 连接 / 各项开关与阈值）
├── requirements.txt            # 依赖清单
├── start_datasource_manager.bat # 双击启动数据源管理 GUI
├── db_auto_router/             # MCP 核心包（server/tools/连接池/SQL守卫/事务/导出导入…）
├── tools/                      # 数据源维护工具（GUI / 命令行 / 模式迁移）
├── docs/                       # sqlite_template.sql、mysql_config.sql、configuration.md
├── data/
│   └── datasources.db          # SQLite 配置库（sqlite 模式的数据源存储，首次运行自动创建）
└── logs/                       # 审计日志（每条 SQL 一行 JSON）
```

## ❓ 常见问题

- **`dump_database` 报错找不到命令？** 它依赖本机 `mysqldump`——只装了 MySQL Server 的通常自带，没有的装个 MySQL Client
- **查询报"驱动缺失"？** 按报错里的 pip 命令装对应驱动即可，装完不用重启
- **PG/GaussDB 上查别的库报"关系不存在"？** PG 单连接不能跨库，再注册一条数据源用 `db_code` 参数指定
- **改了配置不生效？** 外部工具改完最迟 2 秒后自动生效（`MCP_CONFIG_PROBE_TTL`）；`refresh_datasources` 可强制立即全量重读
- **换电脑？** 整个文件夹拷过去（无写死路径），新电脑装 Python + 依赖，`.claude.json` 里改 `db_mcp_router.py` 的绝对路径，双击 bat 核对数据源

## 📖 深入配置

完整环境变量参考（连接池/重试/摘要/事务/导出批量/审计/确认通道……）、配置变更生效机制、
影响行数预估的边界、慢查询工具的前置条件，见
**[docs/configuration.md](docs/configuration.md)**。
