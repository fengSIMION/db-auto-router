# db-auto-router

**多数据源 SQL 查询路由 MCP Server**

让 AI 客户端按当前工作空间自动连接正确的数据库。一个文件夹、五种方言、十八个工具——查询只读、写操作必须经人确认、全程可审计。

<div align="center">

![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)
![MCP SDK](https://img.shields.io/badge/MCP%20SDK-2.x-5A3E85)
![Platform](https://img.shields.io/badge/平台-Windows%20%2F%20macOS%20%2F%20Linux-lightgrey)
![Tools](https://img.shields.io/badge/MCP%20%E5%B7%A5%E5%85%B7-18-2F6FDB)
![Dialects](https://img.shields.io/badge/%E6%96%B9%E8%A8%80-5-1E6B3A)
![Version](https://img.shields.io/badge/version-5.0.0-8A6100)

</div>

## 目录

- [功能特性](#功能特性)
- [工作原理](#工作原理)
- [设计原则](#设计原则)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [数据源配置](#数据源配置)
- [工具一览](#工具一览)
- [安全设计](#安全设计)
- [目录结构](#目录结构)
- [常见问题](#常见问题)
- [深入配置](#深入配置)

---

## 功能特性

- **按工作空间自动路由**——在哪个目录开会话，就连哪个库；`dir_path='*'` 一行配置即可兜底所有工作空间
- **五种方言**——MySQL / 达梦(DM) / PostgreSQL / GaussDB(华为高斯) / Oracle，驱动全部懒加载，用哪个装哪个
- **十八个工具**——查询 / 写入 / 事务 / 表结构内省 / CSV 导入导出 / 整库导出 / 执行计划诊断 / 慢查询 / 索引卫生 / 审计追溯
- **写操作服务端拦截**——`execute` / `import_csv` / `dump_database` 执行前弹出原生确认窗并附影响行数预估，**模型无法绕过**
- **只读通道三层防线**——正则、AST、数据库只读状态逐层校验，任一层不可用时如实降级并标注，不假装安全
- **全量审计**——每条 SQL 按天落 JSON 日志，事后可查"到底执行了什么"
- **低门槛起步**——SQLite 配置库无需额外数据库服务；首次运行 BAT 自动检查并可安装 Python 依赖
- **可视化配置**——自带数据源管理 GUI（列宽自适应、同项目同色、测试连接、保存前预检），也提供命令行等价入口

## 工作原理

```
        AI 客户端（Claude Code / Claude Desktop / Cursor / Continue …）
                          │  MCP over stdio
                          ▼
              db_mcp_router.py（启动入口）
                          │
                          ▼
        ┌──────────────── db_auto_router ────────────────┐
        │  workspace.py    解析当前工作空间（roots 优先，cwd 兜底）│
        │  datasources.py  按 dir_path 选数据源（'*' 兜底）        │
        │  连接池 + 方言适配  五种驱动懒加载，跨库语法翻译          │
        │  SQL 守卫        只读三层防线 / 返回体量硬顶             │
        │  审计            logs/audit-YYYYMMDD.jsonl             │
        └──────────────────────────────────────────────────┘
                          │
                          ▼
     MySQL / 达梦 / PostgreSQL / GaussDB / Oracle
```

配置存放在 SQLite 文件或独立 MySQL 配置库中（`.env` 一键切换）。外部工具修改配置后，MCP 通过文件 mtime 或配置版本号在秒级自动感知，无需重启。

## 设计原则

- **失败方向全是拒**——任何校验层出错、超时或不可用时，默认拒绝执行并说明原因，不放行
- **不假装安全**——哪一层降级了、为什么降级，在返回值里如实标注（如 `read_only_enforced` 的语义是"数据库级兜底有没有兜上"，不是"有没有保护"）
- **不默默改参数**——超出硬顶时夹回上限并在 `warning` 中说明，既不静默照做也不静默改小
- **确认不可绕过**——写操作的确认在服务端强制，模型无法跳过；免确认只对显式标记的开发库生效，且审计照旧记录

## 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | **≥ 3.10** | mcp SDK 2.x 的要求；实测 3.12 正常 |
| mcp SDK | **>= 2, < 3** | `pip install mcp`；2.x 起 `FastMCP` 已改名 `MCPServer`，本仓库按 2.x 编写，**请勿降级 1.x** |
| python-dotenv | >= 1.0 | 必装，加载 `.env` |
| sqlglot | **>= 30, < 31** | 必装，提供只读通道第二层 SQL AST 校验 |
| 数据库驱动 | 按需安装 | 查什么装什么，见下表；**未安装的驱动不影响启动** |
| tkinter | 可选 | Python 自带；缺失时写确认自动降级为终端确认，功能不受影响 |
| 操作系统 | Windows / macOS / Linux | 路径均相对本文件夹解析，无写死路径 |
| MCP 客户端 | 任一支持 MCP 的客户端 | Claude Code / Claude Desktop / Cursor / Continue… |

```bash
# 必装（推荐直接使用依赖清单）
pip install -r requirements.txt

# 按需：业务数据库驱动
pip install mysql-connector-python    # 查 MySQL
pip install dmPython                   # 查达梦
pip install psycopg2-binary            # 查 PostgreSQL / GaussDB
pip install oracledb                   # 查 Oracle
```

> 驱动全部懒加载：缺失时报错信息会带上对应的 pip 安装命令。

## 快速开始

### 1. 配置数据源

双击 `start_datasource_manager.bat`（或 `python tools/manage_datasources_gui.py`），在图形界面中添加数据源：选择 `db_type`、填写 `conn_url`、用户名、密码，点击「测试连接」，保存。

- BAT 会先检查 Python ≥ 3.10、MCP SDK、dotenv、sqlglot，以及配置库中已有数据源对应的驱动；发现缺失项时列出清单并询问是否用当前 Python 自动安装，未经确认不会修改环境
- 自动安装仅处理 Python 包，不安装数据库服务、达梦客户端、MySQL Client 或 `mysqldump`
- 首次运行自动创建 `data/datasources.db` 空配置库
- **首次双击后，桌面会自动出现「数据源管理.lnk」**——此后点击它启动更快；`.bat` 保留在文件夹内作为保底。更换机器拷贝文件夹后，首次双击即按新路径自动重建
- `dir_path` 填 `*` = 任意工作空间都能使用该数据源（只有一个项目时用这个即可）
- **「复制」按钮**克隆一条数据源给另一个工作空间使用：工作空间路径自动追加 `_copy` 后缀，修改「地址」即可指向另一个库（源行为 `*` 兜底时后缀加在数据库名称上，并取消默认标记）
- 也可以用 DB Browser (SQLite) 直接编辑，或 `python tools/manage_datasources.py add` 通过命令行添加
- `conn_url` 格式随方言而变，填错将无法连接——**务必阅读下方《conn_url 填写格式》**

### 2. 将文件夹放到固定位置

置于**不频繁移动、不含中文和空格**的目录，并记住其绝对路径。

### 3. 配置客户端

查询 Python 绝对路径：

```bash
# Mac / Linux
which python3
# Windows CMD
where python
```

写入客户端的 MCP 配置（Claude Code 为 `.claude.json`，Claude Desktop 为 `claude_desktop_config.json`）：

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

> Windows 路径在 JSON 中需使用双反斜杠 `\\` 或正斜杠 `/`，例如 `"D:/mcp/db-auto-router/db_mcp_router.py"`。

### 4. 重启客户端并验证

重启 AI 客户端后，让其调用 `list_datasources` 工具——能看到已配置的数据源即表示成功。

## 数据源配置

### 配置存储模式（`.env` 的 `MCP_CONFIG_MODE` 切换）

| 模式 | 配置存放 | 适合谁 | 额外要求 |
|------|---------|--------|---------|
| `sqlite`（默认） | `data/datasources.db` 文件 | **拿到文件夹即可使用** | 无（Python 自带，零安装） |
| `mysql` | 独立 MySQL 服务的 `mcp` 配置库 | 已有全套环境的老用户 | 本地 MySQL 服务 |

修改 `.env` 后重启会话即生效。从 mysql 模式迁移到 sqlite 模式：
`python tools/migrate_datasources.py --dry-run` 先预览，去掉 `--dry-run` 执行（自动备份原文件、全量搬运、逐条核对）。

### conn_url 填写格式（五种方言不同，填错连不上）

`conn_url` 被原样交给对应驱动，各方言要求不同：

| db_type | conn_url 格式 | 示例 | 数据库 / schema 来源 |
|---|---|---|---|
| `mysql` | `主机:端口` | `10.0.0.1:3306` | **`db_code` 字段** |
| `dm` | `主机:端口`（dmPython DSN） | `10.0.0.1:5236` | DM 用户的默认 schema |
| `postgres` | libpq 连接串，**必须带 dbname** | `host=10.0.0.1 port=5432 dbname=mydb` | `conn_url` 中的 `dbname` |
| `gaussdb` | 同 PostgreSQL（兼容 PG 协议） | `host=10.0.0.1 port=15432 dbname=mydb` | `conn_url` 中的 `dbname` |
| `oracle` | Easy Connect / TNS 别名 / 完整描述符 | `10.0.0.1:1521/orcl` | `conn_url` 中的服务名 |

**几个容易踩的点：**

- **MySQL 的库名取自 `db_code`，而非 `conn_url`**。`10.0.0.1:3306/mydb` 中斜杠后的部分会被忽略——要连哪个库就改 `db_code`
- **DM 的 schema 由用户账号决定**，`db_code` 仅作为标签；更换 schema 请改 `db_user`，或在 SQL 中写 `schema名.表名`
- **PostgreSQL / GaussDB 的 `conn_url` 必须带 `dbname`**，否则连接的是驱动默认库；这两种方言的库名**只**认 `conn_url`
- **Oracle 三种写法均支持**：`主机:端口/服务名`（最常用）、`tnsnames.ora` 中配好的别名、或 `(DESCRIPTION=(ADDRESS=...))` 完整描述符
- libpq 连接串也可使用 URI 形式：`postgresql://10.0.0.1:5432/mydb`（用户名与密码仍单独填写，不要写进 URI）

### 工作空间匹配规则

- `dir_path = '*'`（或空串）：**任意工作空间兜底数据源**，当前工作空间没有精确配置时自动启用
- `dir_path = 'D:\myproject'`：仅在该工作空间下打开会话时生效（正反斜杠均可）

多个项目连接不同库时按工作空间区分；只有一个项目时，使用 `'*'` 即可。

### 跨库查询语法（`db.table`）的实际行为

工具支持 `db_code.table_name` 写法，但**能否真正跨库取决于方言**：

| 方言 | 行为 |
|---|---|
| `mysql` | 完整支持。识别到限定库名后会**切换连接的默认库**再执行 |
| `oracle` / `dm` | 可用。解释为同一实例内的 `schema.table`，无需切换连接 |
| `postgres` / `gaussdb` | **不支持跨数据库**（PG 单连接内无法引用其它数据库，需 postgres_fdw / dblink） |

> 对 PG / GaussDB：要查另一个库请**再注册一条数据源**，用 `db_code` 参数指定。
> 另外，`information_schema`、`pg_catalog` 这类系统 schema 作为前缀时不会触发切库（MySQL）。

### 两个可选列（用 DB Browser / GUI 维护）

| 列 | 默认 | 作用 |
|---|---|---|
| `db_schema` | 空 | 表结构查询按哪个 schema 过滤。留空时按方言取默认：mysql→`db_code`，dm→登录用户视图，pg/gaussdb→`current_schema()`，oracle→`USER`。SQL 中显式书写的 `schema.表名` 优先级最高 |
| `write_confirm` | `1` | `0` = 该数据源（开发库）写操作免确认，审计日志照旧记录；`1` = 需人工审核。与全局开关是"与"的关系，只能放宽不能收紧 |

## 工具一览

共 **18 个工具**：

| 工具 | 用途 | 写操作需确认 |
|---|---|---|
| `query` | 只读查询（自动 LIMIT 截断、大结果自动摘要） | — |
| `execute` | 任意 SQL，支持多语句批量（全部成功才提交） | ✅ |
| `list_datasources` | 列出当前工作空间可用的数据源 | — |
| `refresh_datasources` | 强制刷新当前工作空间的数据源配置缓存 | — |
| `search_tables` | 按表名 / 注释模糊搜索表 | — |
| `get_schema_info` | 查表结构：字段、索引、约束 | — |
| `begin_transaction` | 开事务（可设 `idle_timeout` 放宽本事务空闲阈值） | — |
| `commit_transaction` | 提交事务 | — |
| `rollback_transaction` | 回滚事务 | — |
| `list_transactions` | 列活跃事务（含剩余空闲时间、是否即将过期） | — |
| `extend_transaction` | 事务续期：重置空闲计时、提升阈值（只升不降） | — |
| `export_csv` | 查询结果导 CSV（utf-8-sig，Excel 直接打开） | — |
| `import_csv` | CSV 导入表（按表头映射列名，分批插入，失败整批回滚） | ✅ |
| `dump_database` | 整库导出为 .sql（MySQL 走本机 `mysqldump`） | ✅ |
| `query_audit_log` | 查询审计日志（按工具名 / 数据源 / 天数过滤） | — |
| `explain_query` | 执行计划 + 自动诊断（全表扫描 / 索引未用 / filesort / 统计信息过期） | — |
| `slow_queries` | 慢查询与正在执行的语句（覆盖程度随方言而异） | — |
| `db_health` | 索引卫生检查（冗余 / 未用索引，只读诊断，绝不自动删） | — |

事务默认空闲 600 秒自动回滚放锁（`MCP_TXN_IDLE_TIMEOUT` 可调）；后台每 5 秒检查一次，事务被访问时也会校验是否已过期。超时事务由后台清理线程回滚并释放连接；过期的 `txn_id` 无法通过继续执行语句续活，只能重新开启事务。

## 安全设计

### 写操作确认（服务端强制，模型无法绕过）

`execute` / `import_csv` / `dump_database` 在真正接触数据库之前，服务端先弹出原生中文确认窗（「✓ 接受执行 / ✗ 拒绝」，Enter / Esc 快捷键，关闭窗口视为拒绝），tkinter 不可用时降级为 MCP 终端确认。弹窗附**影响行数预估**——在专用连接上试执行一遍、读取 rowcount、立即回滚，不提交（自增值可能被消耗、触发器会真实触发一次；介意可设 `MCP_WRITE_ESTIMATE=off`）。

### 审计日志

每条 SQL 操作按天落一行 JSON 到 `logs/audit-YYYYMMDD.jsonl`：工具名、数据源、完整 SQL（截断至 4000 字符）、状态、耗时、影响行数。`MCP_AUDIT_LOG_DIR=off` 可关闭。

### 只读守卫（三层，失败方向全是拒）

`query` / `export_csv` / `explain_query` 三个只读通道共用三层，全部通过才执行：

1. **正则快速拒**——前缀白名单 + 注释 / 字符串脱敏 + CTE 写检测 + 加锁子句拦截，`EXPLAIN ANALYZE DELETE ...` 这类剥壳后重新过门
2. **AST 解析验**——sqlglot 语法树判语句形态（第二意见，不靠枚举关键字），外加高危函数集（`pg_read_file` / `dblink` / `nextval` / `BENCHMARK` 这类"形态是 SELECT 但函数有副作用"的语句，正则和 AST 都容易漏的一类）
3. **数据库只读兜底**——查询时让数据库自身进入只读状态（MySQL / PG / GaussDB / Oracle / 达梦各有各的开法），服务端字符串判错了数据库也不让改

任何一层不可用时**如实降级并说明，不假装安全**：AST 判不了的语句（如达梦特有语法）放行并标注、库不认只读语法就跳过并在返回值 `note` 标注、sqlglot 未装则整个第二层关闭（必装依赖，启动时 warning 一次）。返回值统一带 `read_only_enforced`——其语义是"数据库级兜底有没有兜上"，**不是"有没有保护"**，前两层仍在工作。

完整契约与三条豁免路径（事务内 / EXPLAIN / 超时直连）见 [docs/configuration.md](docs/configuration.md#只读通道三层防线)。

### 返回体量硬顶（服务端强制）

`query` 的 `max_rows` 不超过 50000 行、`timeout` 不超过 600 秒，`export_csv` 的 `max_rows` 只能将 `MCP_EXPORT_MAX_ROWS` 往下调。这三个参数由模型自行决定传值，不设顶时传个 `max_rows=10000000` 会把上千万行整份塞进上下文、拖垮会话。超顶时**夹回硬顶并在返回值 `warning` 中说明**，不默默照做、也不静默改小。三个值均可用环境变量调整（见 [docs/configuration.md](docs/configuration.md)）。

> ⚠️ `data/datasources.db` 中密码为明文，文件本身不要外传。

## 目录结构

```
db-auto-router/
├── db_mcp_router.py             # MCP 启动入口（.claude.json 引用此文件）
├── .env                         # 全部配置（模式切换 / 连接 / 各项开关与阈值）
├── requirements.txt             # 依赖清单
├── start_datasource_manager.bat # 双击启动数据源管理 GUI
├── db_auto_router/              # MCP 核心包（server / tools / 连接池 / SQL 守卫 / 事务 / 导出导入…）
├── tools/                       # 数据源维护工具（GUI / 命令行 / 模式迁移）
├── docs/                        # configuration.md、mysql_config.sql、sqlite_template.sql
├── data/
│   └── datasources.db           # SQLite 配置库（sqlite 模式的数据源存储，首次运行自动创建）
└── logs/                        # 审计日志（每条 SQL 一行 JSON）
```

## 常见问题

- **`dump_database` 报错找不到命令？** 它依赖本机 `mysqldump`——只装了 MySQL Server 的通常自带，没有请安装 MySQL Client
- **查询报"驱动缺失"？** 按报错中的 pip 命令安装对应驱动即可，装完无需重启
- **PG / GaussDB 上查别的库报"关系不存在"？** PG 单连接不能跨库，再注册一条数据源并用 `db_code` 参数指定
- **改了配置不生效？** 外部工具改完最迟 2 秒后自动生效（`MCP_CONFIG_PROBE_TTL`）；`refresh_datasources` 可强制立即全量重读
- **换电脑？** 整个文件夹拷过去（无写死路径），新电脑装 Python + 依赖，`.claude.json` 中改 `db_mcp_router.py` 的绝对路径，双击 bat 核对数据源

## 深入配置

完整环境变量参考（连接池 / 重试 / 摘要 / 事务 / 导出批量 / 审计 / 确认通道……）、配置变更生效机制、影响行数预估的边界、慢查询工具的前置条件，见 **[docs/configuration.md](docs/configuration.md)**。
