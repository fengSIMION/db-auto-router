# -*- coding: utf-8 -*-
"""第二层接入三个只读通道的接线测试（Task 3）。

测的是"接没接上"，不是第二层本身的判定逻辑（那在 test_sqlparse.py）。
关键：用例必须选**第一层放行、第二层拒绝**的语句——高危函数正是这样的语句：
第一层只判语句形态，拦不住"形态是 SELECT 但函数有副作用"。
若拿 `WITH c AS (INSERT...)` 这类第一层本就拦的语句来测，不接第二层测试也会绿。
"""
import os, sys, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from db_auto_router import tools, csvio, readengine, readonly
from db_auto_router.errors import ToolFailure
from tests.dbfakes import FakeConn, FakeCursor

PG_DS = {"db_code": "x", "db_type": "postgres",
         "conn_url": "host=127.0.0.1 port=5432 dbname=d",
         "db_user": "u", "db_password": "p", "write_confirm": 1}
MY_DS = {"db_code": "x", "db_type": "mysql", "conn_url": "127.0.0.1:3306",
         "db_user": "u", "db_password": "p", "write_confirm": 1}


@pytest.fixture(autouse=True)
def _clean_capability_cache(monkeypatch):
    """能力缓存是模块级状态，且 key 由 ds 算出——同一份 FAKE_DS 在不同测试间
    会命中同一个 key。不清就等于把"这个库不支持只读"的结论泄漏给下一个测试，
    测试结果随执行顺序漂移。"""
    monkeypatch.setattr(readonly, "_capability", {})
    monkeypatch.setattr(readonly, "_original", {})


class RowConn(FakeConn):
    """返回一批结果集的假连接：description 两列，fetchmany 给一批然后空。"""

    def __init__(self, rows=60, fail_on=None):
        super().__init__(fail_on=fail_on)
        self._n_rows = rows

    def cursor(self):
        cur = FakeCursor(self)
        cur.description = [("a",), ("b",)]
        cur._rows = [[(i, "x") for i in range(self._n_rows)], []]
        return cur


@pytest.fixture
def wired(monkeypatch):
    """把数据源解析与执行引擎换成假的，只留接线本身是真的。

    注意 _resolve_datasource 是 async 的，假件必须也是协程函数——
    用同步 lambda 会在 `await` 处抛 TypeError，把测试变成测别的东西。
    """
    async def fake_resolve(db_code):
        return {"sources": {}, "db_code": "x", "ds": PG_DS}

    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools.workspace, "_resolve_datasource", fake_resolve)
    monkeypatch.setattr(tools, "execute_read_only_query",
                        lambda sql, ds, db, **k: {"data": [], "total_rows": 0,
                                                  "dialect": "postgres"})


# 1. query：第二层拒绝以 PermissionError 出面，不被包成 ToolFailure --------

def test_query_layer2_rejection_stays_permission_error(wired):
    with pytest.raises(PermissionError):
        asyncio.run(tools.query("SELECT pg_read_file('/etc/passwd')", ctx=None))


def test_query_layer2_rejection_not_wrapped_in_tool_failure(wired):
    """错误形状必须与第一层一致：不能是 ToolFailure('PermissionError: ...')，
    否则技能层按文案匹配的分支会失配。"""
    with pytest.raises(PermissionError) as ei:
        asyncio.run(tools.query("SELECT pg_read_file('/etc/passwd')", ctx=None))
    assert not isinstance(ei.value, ToolFailure)
    assert "execute" in str(ei.value)      # 逃生门必须在


# 2. explain_query：同样过第二层 ------------------------------------------

def test_explain_query_layer2_rejection(wired, monkeypatch):
    monkeypatch.setattr(tools, "explain_query_impl", lambda *a, **k: {"plan": ""})
    with pytest.raises(PermissionError):
        asyncio.run(tools.explain_query("SELECT pg_read_file('/etc/passwd')", ctx=None))


# 3. export_csv：csvio 内的接线（绕过工具层直接打 impl）--------------------

def test_export_csv_layer2_rejection(wired, tmp_path):
    with pytest.raises(PermissionError):
        csvio.export_csv_impl("SELECT pg_read_file('/etc/passwd')",
                              str(tmp_path / "o.csv"), PG_DS, "x")


# 4. 反向：干净查询两层都过，接线没把正常路径堵死 ------------------------

def test_clean_query_still_passes_both_layers(wired):
    r = asyncio.run(tools.query("SELECT 1", ctx=None))
    assert r["total_rows"] == 0


# ---- Task 4：第三层接入 query 读路径 + read_only_enforced 字段 ----------

def _fake_pool(monkeypatch, conn):
    monkeypatch.setattr(readengine, "get_connection", lambda ds, db=None: conn)
    monkeypatch.setattr(readengine, "put_connection", lambda ds, c, db=None: None)


def test_query_result_carries_read_only_enforced(monkeypatch):
    c = RowConn()
    _fake_pool(monkeypatch, c)
    r = readengine.execute_read_only_query("SELECT * FROM t", MY_DS, "x")
    assert r["read_only_enforced"] is True
    assert any("START TRANSACTION READ ONLY" in s for s in c.executed)


def test_query_degrades_with_note_when_db_rejects(monkeypatch):
    """库不认只读语法：字段 false + note 说明，但查询本身照常成功。"""
    c = RowConn(fail_on={"READ ONLY": PermissionError("nope")})
    _fake_pool(monkeypatch, c)
    r = readengine.execute_read_only_query("SELECT * FROM t", MY_DS, "x")
    assert r["read_only_enforced"] is False and "一二层" in r["note"]
    assert r["total_rows"] == 60          # 查询没被第三层拖死


def test_summary_shape_carries_field(monkeypatch):
    """第二个成功返回形状（超过 SUMMARY_THRESHOLD 的摘要 dict）也必须带字段。"""
    c = RowConn(rows=60)
    _fake_pool(monkeypatch, c)
    r = readengine.execute_read_only_query("SELECT * FROM t", MY_DS, "x")
    assert r.get("summary") is True
    assert r["read_only_enforced"] is True


def test_txn_read_marks_exempt(monkeypatch):
    """事务内读路径：豁免第三层 + 如实标注；且绝不碰连接的只读开关。"""
    async def fake_resolve(db_code):
        return {"sources": {}, "db_code": "x", "ds": MY_DS}
    touched = []
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools.workspace, "_resolve_datasource", fake_resolve)
    monkeypatch.setattr(tools, "execute_in_transaction",
                        lambda txn_id, sql: {"data": [], "total_rows": 0, "dialect": "mysql"})
    monkeypatch.setattr(readonly, "enable_read_only",
                        lambda *a, **k: touched.append(a) or True)
    r = asyncio.run(tools.query("SELECT 1", txn_id="t1", ctx=None))
    assert r["read_only_enforced"] is False and "事务" in r["note"]
    assert touched == []                   # 事务路径没有去开只读


# ---- Task 5：第三层接入 export_csv + explain_query 豁免标注 -------------

def test_export_csv_pooled_path_enforces(monkeypatch, tmp_path):
    c = RowConn(rows=2)
    # csvio 有自己对 get_connection/put_connection 的引用，必须 patch 它这份
    monkeypatch.setattr(csvio, "get_connection", lambda ds, db=None: c)
    monkeypatch.setattr(csvio, "put_connection", lambda ds, conn, db=None: None)
    out = csvio.export_csv_impl("SELECT * FROM t", str(tmp_path / "o.csv"), MY_DS, "x")
    assert out["read_only_enforced"] is True
    assert any("START TRANSACTION READ ONLY" in s for s in c.executed)


def test_export_csv_direct_path_field(monkeypatch, tmp_path):
    """timeout 覆盖走专用连接：用完即关，不做恢复；字段如实标 false。"""
    c = RowConn(rows=2)
    monkeypatch.setattr(csvio, "_open_direct_connection_with_timeout", lambda *a, **k: c)
    out = csvio.export_csv_impl("SELECT * FROM t", str(tmp_path / "o.csv"),
                                MY_DS, "x", timeout=60)
    assert out["read_only_enforced"] is False
    assert c.closed is True


def test_explain_marks_exempt(monkeypatch):
    async def fake_resolve(db_code):
        return {"sources": {}, "db_code": "x", "ds": MY_DS}
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools.workspace, "_resolve_datasource", fake_resolve)
    monkeypatch.setattr(tools, "explain_query_impl", lambda *a, **k: {"plan": "x"})
    r = asyncio.run(tools.explain_query("SELECT 1", ctx=None))
    assert r["read_only_enforced"] is False and "PLAN_TABLE" in r["note"]
