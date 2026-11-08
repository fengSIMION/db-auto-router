# -*- coding: utf-8 -*-
"""db_health 索引卫生：冗余/未用索引的纯逻辑 + 各方言接线。

核心纯逻辑 find_redundant_indexes 单独测（最易错）：最左前缀覆盖、完全重复、
以及"唯一/主键索引提供约束、不被更长索引覆盖"这条安全边界。
"""
from db_auto_router import dbhealth
from db_auto_router.dbhealth import find_redundant_indexes


def _idx(table, name, columns, unique=False, primary=False):
    return {"table": table, "name": name, "columns": list(columns),
            "unique": unique, "primary": primary}


# ---- find_redundant_indexes：纯逻辑 ----
def test_strict_leftmost_prefix_is_redundant():
    idxs = [
        _idx("t", "idx_a", ["a"]),
        _idx("t", "idx_ab", ["a", "b"]),
    ]
    findings = find_redundant_indexes(idxs)
    assert len(findings) == 1
    f = findings[0]
    assert f["index"] == "idx_a"
    assert f["covered_by"] == "idx_ab"
    assert f["reason"] == "最左前缀"


def test_non_prefix_columns_not_redundant():
    idxs = [
        _idx("t", "idx_ab", ["a", "b"]),
        _idx("t", "idx_ac", ["a", "c"]),
    ]
    assert find_redundant_indexes(idxs) == []


def test_equal_length_different_columns_not_redundant():
    idxs = [
        _idx("t", "idx_a", ["a"]),
        _idx("t", "idx_b", ["b"]),
    ]
    assert find_redundant_indexes(idxs) == []


def test_exact_duplicate_flags_all_but_one():
    idxs = [
        _idx("t", "idx_a_1", ["a"]),
        _idx("t", "idx_a_2", ["a"]),
    ]
    findings = find_redundant_indexes(idxs)
    assert len(findings) == 1
    # 保留字典序第一个 idx_a_1，另一个标为完全重复
    assert findings[0]["index"] == "idx_a_2"
    assert findings[0]["covered_by"] == "idx_a_1"
    assert findings[0]["reason"] == "完全重复"


def test_unique_index_never_flagged_redundant():
    """唯一索引提供约束，更长索引不保证前缀唯一性——绝不能判它可删。"""
    idxs = [
        _idx("t", "uq_a", ["a"], unique=True),
        _idx("t", "idx_ab", ["a", "b"]),
    ]
    assert find_redundant_indexes(idxs) == []


def test_primary_index_never_flagged_redundant():
    idxs = [
        _idx("t", "pk", ["a"], primary=True),
        _idx("t", "idx_ab", ["a", "b"]),
    ]
    assert find_redundant_indexes(idxs) == []


def test_prefix_covered_by_unique_longer_still_flags_short():
    """短的是普通索引、长的是唯一索引：短的仍可删（约束由长的额外提供，前缀覆盖成立）。"""
    idxs = [
        _idx("t", "idx_a", ["a"]),
        _idx("t", "uq_ab", ["a", "b"], unique=True),
    ]
    findings = find_redundant_indexes(idxs)
    assert len(findings) == 1
    assert findings[0]["index"] == "idx_a"
    assert findings[0]["covered_by"] == "uq_ab"


def test_different_tables_do_not_cross_match():
    idxs = [
        _idx("t1", "idx_a", ["a"]),
        _idx("t2", "idx_ab", ["a", "b"]),
    ]
    assert find_redundant_indexes(idxs) == []


# ---- db_health_impl 编排 + PG 方言（fake 连接，不碰真库）----
from tests.dbfakes import FakeConn, FakeCursor


class _PgHealthCursor(FakeCursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        low = sql.lower()
        if "array_position" in low:                       # 冗余：索引列（有序）
            self.description = [("index_name",), ("table_name",), ("indisunique",),
                                ("indisprimary",), ("column_name",), ("seq",)]
            self._rows = [[
                ("idx_a", "t", False, False, "a", 1),
                ("idx_ab", "t", False, False, "a", 1),
                ("idx_ab", "t", False, False, "b", 2),
            ]]
        elif "pg_stat_user_indexes" in low:               # 未用：idx_scan=0
            self.description = [("table_name",), ("index_name",), ("idx_scan",)]
            self._rows = [[("t", "idx_c", 0)]]
        else:
            self.description = []
            self._rows = []


class _PgHealthConn(FakeConn):
    def cursor(self):
        return _PgHealthCursor(self)


def _patch_health_conn(monkeypatch, conn):
    monkeypatch.setattr(dbhealth, "get_connection", lambda ds, target_db=None: conn)
    monkeypatch.setattr(dbhealth, "put_connection", lambda ds, c, target_db=None: None)


def test_db_health_impl_pg_end_to_end(monkeypatch):
    _patch_health_conn(monkeypatch, _PgHealthConn())

    result = dbhealth.db_health_impl({"db_type": "postgres"}, "x")

    assert result["dialect"] == "postgres"
    red = result["redundant_indexes"]
    assert any(f["index"] == "idx_a" and f["covered_by"] == "idx_ab" for f in red)
    un = result["unused_indexes"]
    fc = next((f for f in un if f["index"] == "idx_c"), None)
    assert fc is not None
    # 未用项必须带"窗口/候选"口径，绝不能讲成"可安全删除"
    assert ("窗口" in fc["suggestion"]) or ("候选" in fc["suggestion"])
    # 每条都带可执行的 drop_sql
    assert all(f.get("drop_sql") for f in red + un)
    assert result["note"]   # PG 也要说明 idx_scan 的窗口来源


def test_db_health_impl_unsupported_dialect_degrades(monkeypatch):
    _patch_health_conn(monkeypatch, _PgHealthConn())
    result = dbhealth.db_health_impl({"db_type": "sqlite"}, "x")
    assert result["redundant_indexes"] == []
    assert result["unused_indexes"] == []
    assert result["note"]


def test_db_health_impl_never_raises_on_conn_error(monkeypatch):
    def boom(ds, target_db=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(dbhealth, "get_connection", boom)
    result = dbhealth.db_health_impl({"db_type": "postgres"}, "x")
    # 尽力而为：连接失败也不抛，note 说明
    assert result["note"]
    assert result["redundant_indexes"] == [] and result["unused_indexes"] == []


# ---- MySQL / Oracle-DM 方言：冗余能判，未用缺统计则降级 ----
class _MyHealthCursor(FakeCursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "statistics" in sql.lower():
            self.description = [("table_name",), ("index_name",), ("non_unique",),
                                ("seq_in_index",), ("column_name",)]
            self._rows = [[
                ("t", "idx_a", 1, 1, "a"),
                ("t", "idx_ab", 1, 1, "a"),
                ("t", "idx_ab", 1, 2, "b"),
            ]]
        else:
            self.description = []
            self._rows = []


class _MyHealthConn(FakeConn):
    def __init__(self, fail_on=None):
        super().__init__(fail_on)

    def cursor(self):
        return _MyHealthCursor(self)


class _OraHealthCursor(FakeCursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "user_ind_columns" in sql.lower():
            self.description = [("index_name",), ("table_name",), ("uniqueness",),
                                ("column_name",), ("seq",)]
            self._rows = [[
                ("idx_a", "t", "NONUNIQUE", "a", 1),
                ("idx_ab", "t", "NONUNIQUE", "a", 1),
                ("idx_ab", "t", "NONUNIQUE", "b", 2),
            ]]
        else:
            self.description = []
            self._rows = []


class _OraHealthConn(FakeConn):
    def __init__(self, fail_on=None):
        super().__init__(fail_on)

    def cursor(self):
        return _OraHealthCursor(self)


def test_db_health_impl_mysql_redundancy_and_unused_degrade(monkeypatch):
    # performance_schema 没开：未用查询炸 → 降级 note，但冗余照常判
    conn = _MyHealthConn(fail_on={"COUNT_STAR": RuntimeError("perf_schema off")})
    _patch_health_conn(monkeypatch, conn)

    result = dbhealth.db_health_impl({"db_type": "mysql"}, "x")

    assert result["dialect"] == "mysql"
    red = result["redundant_indexes"]
    assert any(f["index"] == "idx_a" and f["covered_by"] == "idx_ab" for f in red)
    assert red[0]["drop_sql"].startswith("ALTER TABLE")   # MySQL 删索引语法
    assert "未用" in (result["note"] or "")               # 降级说明


def test_db_health_impl_oracle_redundancy_and_unused_degrade(monkeypatch):
    # 没有 v$ 权限：未用查询炸 → 降级 note，冗余照常判
    conn = _OraHealthConn(fail_on={"OBJECT_USAGE": RuntimeError("no v$ privilege")})
    _patch_health_conn(monkeypatch, conn)

    result = dbhealth.db_health_impl({"db_type": "oracle"}, "x")

    assert result["dialect"] == "oracle"
    assert any(f["index"] == "idx_a" and f["covered_by"] == "idx_ab"
               for f in result["redundant_indexes"])
    assert "未用" in (result["note"] or "")


# ---- 工具层接线：db_health 工具解析数据源并调 db_health_impl ----
def test_db_health_tool_wires_impl(monkeypatch):
    import asyncio
    from db_auto_router import tools

    captured = {}

    def fake_impl(ds, db_code):
        captured["ds"] = ds
        return {"dialect": "postgres", "db_code": db_code, "redundant_indexes": [],
                "unused_indexes": [], "note": None, "execution_time_ms": 1.0}

    monkeypatch.setattr(tools, "db_health_impl", fake_impl)
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)

    async def fake_resolve(db_code):
        return {"sources": {}, "db_code": "x", "ds": {"db_type": "postgres"}}

    monkeypatch.setattr(tools.workspace, "_resolve_datasource", fake_resolve)

    r = asyncio.run(tools.db_health(db_code="x"))
    assert r["dialect"] == "postgres"
    assert captured["ds"] == {"db_type": "postgres"}
