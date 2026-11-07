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

from db_auto_router import tools, csvio
from db_auto_router.errors import ToolFailure

PG_DS = {"db_code": "x", "db_type": "postgres",
         "conn_url": "host=127.0.0.1 port=5432 dbname=d",
         "db_user": "u", "db_password": "p", "write_confirm": 1}


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
