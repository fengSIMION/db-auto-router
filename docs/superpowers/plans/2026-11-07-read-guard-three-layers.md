# 只读通道三层防线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把只读通道从"一道正则守卫"升级为"正则快速拒 → sqlglot 解析验 → 数据库只读兜底"三层，每层失败方向都是拒，不可用时如实降级并标注。

**Architecture:** 新增两个纯模块（`sqlparse.py` 做 AST 三态判定，`readonly.py` 做数据库只读开关+能力缓存+返回值契约），`pool.py` 补"弃连接"与公开 key 计算两个小能力；三个只读通道（query / export_csv / explain_query）在数据源解析后接入，事务路径与 explain_query 按设计豁免第三层。不新增工具、不改签名、execute 零变化。

**Tech Stack:** Python 3.10+ / mcp SDK 2.x / sqlglot>=30,<31（懒加载）/ pytest

**Spec:** `docs/superpowers/specs/2026-11-07-read-guard-three-layers-design.md`（计划从设计文档论证，执行前两份都读）

## Global Constraints

以下每条对整个计划所有任务生效，逐条抄自设计文档：

- Layer 1（`sqlguard.validate_read_only_sql`）行为、报错文案、审计路径**零变化**
- 三层失败方向全部是"拒"，任何一层不许静默放行
- **A2**：不许把"本地解析器无法解析"说成"你的 SQL 有语法错误"——拒绝文案必须给逃生门（改走 execute）
- **A5**：AST 判不了的语句必须放行并 note 说明，不许株连（否则 explain_query 死在 4/5 方言、达梦合法 SQL 被误杀）
- 不新增工具、不改工具签名、`execute` 写通道行为零变化
- 第二层拒绝一律抛 `PermissionError`（与 Layer 1 同类型），三个工具层补直通分支，不让它被包成 `ToolFailure("PermissionError: ...")`
- 返回值字段名固定为 `read_only_enforced`；**错误返回不带该字段**；false 时必须附 note 说明哪层没兜上
- 第三层覆盖全部五个方言；库不认只读语法时自动降级（缓存 negative，不缓存 positive）
- `query(..., txn_id=...)` 与 `explain_query` **豁免第三层**（前者碰了会隐式提交用户在飞的事务；后者 Oracle/达梦的 EXPLAIN 要写 PLAN_TABLE）
- sqlglot 钉 `>=30,<31`，懒加载；缺失时第二层整体关闭 + 启动 warning + 每次返回 note
- 病态 SQL 用**确定性**预检拒绝（长度/嵌套深度），不引入线程超时

## Review Focus

| # | 输入/条件 | 合理期望 | 钉它的测试落在 |
|---|---|---|---|
| 1 | 达梦/GaussDB 特有只读语法（映射到 oracle/postgres 方言后）解析失败 | 宁弃权不株连，绝不一刀切拒 | Task 1 `test_abstains_on_command` + `test_five_dialect_corpus` |
| 2 | PG 只读恢复失败，连接带只读标记回池 | 恢复失败即弃连接，后续 execute 不受影响 | Task 2 `test_release_failure_discards_connection` |
| 3 | 事务内 `query(txn_id=...)` | 不碰数据库只读（会隐式提交用户未提交工作），且如实标注 | Task 4 `test_txn_read_marks_exempt` |
| 4 | 模型传入超大/深嵌套 SQL | 确定性拒绝，不把工作线程挂住 | Task 1 `test_oversized_sql_rejected` / `test_deep_nesting_rejected` |
| 5 | sqlglot 缺失或升级后行为翻转 | 缺失→明确降级+note；版本钉死+5 方言语料当漂移告警器 | Task 1 `test_missing_sqlglot_degrades` + `test_five_dialect_corpus` |

---

## 文件结构

| 文件 | 责任 | 任务 |
|---|---|---|
| `db_auto_router/sqlparse.py` | 新建。第二层：方言映射、AST 三态判定、危险函数集、病态预检 | T1 |
| `db_auto_router/readonly.py` | 新建。第三层：按方言开关只读、能力缓存、返回值契约 `read_only_enforced` | T2 |
| `db_auto_router/pool.py` | 改。公开 `pool_key_for`、新增 `discard_connection` | T2 |
| `db_auto_router/readengine.py` | 改。query 读路径接入第三层 + 注入字段 | T4 |
| `db_auto_router/csvio.py` | 改。export_csv 接入第二层 + 第三层 + 注入字段 | T3、T5 |
| `db_auto_router/tools.py` | 改。query/explain_query 接入第二层、三处 PermissionError 直通、事务路径豁免标注 | T3、T4 |
| `db_auto_router/config.py` | 改。两个开关 `AST_GUARD` / `READONLY_DB_GUARD` | T1、T2 |
| `tests/dbfakes.py` | 新建。假连接/假游标（两个新测试文件共用） | T2 |
| `tests/test_sqlparse.py` | 新建。第二层单测 + 5 方言回归语料 | T1 |
| `tests/test_readonly.py` | 新建。第三层单测（假连接） | T2 |
| `tests/test_guard_wiring.py` | 新建。接线测试（monkeypatch 数据源与执行引擎） | T3–T5 |
| `requirements.txt` / `.env` / `docs/configuration.md` / `README.md` | 依赖、开关、文档 | T1、T2、T6 |
| `~/.claude/skills/db-ops/rules/tools-reference.md` | 仓库外：技能层记录新返回字段 | T6 |

---

### Task 1: 第二层 AST 解析校验模块

**Files:**
- Create: `db_auto_router/sqlparse.py`
- Create: `tests/test_sqlparse.py`
- Modify: `db_auto_router/config.py`（在 READ_ONLY_SQL_PREFIXES 附近加 `AST_GUARD`）
- Modify: `requirements.txt`（必装区加 `sqlglot>=30,<31`）
- Modify: `.env`（加 `MCP_READ_GUARD_AST=on`）
- Modify: `docs/configuration.md`（环境变量全表「连接与查询」节加一行）

**Interfaces:**
- Consumes: `config.AST_GUARD`（字符串，`"on"`/`"off"`，默认 `"on"`）
- Produces:
  - `DIALECT_MAP: dict[str, str]` — `{"mysql":"mysql","postgres":"postgres","gaussdb":"postgres","oracle":"oracle","dm":"oracle"}`
  - `validate_read_statement_ast(sql: str, dialect: str) -> None` — 拒绝抛 `PermissionError`；通过或弃权返回 `None`
  - `DANGEROUS_FUNCTIONS: dict[str, frozenset[str]]` — key 为**映射后**的 sqlglot 方言名，value 为小写函数名

- [ ] **Step 1: 写失败测试 `tests/test_sqlparse.py`**

测试风格跟随 `tests/test_limits_cap.py`（纯 pytest 函数 + assert）。每个测试的断言要点：

```python
import pytest
from db_auto_router import sqlparse
from db_auto_router.sqlparse import validate_read_statement_ast as v

# 1. 方言映射
def test_dialect_map_covers_five_dialects():
    assert sqlparse.DIALECT_MAP == {"mysql": "mysql", "postgres": "postgres",
                                    "gaussdb": "postgres", "oracle": "oracle", "dm": "oracle"}

# 2. 写语句必须拒（含"不是 DML/DDL 子类"的那批）
@pytest.mark.parametrize("sql", [
    "DELETE FROM t", "UPDATE t SET a=1", "INSERT INTO t VALUES (1)",
    "DROP TABLE t", "CREATE TABLE x(a int)", "TRUNCATE TABLE t",
    "SET @x=1", "USE otherdb", "LOCK TABLES t WRITE", "ANALYZE TABLE t",
    "CALL my_proc()", "GRANT ALL ON *.* TO u",
])
def test_rejects_write_statements(sql):
    with pytest.raises(PermissionError):
        v(sql, "mysql")

# 3. 树里藏写必须拒（根节点是 Select，只查根必漏）
def test_rejects_write_hidden_in_cte():
    with pytest.raises(PermissionError):
        v("WITH c AS (INSERT INTO t VALUES (1)) SELECT * FROM c", "mysql")

# 4. 合法只读必须放行（不抛异常）
@pytest.mark.parametrize("sql", [
    "SELECT 1", "SELECT * FROM t WHERE a='LIMIT 5'", "SELECT COUNT(*) FROM t",
    "WITH x AS (SELECT 1) SELECT * FROM x", "SHOW TABLES", "DESC t",
    "EXPLAIN SELECT * FROM t", "EXPLAIN ANALYZE SELECT * FROM t",
])
def test_passes_read_statements(sql):
    v(sql, "mysql")  # 不抛即通过

# 5. Command 弃权：EXPLAIN/SHOW 在 PG/Oracle/DM 落 exp.Command，判不了就不判
@pytest.mark.parametrize("dialect,sql", [
    ("postgres", "EXPLAIN SELECT * FROM t"),
    ("postgres", "EXPLAIN ANALYZE SELECT * FROM t"),
    ("oracle", "EXPLAIN SELECT * FROM t"),
    ("dm", "SELECT * FROM t WHERE ROWNUM <= 10"),
    ("gaussdb", "SHOW TABLES"),
])
def test_abstains_when_unclassifiable(dialect, sql):
    v(sql, dialect)  # 不抛即弃权放行

# 6. 真 ParseError → 拒，且文案给逃生门（A2）
def test_rejects_parse_error_with_escape_hatch():
    with pytest.raises(PermissionError) as ei:
        v("SELECT FROM WHERE GROUP BY", "mysql")
    assert "execute" in str(ei.value)      # 逃生门
    assert "语法错误" not in str(ei.value)  # A2：不许说用户的 SQL 有语法错

# 7. 未映射方言 → 弃权
def test_abstains_on_unmapped_dialect():
    v("SELECT 1", "sqlserver")

# 8. 危险函数集
@pytest.mark.parametrize("dialect,sql", [
    ("postgres", "SELECT pg_read_file('/etc/passwd')"),
    ("postgres", "SELECT * FROM dblink('host', 'select 1')"),
    ("postgres", "SELECT pg_sleep(10)"),
    ("postgres", "SELECT nextval('s')"),
    ("mysql", "SELECT LOAD_FILE('/etc/passwd')"),
    ("mysql", "SELECT BENCHMARK(1000000, MD5('x'))"),
])
def test_rejects_dangerous_functions(dialect, sql):
    with pytest.raises(PermissionError):
        v(sql, dialect)

def test_unknown_function_abstains():
    v("SELECT my_custom_func(x) FROM t", "mysql")  # 不株连

# 9. 病态 SQL 确定性拒绝
def test_oversized_sql_rejected():
    with pytest.raises(PermissionError):
        v("SELECT " + ",".join(f"c{i}" for i in range(20000)) + " FROM t", "mysql")

def test_deep_nesting_rejected():
    with pytest.raises(PermissionError):
        v("SELECT " + "(" * 200 + "1" + ")" * 200, "mysql")

# 10. 降级路径
def test_missing_sqlglot_degrades(monkeypatch):
    monkeypatch.setattr(sqlparse, "_SQLGLOT", None)      # 模拟导入失败后的状态
    monkeypatch.setattr(sqlparse, "_sqlglot_probe_failed", True)
    v("DELETE FROM t", "mysql")  # 第二层关闭 → 不抛，交给第一层

def test_guard_switch_off(monkeypatch):
    monkeypatch.setattr(sqlparse, "AST_GUARD", "off")
    v("DELETE FROM t", "mysql")

# 11. 5 方言回归语料（A5 守门测试 + D4 漂移告警器）
#     语料 = tests/test_sqlguard.py section C 的 15 条"必须放行"
CORPUS = [ ...  # 从 test_sqlguard.py:34-51 原文抄入 15 条
]
@pytest.mark.parametrize("dialect", ["mysql", "postgres", "gaussdb", "oracle", "dm"])
@pytest.mark.parametrize("sql", CORPUS)
def test_five_dialect_corpus_never_false_rejects(dialect, sql):
    v(sql, dialect)  # Layer 1 放行的合法只读，Layer 2 一律不许拒
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_sqlparse.py -q`
Expected: FAIL（`ModuleNotFoundError: db_auto_router.sqlparse`）

- [ ] **Step 3: 实现 `db_auto_router/sqlparse.py`**

签名与行为由 Step 1 的测试钉死，实现要点（测试未覆盖的部分在此定）：

- 模块顶部：`from .config import AST_GUARD`；`import` 时**不** import sqlglot——函数内首次调用时 `try: import sqlglot except ImportError: _SQLGLOT=None; _sqlglot_probe_failed=True`，并 `logger.warning` 一次
- `_WRITE_NODES`：`(exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Copy, exp.Drop, exp.Create, exp.Alter, exp.Grant, exp.Set, exp.Lock, exp.Transaction, exp.Use, exp.TruncateTable, exp.Analyze, exp.Into)` —— 逐个列，不靠 isinstance 继承（实测这些不全是 DML/DDL 子类）
- `_READ_ROOTS = (exp.Query, exp.Show, exp.Describe)`
- 流程：开关 off → return；方言未映射 → return；sqlglot 不可用 → return；长度 > 100_000 字符或括号深度 > 100 → 抛 PermissionError（文案："语句过大/嵌套过深，疑似病态 SQL，已拒绝"）；`parse_one(sql, read=mapped)` 抛 `ParseError` → 抛 PermissionError（逃生门文案）；根不在 `_READ_ROOTS` 且不是 `exp.Command` → 抛；`walk()` 命中 `_WRITE_NODES` → 抛；`find_all(exp.Func)` 命中 `DANGEROUS_FUNCTIONS[mapped]` → 抛；其余（含根是 `exp.Command`）→ return
- `DANGEROUS_FUNCTIONS`：`postgres` = `{pg_read_file, pg_read_server_files, dblink, pg_sleep, pg_terminate_backend, lo_import, lo_export, nextval, setval}`；`mysql` = `{load_file, benchmark, sys_exec}`；`oracle` = `{}`（Oracle/DM 的文件网络函数多需 PL/SQL 门控，SELECT 层够不到，留空并在注释说明）

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_sqlparse.py -q`
Expected: PASS（全绿，含 5×15 语料矩阵）

- [ ] **Step 5: 补依赖、开关与文档**

`requirements.txt` 必装区（`mcp>=2,<3` 之后）加：

```
# ---- 只读通道第二层：SQL AST 解析（懒加载；缺失时该层自动关闭并在返回值 note 说明）----
sqlglot>=30,<31
```

`.env` 加一行（放在 `MCP_READ_TIMEOUT` 附近）：`MCP_READ_GUARD_AST=on`。
`docs/configuration.md` 「连接与查询」表加一行：`MCP_READ_GUARD_AST` | `on` | 只读通道第二层（sqlglot AST 校验）总开关；`off` = 只剩正则与数据库两层。

- [ ] **Step 6: 提交**

```bash
git add db_auto_router/sqlparse.py db_auto_router/config.py tests/test_sqlparse.py requirements.txt .env docs/configuration.md
git commit -m "feat: 只读通道第二层——sqlglot AST 三态判定 + 危险函数集 + 五方言回归语料

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: 第三层数据库只读模块 + 连接池弃连能力

**Files:**
- Create: `db_auto_router/readonly.py`
- Create: `tests/dbfakes.py`
- Create: `tests/test_readonly.py`
- Modify: `db_auto_router/pool.py`（末尾加两个公开函数）
- Modify: `db_auto_router/config.py`（加 `READONLY_DB_GUARD`）

**Interfaces:**
- Consumes: `pool._make_pool_key`（经新公开包装）、`errutil.is_transient_error`、`errutil.safe_close`
- Produces:
  - `pool.pool_key_for(ds: Dict[str, str], target_db: str = None) -> str`
  - `pool.discard_connection(ds: Dict[str, str], conn, target_db: str = None) -> None`
  - `readonly.enable_read_only(conn, dialect: str, pool_key: str) -> bool`
  - `readonly.release_read_only(conn, dialect: str, pool_key: str, ds: Dict[str, str], target_db: str = None) -> bool` — 返回 `True`=连接可正常回池；`False`=连接已弃，调用方 **MUST NOT** 再 `put_connection`
  - `readonly.readonly_field(dialect: str, enforced: bool, exempt_reason: Optional[str] = None) -> Dict[str, Any]`

- [ ] **Step 1: 写失败测试 `tests/dbfakes.py` + `tests/test_readonly.py`**

`tests/dbfakes.py`（两个测试文件共用，放在这里一次）：

```python
class FakeCursor:
    """记录执行过的 SQL；fail_on 里命中的语句抛异常（模拟库不认/连接断）。"""
    def __init__(self, conn): self.conn = conn; self.executed = []
    def execute(self, sql, params=None):
        self.executed.append(sql)
        for pat, exc in self.conn.fail_on.items():
            if pat in sql: raise exc
        self.conn.last_sql = sql
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): self.close()

class FakeConn:
    def __init__(self, fail_on=None): self.fail_on = fail_on or {}; self.executed = []; self.closed = False
    def cursor(self): return FakeCursor(self)
    def rollback(self): self.executed.append("--rollback--")
    def close(self): self.closed = True
```

`tests/test_readonly.py` 断言要点：

```python
from db_auto_router import readonly
from db_auto_router.readonly import enable_read_only, release_read_only, readonly_field
from tests.dbfakes import FakeConn

def _key(): return "mysql:10.0.0.1:3306:db:user:abc123"   # 形状同 pool._make_pool_key

# 1. 各方言发出的开关语句
def test_enable_mysql_issues_start_transaction_read_only():
    c = FakeConn(); assert enable_read_only(c, "mysql", _key()) is True
    assert any("START TRANSACTION READ ONLY" in s for s in c.executed)

def test_enable_pg_issues_set_default():
    c = FakeConn(); assert enable_read_only(c, "postgres", _key()) is True
    assert any("default_transaction_read_only" in s for s in c.executed)

@pytest.mark.parametrize("d", ["oracle", "dm"])
def test_enable_oracle_dm_issues_set_transaction(d):
    c = FakeConn(); assert enable_read_only(c, d, _key()) is True
    assert any("SET TRANSACTION READ ONLY" in s for s in c.executed)

def test_unknown_dialect_returns_false_without_executing():
    c = FakeConn(); assert enable_read_only(c, "sqlserver", _key()) is False
    assert c.executed == []

# 2. 能力缓存：明确拒绝才缓存，且按 pool_key 隔离
def test_explicit_rejection_cached_per_pool_key(monkeypatch):
    monkeypatch.setattr(readonly, "_capability", {})
    boom = PermissionError("READ ONLY transaction not supported")
    c = FakeConn(fail_on={"READ ONLY": boom})
    assert enable_read_only(c, "mysql", "k1") is False
    c2 = FakeConn(fail_on={"READ ONLY": boom})
    assert enable_read_only(c2, "mysql", "k1") is False
    assert c2.executed == []                      # 第二次不再探测（缓存命中）
    c3 = FakeConn(fail_on={"READ ONLY": boom})
    assert enable_read_only(c3, "mysql", "k2") is False
    assert c3.executed != []                      # 换 pool_key 重新探测

# 3. 瞬时错误不缓存（一次网络抖动不许永久毒化该层）
def test_transient_error_not_cached(monkeypatch):
    monkeypatch.setattr(readonly, "_capability", {})
    c = FakeConn(fail_on={"READ ONLY": ConnectionError("Lost connection to MySQL server during query")})
    assert enable_read_only(c, "mysql", "k1") is False
    c2 = FakeConn()
    assert enable_read_only(c2, "mysql", "k1") is True   # 恢复了就重新探测并生效

# 4. 开关关闭
def test_guard_off(monkeypatch):
    monkeypatch.setattr(readonly, "READONLY_DB_GUARD", "off")
    c = FakeConn(); assert enable_read_only(c, "mysql", _key()) is False
    assert c.executed == []

# 5. 恢复：PG 恢复原值；MySQL 无需恢复；恢复失败弃连接
def test_release_restores_pg_original_value(monkeypatch):
    monkeypatch.setattr(readonly, "_capability", {}); monkeypatch.setattr(readonly, "_original", {})
    c = FakeConn(); enable_read_only(c, "postgres", "k1")
    c.executed.clear()
    assert release_read_only(c, "postgres", "k1", ds={"db_type": "postgres"}) is True
    assert any("default_transaction_read_only" in s for s in c.executed)

def test_release_noop_for_mysql():
    c = FakeConn(); enable_read_only(c, "mysql", _key()); c.executed.clear()
    assert release_read_only(c, "mysql", _key(), ds={"db_type": "mysql"}) is True
    assert c.executed == []

def test_release_failure_discards_connection(monkeypatch):
    monkeypatch.setattr(readonly, "_capability", {}); monkeypatch.setattr(readonly, "_original", {"k1": "off"})
    c = FakeConn(); enable_read_only(c, "postgres", "k1")
    c.fail_on["default_transaction_read_only"] = ConnectionError("connection reset")
    assert release_read_only(c, "postgres", "k1", ds={"db_type": "postgres"}) is False
    assert c.closed is True        # 连接被弃，不回池

# 6. 返回值契约
def test_readonly_field_true():
    assert readonly_field("mysql", True) == {"read_only_enforced": True}

def test_readonly_field_unsupported_note():
    r = readonly_field("dm", False)
    assert r["read_only_enforced"] is False and "一二层" in r["note"]

def test_readonly_field_txn_exempt_note():
    r = readonly_field("mysql", False, exempt_reason="txn")
    assert "事务" in r["note"]

def test_readonly_field_explain_exempt_note():
    r = readonly_field("oracle", False, exempt_reason="explain")
    assert "PLAN_TABLE" in r["note"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_readonly.py -q`
Expected: FAIL（`ModuleNotFoundError: db_auto_router.readonly`）

- [ ] **Step 3: 实现 `db_auto_router/readonly.py` 与 `pool.py` 两个函数**

`readonly.py` 实现要点：

- `_READONLY_SQL: dict[str, tuple[str, bool]]`，value = (开关语句, 是否需要恢复)：
  `{"mysql": ("START TRANSACTION READ ONLY", False), "postgres": ("SET default_transaction_read_only=on", True), "gaussdb": ("SET default_transaction_read_only=on", True), "oracle": ("SET TRANSACTION READ ONLY", False), "dm": ("SET TRANSACTION READ ONLY", False)}`
- `_capability: dict[str, bool]`（pool_key → 是否支持，只存 negative）、`_original: dict[str, str]`（pool_key → PG 原值）
- `enable_read_only`：开关 off / 方言不在表 → False 且不执行；否则 `with safe_cursor(conn) as cur: cur.execute(sql)`；成功 → `_capability[key]=True`（仅用于跳过"明确拒绝"的重复探测，**不**据此跳过后续查询的 SET）；抛异常 → `is_transient_error(e)` 为真则不动缓存返回 False，否则 `_capability[key]=False` 返回 False；两种都 `logger` 记一次
  - PG/GaussDB 首次成功时多执行一次 `SELECT current_setting('default_transaction_read_only')` 存 `_original[key]`（后续查询不再读）
- `release_read_only`：方言不需要恢复 → True；需要恢复 → 执行 `SET default_transaction_read_only=<原值>`，成功 True；失败 → `pool.discard_connection(ds, conn, target_db)` 并返回 False
- `readonly_field(dialect, enforced, exempt_reason)`：enforced 真 → `{"read_only_enforced": True}`；否则 `{"read_only_enforced": False, "note": ...}`，note 三种文案：`exempt_reason="txn"` → "事务内执行未套数据库只读（会隐式提交你在飞的事务）；本语句仍过了一二层只读校验"；`"explain"` → "EXPLAIN 未套数据库只读（Oracle/达梦的 EXPLAIN 要写 PLAN_TABLE）；仍过了一二层"；None → f"该数据源（{dialect}）未启用数据库只读兜底（库不支持该语法），已跳过；仍有一二层保护"

`pool.py` 末尾追加（复用已有的 `_make_pool_key` / `_release_slot_locked` / `safe_close`）：

```python
def pool_key_for(ds: Dict[str, str], target_db: str = None) -> str:
    """公开的连接池 key 计算——只读层按连接身份缓存能力，与池同口径。"""
    return _make_pool_key(ds, target_db)


def discard_connection(ds: Dict[str, str], conn, target_db: str = None) -> None:
    """关闭一条不应回池的连接，并释放它的借出名额（池容量计数不能漏）。"""
    if conn is None:
        return
    pool_key = _make_pool_key(ds, target_db)
    safe_close(conn)
    with _lock:
        _release_slot_locked(pool_key, conn)
```

`config.py` 加：`READONLY_DB_GUARD = os.environ.get("MCP_READONLY_DB_GUARD", "on").strip().lower()`

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_readonly.py -q`
Expected: PASS

- [ ] **Step 5: 补开关文档**

`.env` 加 `MCP_READONLY_DB_GUARD=on`；`docs/configuration.md` 「连接与查询」表加一行：`MCP_READONLY_DB_GUARD` | `on` | 只读通道第三层（数据库只读兜底）总开关；库不认只读语法时该层自动关闭并在返回值 note 说明。

- [ ] **Step 6: 提交**

```bash
git add db_auto_router/readonly.py db_auto_router/pool.py db_auto_router/config.py tests/dbfakes.py tests/test_readonly.py .env docs/configuration.md
git commit -m "feat: 只读通道第三层——数据库只读兜底（五方言）+ 能力缓存 + 连接池弃连能力

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: 第二层接入三个只读通道 + PermissionError 直通

**Files:**
- Modify: `db_auto_router/tools.py`（query 的 `resolved = await workspace._resolve_datasource(...)` 之后；explain_query 同位置；三处 `except` 补直通）
- Modify: `db_auto_router/csvio.py`（`export_csv_impl` 的 `validate_read_only_sql(sql)` 之后）
- Create: `tests/test_guard_wiring.py`

**Interfaces:**
- Consumes: Task 1 的 `validate_read_statement_ast(sql, dialect)`
- Produces: 无新公开接口；改变三个工具的拒绝路径形状（`PermissionError` 直通，不被包成 `ToolFailure`）

- [ ] **Step 1: 写失败测试 `tests/test_guard_wiring.py`**

测试用 monkeypatch 把数据源解析与执行引擎换成假的，专测接线本身：

```python
import pytest
from db_auto_router import tools

FAKE_DS = {"db_code": "x", "db_type": "mysql", "conn_url": "127.0.0.1:3306",
           "db_user": "u", "db_password": "p", "write_confirm": 1}

@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools.workspace, "_resolve_datasource",
                        lambda db_code: {"sources": {}, "db_code": "x", "ds": FAKE_DS})
    monkeypatch.setattr(tools, "execute_read_only_query",
                        lambda sql, ds, db, **k: {"data": [], "total_rows": 0, "dialect": "mysql"})

# 1. 第二层拒绝在 query 工具里仍以 PermissionError 出面（不被包成 ToolFailure）
async def test_query_layer2_rejection_stays_permission_error(wired):
    with pytest.raises(PermissionError):
        await tools.query("WITH c AS (INSERT INTO t VALUES (1)) SELECT * FROM c", ctx=None)

# 2. explain_query 同样过第二层
async def test_explain_layer2_rejection_stays_permission_error(wired, monkeypatch):
    monkeypatch.setattr(tools, "explain_query_impl", lambda *a, **k: {"plan": ""})
    with pytest.raises(PermissionError):
        await tools.explain_query("DELETE FROM t", ctx=None)

# 3. export_csv 的拒绝也是 PermissionError 直通
async def test_export_csv_layer2_rejection_stays_permission_error(wired, monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "export_csv_impl", lambda *a, **k: {"success": True})
    # 直接打 impl 层：绕过工具层，验 csvio 内的接线
    from db_auto_router import csvio
    with pytest.raises(PermissionError):
        csvio.export_csv_impl("DELETE FROM t", str(tmp_path / "o.csv"), FAKE_DS, "x")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_guard_wiring.py -q`
Expected: FAIL——`query("WITH c AS (INSERT...)") 当前不抛 PermissionError（第二层还没接），或抛了但被包成 ToolFailure`

- [ ] **Step 3: 接线**

`tools.py` `query`：在 `resolved_db_code = resolved["db_code"]` 之后、`if txn_id:` 之前插入：

```python
        validate_read_statement_ast(sql, resolved["ds"].get("db_type", ""))
```

`tools.py` `explain_query`：在 `resolved_db_code = resolved["db_code"]` 之后、`_run_db(explain_query_impl, ...)` 之前插入同一行。

`tools.py` 三个工具（query / export_csv / explain_query）各自的 `except` 组里，在 `except ToolFailure` **之前**加：

```python
    except PermissionError as e:
        # 第二层拒绝：与第一层同类型直通。不直通会被 except Exception 包成
        # ToolFailure("PermissionError: ...")，错误形状与第一层不一致，
        # 技能层按文案匹配的分支会失配
        _audit_error("<tool>", resolved_db_code, sql, e, txn_id)
        raise
```

（`export_csv` 的 `txn_id` 参数不存在，去掉该实参。）

`csvio.py` `export_csv_impl`：在 `validate_read_only_sql(sql)` 之后插入：

```python
    validate_read_statement_ast(sql, ds.get("db_type", ""))
```

并在顶部 import：`from .sqlparse import validate_read_statement_ast`。

`tools.py` 顶部 import 补 `from .sqlparse import validate_read_statement_ast`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_guard_wiring.py tests/test_sqlparse.py -q`
Expected: PASS

- [ ] **Step 5: 回归**

Run: `python tests/test_sqlguard.py`
Expected: `✅ 全部通过（5 个缺陷已修 + 33 项放行 + 30 项拦截 + 6 项拆分 无回归）`

- [ ] **Step 6: 提交**

```bash
git add db_auto_router/tools.py db_auto_router/csvio.py tests/test_guard_wiring.py
git commit -m "feat: 第二层接入三个只读通道 + PermissionError 直通（错误形状与第一层一致）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: 第三层接入 query 读路径 + read_only_enforced 字段

**Files:**
- Modify: `db_auto_router/readengine.py`
- Modify: `db_auto_router/tools.py`（query 的 txn 分支）
- Modify: `tests/test_guard_wiring.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `enable_read_only` / `release_read_only` / `readonly_field` / `pool.pool_key_for`
- Produces: `query` / `export_csv` / `explain_query` 成功返回统一携带 `read_only_enforced`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_guard_wiring.py`（用 `tests/dbfakes.py` 的假连接替掉池）：

```python
from db_auto_router import readengine, readonly
from tests.dbfakes import FakeConn, FakeCursor

def _fake_pool(monkeypatch, conn):
    monkeypatch.setattr(readengine, "get_connection", lambda ds, db=None: conn)
    monkeypatch.setattr(readengine, "put_connection", lambda ds, c, db=None: None)

# 1. 成功返回带字段，且确实对连接下了只读语句
def test_query_result_carries_read_only_enforced(monkeypatch):
    c = FakeConn(); _fake_pool(monkeypatch, c)
    # 假游标要能返回结果集：FakeCursor.description 置两列，fetchmany 给一批即空
    ...
    r = readengine.execute_read_only_query("SELECT * FROM t", FAKE_DS, "x")
    assert r["read_only_enforced"] is True
    assert any("START TRANSACTION READ ONLY" in s for s in c.executed)

# 2. 库不认只读语法 → false + note，查询照常成功
def test_query_degrades_with_note_when_db_rejects(monkeypatch):
    c = FakeConn(fail_on={"READ ONLY": PermissionError("nope")})
    _fake_pool(monkeypatch, c)
    r = readengine.execute_read_only_query("SELECT * FROM t", FAKE_DS, "x")
    assert r["read_only_enforced"] is False and "一二层" in r["note"]

# 3. 摘要形状也带字段（第二个成功返回形状）
def test_summary_shape_carries_field(monkeypatch): ...

# 4. 事务内读路径：豁免 + 标注，且绝不碰连接的只读开关
async def test_txn_read_marks_exempt(monkeypatch):
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools.workspace, "_resolve_datasource",
                        lambda db_code: {"sources": {}, "db_code": "x", "ds": FAKE_DS})
    monkeypatch.setattr(tools, "execute_in_transaction",
                        lambda txn_id, sql: {"data": [], "total_rows": 0, "dialect": "mysql"})
    r = await tools.query("SELECT 1", txn_id="t1", ctx=None)
    assert r["read_only_enforced"] is False and "事务" in r["note"]
```

（FakeCursor 需能返回 `description` 与 `fetchmany`——在 `tests/dbfakes.py` 里给 FakeCursor 加 `description`/`fetchmany` 两个可注入属性，默认 `description=None`、`fetchmany=lambda n: []`。）

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_guard_wiring.py -q`
Expected: FAIL（`KeyError: 'read_only_enforced'`）

- [ ] **Step 3: 接线 `readengine.py`**

在重试循环内、`conn = get_connection(...)` 拿到连接之后、`start_time = time.time()` 之前：

```python
                # 第三层：数据库只读兜底（该库不认就降级，note 由返回值说明）
                ro_key = pool.pool_key_for(ds, target_db if is_cross_db else None)
                ro_enforced = enable_read_only(conn, dialect, ro_key)
```

`dialect` 变量当前在函数尾部才算（`readengine.py:172`）——把 `dialect = ds.get("db_type", "").lower().strip()` 上移到循环之前，尾部改为复用。

循环的 `finally`（当前是 `if pooled_conn is not None: put_connection(...)`）改为：

```python
            finally:
                if pooled_conn is not None:
                    # 恢复必须先于归还：连接一旦回池就可能被别的调用者取走，
                    # 带着只读标记会让他们后续的 execute 写库失败
                    if ro_enforced and not release_read_only(
                            conn, dialect, ro_key, ds, target_db if is_cross_db else None):
                        pooled_conn = None   # 恢复失败：连接已弃，MUST NOT 回池
                if pooled_conn is not None:
                    put_connection(ds, pooled_conn, target_db if is_cross_db else None)
```

注意 `ro_key` / `ro_enforced` 在循环内赋值、finally 内读取——把 `ro_key = None; ro_enforced = False` 初始化放在循环外（`direct_conn` 分支不需要恢复，`release_read_only` 对 `False` 直接返回 True）。

两个成功返回形状（摘要 dict 与常规 dict）在 return 前统一：

```python
        result.update(readonly_field(dialect, ro_enforced))
```

（直接连接分支 `direct_conn` 不设 `ro_enforced`——它用完即关，无需恢复；此时字段为 False + note，语义正确：该路径没有池连接可标记。若要让直连也生效，在同一位置对 `direct_conn` 调一次 `enable_read_only` 即可——**本计划选择不调**，因为直连路径本来就是 timeout 覆盖的例外场景，一二层已覆盖。）

`tools.py` `query` 的 txn 分支：

```python
        if txn_id:
            result_data = await _run_db(execute_in_transaction, txn_id, sql)
            # 第三层对事务内执行豁免：MySQL 上开只读会隐式提交用户在飞的事务。
            # 如实标注，不让调用方以为这条查询有数据库级兜底
            if isinstance(result_data, dict):
                result_data.update(readonly_field(
                    result_data.get("dialect", ""), False, exempt_reason="txn"))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_guard_wiring.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add db_auto_router/readengine.py db_auto_router/tools.py tests/test_guard_wiring.py tests/dbfakes.py
git commit -m "feat: 第三层接入 query 读路径 + read_only_enforced 字段（事务内豁免并标注）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: 第三层接入 export_csv + explain_query 豁免标注

**Files:**
- Modify: `db_auto_router/csvio.py`
- Modify: `db_auto_router/tools.py`（explain_query 的豁免标注）
- Modify: `tests/test_guard_wiring.py`（追加）

**Interfaces:**
- Consumes: Task 2 / Task 4 的同名接口
- Produces: `export_csv` 与 `explain_query` 的返回字段补齐（三个工具契约完整）

- [ ] **Step 1: 写失败测试**

```python
# 1. 池连接路径：下只读 + 带字段 + 恢复
def test_export_csv_pooled_path_enforces(monkeypatch, tmp_path):
    c = FakeConn(); _fake_pool(monkeypatch, c)
    out = csvio.export_csv_impl("SELECT * FROM t", str(tmp_path / "o.csv"), FAKE_DS, "x")
    assert out["read_only_enforced"] is True
    assert any("START TRANSACTION READ ONLY" in s for s in c.executed)

# 2. 专用连接路径（timeout 覆盖）：也带字段；连接用完即关，不做恢复
def test_export_csv_direct_path_field(monkeypatch, tmp_path):
    c = FakeConn()
    monkeypatch.setattr(csvio, "_open_direct_connection_with_timeout", lambda *a, **k: c)
    out = csvio.export_csv_impl("SELECT * FROM t", str(tmp_path / "o.csv"), FAKE_DS, "x", timeout=60)
    assert out["read_only_enforced"] is False   # 直连例外场景，一二层覆盖
    assert c.closed is True

# 3. explain_query 豁免标注
async def test_explain_marks_exempt(monkeypatch):
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools.workspace, "_resolve_datasource",
                        lambda db_code: {"sources": {}, "db_code": "x", "ds": FAKE_DS})
    monkeypatch.setattr(tools, "explain_query_impl", lambda *a, **k: {"plan": "x"})
    r = await tools.explain_query("SELECT 1", ctx=None)
    assert r["read_only_enforced"] is False and "PLAN_TABLE" in r["note"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_guard_wiring.py -q`
Expected: FAIL（字段缺失）

- [ ] **Step 3: 接线**

`csvio.py` `export_csv_impl`：在 `conn_is_direct` 确定之后、`with safe_cursor(conn)` 之前加 `enable_read_only`；成功返回 dict 里加 `result.update(readonly_field(dialect_var, ro_enforced))`；`finally` 里对**池连接**分支在 `put_connection` 之前加 `release_read_only`（直连分支不恢复，连接随即关闭）。`dialect_var = ds.get("db_type", "").lower().strip()`（该值结果 dict 里已在用，提取成变量复用）。

`tools.py` `explain_query`：在 `result_data = await _run_db(explain_query_impl, ...)` 之后、`audit_log(...)` 之前：

```python
        if isinstance(result_data, dict):
            # 第三层对 EXPLAIN 豁免：Oracle/达梦的 EXPLAIN PLAN FOR 要写 PLAN_TABLE，
            # 只读事务里必报错。一二层照旧，此处如实标注
            result_data.update(readonly_field(
                resolved["ds"].get("db_type", ""), False, exempt_reason="explain"))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_guard_wiring.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add db_auto_router/csvio.py db_auto_router/tools.py tests/test_guard_wiring.py
git commit -m "feat: 第三层接入 export_csv + explain_query 豁免标注（三工具契约补齐）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: 文档、技能层记录与全量回归

**Files:**
- Modify: `docs/configuration.md`（「安全设计」章节补三层说明 + 返回值契约）
- Modify: `README.md`（「🔐 安全设计」补一段三层防线）
- Modify: `~/.claude/skills/db-ops/rules/tools-reference.md`（**仓库外**：记录新返回字段与豁免语义）

- [ ] **Step 1: 更新 `docs/configuration.md`**

新增小节「只读通道三层防线」：三层各是什么、失败方向、两个开关、`read_only_enforced` 契约（含"不承诺 DDL 边界"的原话）、三条豁免路径（事务内 / EXPLAIN / 直连超时）各自的 note 文案。

- [ ] **Step 2: 更新 `README.md`**

「🔐 安全设计」节在「只读守卫」段后补一段：三层结构一句话图 + "任何一层不可用时如实降级并标注，不假装安全"。

- [ ] **Step 3: 更新技能层（仓库外文件）**

`~/.claude/skills/db-ops/rules/tools-reference.md` 的 `query` / `export_csv` / `explain_query` 三条返回值说明里各加：`read_only_enforced`（true=数据库只读层生效；false=该库不支持或本条路径豁免，note 会说明原因，一二层仍生效）。**MUST NOT** 因为该字段为 false 就向用户报告"查询无保护"——它的语义是"数据库级兜底没兜上"，不是"没有防线"。

- [ ] **Step 4: 全量回归**

Run: `python -m pytest tests/ -q && python tests/test_sqlguard.py`
Expected: 全部 PASS（含 5 方言语料矩阵）

- [ ] **Step 5: 真机检查单（交给用户执行，不代跑）**

在活库上依次确认，每条的预期：
1. `query("SELECT 1")` → 返回含 `read_only_enforced`，MySQL/PG 应为 true，达梦/Oracle/GaussDB 按该库是否认这条语法 true/false 均可，false 时 note 说明原因
2. `query("SELECT pg_read_file('/etc/passwd')")`（PG 库）→ 第二层拒绝，文案含逃生门
3. `query("WITH c AS (INSERT INTO t VALUES (1)) SELECT * FROM c")` → 第二层拒绝
4. `execute("UPDATE ... WHERE id=1")` → 确认弹窗照旧，写通道无变化
5. `begin_transaction` → `query(txn_id=...)` → 返回 `read_only_enforced: false` + 事务 note，且事务内的写操作仍可正常 `execute`

- [ ] **Step 6: 提交**

```bash
git add docs/configuration.md README.md
git commit -m "docs: 只读通道三层防线 + read_only_enforced 契约 + 豁免路径说明

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Self-Review 记录

**1. Spec 覆盖**：G1→T1；G2→T2/T4/T5；G3→T1（sqlglot 缺失）、T2（能力缓存降级）；G4→无新增工具（T3/T4/T5 只改内部）；G5→T1 逃生门文案、T2 note 文案。A1–A5→T1/T2 测试。C1→T3 接线位置（resolve 之后）。C7→T3 直通分支。R1→T1 三态；R2→T4 事务豁免；R3→T5 explain 豁免 + 契约降级；R4→T2 pool_key + 瞬时错误；R5→T2 readonly_field + T4 两个形状；R6→T4 finally 顺序；R7→T3；R8→T1 钉版本 + 语料。验收标准的三项→T6 Step 4/5。

**2. 步骤扫描**：每个 Step 1 都给足测试名与断言；每个 Step 3 给足签名、文件、钉死值；未留 "TBD / 适当处理" 类空转步骤。

**3. 类型一致性**：`validate_read_statement_ast(sql, dialect)` 在 T1/T3 一致；`enable_read_only(conn, dialect, pool_key)` / `release_read_only(conn, dialect, pool_key, ds, target_db)` / `readonly_field(dialect, enforced, exempt_reason)` 在 T2/T4/T5 一致；`pool_key_for(ds, target_db)` / `discard_connection(ds, conn, target_db)` 在 T2 定义、T4 使用一致。

**4. Review Focus**：5 条全部有对应测试（见 Task 1/2/4 的测试清单）。

**5. 比例**：代码块仅占测试断言与钉死的 SQL 语句，未写实现体（除 pool.py 两个 20 行内小函数与 sqlglot 流程要点，均为签名+测试定不下来的部分）。

**已知偏离（已在计划内说明）**：设计说"解析超时 5s"，本计划改为**确定性长度/深度预检**——`asyncio.to_thread` 里跑解析，线程超时杀不掉 Python 线程，确定性预检更可靠；病态 SQL 的拒绝方向不变。
