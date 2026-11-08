# -*- coding: utf-8 -*-
"""tools 层接线：query / export_csv 把 ctx 变成一个 progress_cb 传进引擎。

验证三件事：make_progress_reporter 用 (loop, ctx.report_progress) 造、loop 非空、
造出来的 progress_cb 确实作为 progress_cb  kwarg 传给了执行引擎。节流/线程桥本身
在 test_progress.py 覆盖，这里只验"接上了"。
"""
import asyncio

import pytest

from db_auto_router import tools


class _Ctx:
    async def report_progress(self, progress, total=None, message=None):
        return None


@pytest.fixture
def wired(monkeypatch):
    async def fake_resolve(db_code):
        return {"sources": {}, "db_code": "x", "ds": {"db_type": "postgres"}}

    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools.workspace, "_resolve_datasource", fake_resolve)


def test_query_wires_progress_reporter_from_ctx(wired, monkeypatch):
    seen = {}
    sentinel = lambda *a, **k: None

    def fake_mpr(loop, report_coro, min_interval=1.0):
        seen["loop"] = loop
        seen["report_coro"] = report_coro
        return sentinel

    monkeypatch.setattr(tools, "make_progress_reporter", fake_mpr)
    eng = {}

    def fake_engine(sql, ds, db, **k):
        eng["progress_cb"] = k.get("progress_cb", "MISSING")
        return {"data": [], "total_rows": 0, "dialect": "postgres"}

    monkeypatch.setattr(tools, "execute_read_only_query", fake_engine)
    ctx = _Ctx()

    asyncio.run(tools.query("SELECT 1", ctx=ctx))

    assert seen["report_coro"] == ctx.report_progress   # 用 ctx.report_progress 造
    assert seen["loop"] is not None                     # 抓到了事件循环
    assert eng["progress_cb"] is sentinel               # reporter 结果传进了引擎


def test_export_wires_progress_reporter_from_ctx(wired, monkeypatch, tmp_path):
    seen = {}

    def fake_mpr(loop, report_coro, min_interval=1.0):
        seen["report_coro"] = report_coro
        return lambda *a, **k: None

    monkeypatch.setattr(tools, "make_progress_reporter", fake_mpr)
    eng = {}

    def fake_impl(sql, file_path, ds, db, timeout=None, max_rows=None, progress_cb=None):
        eng["progress_cb"] = progress_cb
        return {"success": True, "rows_exported": 0, "file_path": file_path,
                "columns": [], "truncated": False, "execution_time_ms": 0,
                "dialect": "postgres"}

    monkeypatch.setattr(tools, "export_csv_impl", fake_impl)
    ctx = _Ctx()

    asyncio.run(tools.export_csv("SELECT 1", str(tmp_path / "o.csv"), ctx=ctx))

    assert seen["report_coro"] == ctx.report_progress
    assert callable(eng["progress_cb"])
