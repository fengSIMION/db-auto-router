# 只读通道三层防线设计（read-guard three layers）

日期：2026-11-07 · 状态：待用户批准 · 轮次：安全主线（本轮只做这一条）
修订：v2（并入 builder 审查 8 问的决议，见文末「评审决议」）

## 意图与为什么

`query` / `export_csv` / `explain_query` 三个只读通道当前只有一道防线：
`sqlguard.validate_read_only_sql`（正则 + 状态机）。这道防线写得扎实，但本质是
"可证明只读"的枚举式白名单——`_CONFIRM_EXTRA_WRITE_PREFIXES`（sqlguard.py:188）
被打过七次补丁就是信号：**枚举永远慢于绕过**。

2026-11 生态调研的共识是两层，本设计把它接过来：

1. 用真解析器判语句类型，而不是正则（mssql 用 sqlglot AST、crystaldba 用 pglast）
2. 用数据库本身的只读机制兜底，而不是服务端字符串判定
   （clickhouse `readonly=1` 连接级强制、微软 `access_mode: ro` + 只读角色）

即：**正则快速拒 → sqlglot 解析验 → 数据库只读兜底**，三层全部通过才执行；
任何一层拒绝即拒绝；任何一层不可用时如实降级并在返回值说明，不假装安全。

## 目标

- G1 语句类型判定获得 AST 第二意见（mysql/postgres/oracle 用 sqlglot 原生 dialect；
  dm→oracle、gaussdb→postgres 近似映射）
- G2 只读查询在数据库连接上真实处于只读状态（适用范围见 R3）
- G3 任意一层不可用（方言不支持、数据库不认该语法、sqlglot 未安装）时：
  该层自动关闭、返回值带 note、其余层照常工作
- G4 不新增工具、不改工具签名、不改 execute 任何行为
- G5 每层拒绝时，错误原文说清"哪层拒的、为什么、下一步怎么办"

## 非目标（本轮不做）

- sqlglot `qualify` 列存在性校验（依赖 schema 缓存，下一轮）
- execute 写通道的任何改动（确认门保持现状）
- schema 缓存、get_schema_info 增强、execute timeout、report_progress、
  db_health、联邦查询——各自独立轮次
- 新工具、MCP resources/prompts
- 删改 Layer 1 现有逻辑（只在它前后接线）

## 反目标（做了就是失败）

- A1 任何一层从"拒绝"静默变成"放行"——包括 sqlglot 导入失败、解析异常、
  数据库 SET 失败
- A2 把"本地解析器无法解析"说成"你的 SQL 有语法错误"——那是事实错告
  （与 db-diagnose 红线同款事故）
- A3 为省往返跳过第三层，或把"支持"结论缓存后不再设只读
- A4 只读标记泄漏到读写池连接（put_connection 的 rollback 不清 SESSION 级标记）
- A5 **对 AST 无法判定的语句搞株连拒绝**——第二层只许拒绝"被确证为写"或
  "解析失败"的语句；判不了就必须放行并说明，不得把 explain_query 这类
  合法只读工具打死在四个方言上

## 约束（来自现有架构，实现必须遵守）

- C1 Layer 1 保持在数据源解析**之前**执行——tools.py:100 / tools.py:592 的排序是
  刻意设计的（先报"只读通道不允许写操作"，再报"没有可用的数据源"），不许打乱
- C2 三个只读通道入口：query（tools.py）、explain_query（tools.py）、
  export_csv（csvio.py:30）。Layer 2/3 在数据源解析后、执行前接入
- C3 池连接必须走 get_connection / put_connection，不许绕过
  （pool.py 的条纹锁与容量计数依赖这条）
- C4 put_connection 第一件事是 `conn.rollback()`（pool.py:276）——事务级只读标记
  随归还自动清除，这是第三层选型的基础
- C5 驱动懒加载（drivers.py）；sqlglot 同样懒加载，不许拖慢 MCP 启动
  （workspace.py:16-21 记着 import 10.8s→0.3s 的教训）
- C6 返回值新增字段必须走 `_normalize_result` 的 JSON 安全路径（tools.py:636）
- C7 Layer 2 拒绝抛 PermissionError（与 Layer 1 同类型）；三个工具层的
  `except PermissionError` 显式直通分支必须补上，否则报错会被
  `except Exception` 包成 `ToolFailure("PermissionError: ...")`（tools.py:122/309/606），
  错误形状与 Layer 1 不一致

## 设计

### 第一层：正则快速拒（现有，不动）

`validate_read_only_sql(sql)`。行为、报错文案、审计路径全部不变。

### 第二层：sqlglot 解析验（新增）

新模块 `db_auto_router/sqlparse.py`，对外一个函数：

```
validate_read_statement_ast(sql: str, dialect: str) -> None
# 拒绝抛 PermissionError（承接 C7）；通过/弃权返回 None
```

**三态判定**（sqlglot 30.30.0 实测：`parse_one` 对建模不了的语句不抛错，
而是回落 `exp.Command` 并打 warning——这与真正的 `ParseError` 是两回事）：

| 解析结果 | 动作 | 理由 |
|---|---|---|
| **全树 walk** 出现写形态节点 | **拒绝** | 第二意见命中，与 Layer 1 独立 |
| 根是 `exp.Query`/`exp.Show`/`exp.Describe` 且全树无写节点 | 通过 | — |
| 根是 `exp.Command`（EXPLAIN/SHOW 在 PG/Oracle/DM/GaussDB 上都落这里）、或方言未映射 | **弃权 + note** | 判不了就不判；Layer 1 的前缀白名单本就拦住这些位置上的写语句，第三层再兜 DML。弃权是 A5 的硬要求 |
| 真 `ParseError` | **拒绝**（含逃生门文案） | 病态/边缘 SQL 不执行；用户可走 execute |

写形态节点集（实测这些**不是** DML/DDL 子类，逐个列，不能只靠 isinstance 继承）：
`Insert / Update / Delete / Merge / Copy / Drop / Create / Alter / Grant / Set /
Lock / Transaction / Use / TruncateTable / Analyze / Into`。

**必须全树 walk，不能只查根节点**——实测
`WITH c AS (INSERT INTO t VALUES (1)) SELECT * FROM c` 根节点是 `Select`，
写操作藏在 CTE 里；只查根必漏。这与 Layer 1 的括号折叠是两种不同思路，
互为备份。

行为细则：

- dialect 映射：`mysql→mysql`、`postgres/gaussdb→postgres`、`oracle→oracle`、
  `dm→oracle`（近似）。未映射 dialect → 弃权 + note
- sqlglot 未安装 / 导入失败 → 本层整体关闭：启动 warning 一次，每次返回 note，
  不影响其它层（与 tkinter 缺失时确认降级同一哲学）
- 病态长 SQL 设解析上限（建议 2s），超时按"无法解析"处理（拒绝方向）
- **危险函数集**（与 write set 分开，任何模式都拒）——`find_all(exp.Func)` 按名匹配。
  这是 AST 和正则**都漏的唯一大类**（函数副作用），配置化放 config.py：
  - postgres/gaussdb：`pg_read_file`、`dblink`、`pg_sleep`、`pg_terminate_backend`、
    `lo_import`、`nextval`、`setval`
  - mysql：`LOAD_FILE`、`benchmark`（DoS）、`sys_exec` 及 UDF 外传入口
  - oracle/dm：SELECT 层可直接调用的文件/网络函数（多数需 PL/SQL 门控，
    清单从简）
  - 未知函数名 → 弃权，绝不株连
- 解析成本实测：10 行 4 表 JOIN 约 2.1ms，相对一次查询往返可忽略；
  病态嵌套 SQL 的 DoS 防护用解析超时（建议 5s，拒绝方向），
  暂不引入深度计数器（ sqlfluff 的 max_parse_depth 概念留待真机遇到再说）
- 拒绝文案（逃生门，遵守 A2）："本地解析器（sqlglot <dialect>）无法解析该语句——
  可能是方言边缘语法，不代表你的 SQL 有错。如确认安全，请改用 execute 工具
  （有服务端确认兜底）。"

### 第三层：数据库只读兜底（新增）

新模块 `db_auto_router/readonly.py`：

```
enable_read_only(conn, dialect) -> bool   # True=已生效；False=不支持（调用方记 note）
disable_read_only(conn, dialect) -> bool  # 需恢复的方言；False=恢复失败（调用方弃连接）
```

按方言的实现与恢复策略：

| 方言 | 开启语句 | 级别 | 恢复 |
|---|---|---|---|
| mysql | `START TRANSACTION READ ONLY` | 事务级 | 无需恢复，put_connection 的 rollback 自动清（C4） |
| postgres / gaussdb | `SET default_transaction_read_only=on` | 会话级 | 查询后恢复原值；**恢复必须先于 put_connection**（见 R6）；恢复失败 → 关连接不回池 |
| oracle / dm | `SET TRANSACTION READ ONLY` | 事务级 | 无需恢复，随 commit/rollback 失效 |

能力缓存（R4）：key = pool_key（连接身份），不是 dialect 粒度——
一个老库不支持不连坐其它同方言数据源；重建连接会重新探测。
只有数据库**明确拒绝**才缓存 False；连接级/瞬时错误（`errutil.is_transient_error`）
不缓存，下次重试。

**自动降级即验证**：enable 语句真机执行一次，数据库不认 → 缓存 False + 日志一次 +
后续查询 note 说明"该数据源未启用数据库只读兜底，仍有一二层保护"。用户无需手动测。

### 适用范围（R2/R3，本轮最重要的两条边界）

| 路径 | Layer 2 | Layer 3 | 原因 |
|---|---|---|---|
| `query`（无 txn_id） | ✅ | ✅ | 主干道 |
| `export_csv` | ✅ | ✅ | 同 query |
| `explain_query` | ✅ | **❌ 豁免 + note** | Oracle/DM 的 `EXPLAIN PLAN FOR` 要写 PLAN_TABLE，只读事务里必报错（explain.py:377-386）；为它开特例等于按方言再分一支，不划算 |
| `query(..., txn_id=...)` | ✅ | **❌ 豁免 + note** | 事务内开只读会**隐式提交用户在飞的事务**（MySQL）、或直接报错（Oracle/DM 必须事务首语句）、或把只读泄漏到事务后续写语句（PG 会话级）。用户事务是比只读兜底更高的单元，碰它就是数据事故 |
| `execute` | 不动 | 不动 | G4：写通道行为零变化 |

### 返回值契约（R3/R5）

三个工具的成功返回统一带：

```
read_only_enforced: true | false
```

- `true` = 数据库只读层已生效：enable 语句被该库接受，且作用于本次语句所在事务
- `false` = 该库不支持/未生效（附 note 说明原因），**一二层仍在工作**
- **该字段不承诺"写操作绝无可能"**——MySQL 的 DDL 会隐式提交逃出只读事务，
  第三层的定位是"一二层漏判时的 DML 兜底"，不是绝对边界。绝对边界仍是
  Layer 1 前缀白名单 + 数据库账号权限
- 错误返回不带该字段（ Layer 1/2 拒绝时执行没发生，无从谈生效）
- 四个成功返回形状（readengine.py 的摘要/常规/早返回 + csvio + explain）都要带，
  用同一helper 注入，不手写四遍

### 接线图

```
query / export_csv / explain_query
  ├─ Layer1  validate_read_only_sql            （数据源解析前，现有）
  ├─ 解析数据源（拿 dialect）
  ├─ Layer2  validate_read_statement_ast       （新增；explain/query/export 都过）
  ├─ 借出连接
  ├─ Layer3  enable_read_only                  （仅 query / export_csv）
  ├─ 执行
  └─ Layer3 恢复（先于归还）→ put_connection → 返回值 read_only_enforced / note
```

## 已定的决定（我的判断，可推翻）

- D1 本轮只做 parse，不做 qualify（依赖 schema 缓存，拆轮次）
- D2 第三层覆盖全部五个方言，不挑常用的做
- D3 验证方式＝自动降级：enable 失败即关闭该层并如实说明
- D4 sqlglot 进 requirements.txt（`sqlglot>=30,<31`，懒加载）；缺失时 Layer 2
  关闭 + 启动 warning。方言行为随版本漂移，用测试语料兜（见下）
- D5 拒绝逃生门统一指向 execute（有服务端确认兜底），不新增免检开关
- D6 不新增工具、不改签名——隐形加固，工具面保持 17 个

## 验收标准

- **回归语料（最重要）**：把 tests/test_sqlguard.py section C 的"必须放行"集合
  扩成 5 方言矩阵（每条 × mysql/postgres/gaussdb/oracle/dm），Layer 2 全量通过——
  这是 A5 的守门测试，也是 D4 版本漂移的告警器
- 单测：Layer 2 三态（拒绝/弃权/ParseError）× 方言映射；危险函数集；
  Layer 3 能力缓存（明确拒绝才缓存 False、瞬时错误不缓存、按 pool_key 隔离）；
  PG 恢复先于归还；恢复失败弃连接
- 真机：用户在活库跑一次 query，看 `read_only_enforced` 与 note；跑一条
  `SELECT pg_read_file(...)`/`LOAD_FILE(...)` 确认被 Layer 2 拒；再跑一条 UPDATE
  走 execute，确认确认弹窗与写通道不受影响
- 现有测试全绿 + 三个只读通道现有行为（LIMIT 注入、摘要、硬顶、审计）不变

## 评审决议（builder 审查 8 问，v2 并入）

| # | 问题 | 决议 |
|---|---|---|
| R1 | EXPLAIN/SHOW 在 4/5 方言落 `exp.Command`，白名单化会退化成字符串前缀匹配 | 三态判定：Command → 弃权；只有真 ParseError 与写形态才拒。立 A5 反目标 + 5 方言回归语料 |
| R2 | `txn_id` 读路径未覆盖，Layer 3 会破坏用户在飞的事务 | 明确豁免：Layer 3 不进事务路径（MySQL 隐式提交是数据事故级），note 说明；transactions.py 只碰返回值不改语义 |
| R3 | Oracle/DM 的 `EXPLAIN PLAN FOR` 写 PLAN_TABLE，只读事务里必错；且 MySQL 实测 `@@transaction_read_only` 读不出生效、DDL 可逃逸 | ①explain_query 整体豁免 Layer 3（不为它按方言开分支）；②`read_only_enforced` 契约改写为"enable 被接受且生效"，明确不承诺 DDL 边界，真机验证方案删除（没有可靠的验证查询，不假装有） |
| R4 | 瞬时失败会永久毒化能力缓存；dialect 粒度会连坐 | 缓存 key 降为 pool_key；只有明确拒绝才缓存 False，瞬时错误走 `is_transient_error` 不缓存 |
| R5 | 字段语义与返回形状 | 契约见上；四个成功形状统一注入，错误返回不带 |
| R6 | PG 恢复若在 put_connection 之后执行，只读标记会泄漏给别的调用方 | 恢复严格先于归还；恢复失败 → 关连接 + 释放名额（pool 需补最小弃连接能力） |
| R7 | C7 审计断言有误：Layer 2/3 在引擎内抛错，会被包成 ToolFailure | 三个工具层补 `except PermissionError` 直通分支（类型不变），错误形状与 Layer 1 一致 |
| R8 | sqlglot 未钉版本，升级可静默把能跑的 SQL 变成拒 | `sqlglot>=30,<31` + 5 方言分类语料测试当漂移告警器 |
