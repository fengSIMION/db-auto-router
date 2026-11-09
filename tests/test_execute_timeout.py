# -*- coding: utf-8 -*-
"""execute 补 timeout：只为 PG/GaussDB 抬服务端 statement_timeout（防 30s 杀长 DDL）。

纯逻辑 statement_timeout_sql 单测（最易错、无 IO）；execute_any_sql 的
SET/RESET 接线用假连接验证（SET 在执行前、RESET 在归还前、RESET 失败弃连接）。
"""
from db_auto_router import writeengine
from db_auto_router.writeengine import statement_timeout_sql
from tests.dbfakes import FakeConn, FakeCursor


# ---- 纯逻辑：方言 → 是否/如何设服务端语句超时 ----
def test_pg_and_gaussdb_get_set_statement_timeout():
    assert statement_timeout_sql("postgres", 600000) == "SET statement_timeout = 600000"
    assert statement_timeout_sql("gaussdb", 30000) == "SET statement_timeout = 30000"


def test_dialects_without_server_write_timeout_get_none():
    # MySQL 的 max_execution_time 仅对 SELECT 生效；Oracle/达梦无对应项——没有 30s 杀写的问题
    for d in ("mysql", "oracle", "dm", "sqlite", ""):
        assert statement_timeout_sql(d, 600000) is None


def test_zero_or_none_timeout_gets_no_sql():
    assert statement_timeout_sql("postgres", 0) is None
    assert statement_timeout_sql("postgres", None) is None


# ---- execute_any_sql 接线：SET 在前、RESET 在归还前 ----
class _WCursor(FakeCursor):
    def __init__(self, conn):
        super().__init__(conn)
        self.description = None
        self._rows = [[]]        # 一次空 fetchmany，让语句"执行完"


class _WConn(FakeConn):
    def cursor(self):
        return _WCursor(self)


def _patch(monkeypatch, conn, puts):
    monkeypatch.setattr(writeengine, "get_connection", lambda ds, target_db=None: conn)
    monkeypatch.setattr(writeengine, "put_connection",
                        lambda ds, c, target_db=None: puts.append(c))


def test_execute_pg_sets_and_resets_statement_timeout(monkeypatch):
    conn = _WConn()
    puts = []
    _patch(monkeypatch, conn, puts)

    writeengine.execute_any_sql(
        "CREATE INDEX idx ON t(a)", {"db_type": "postgres"}, "defdb", timeout_ms=600000)

    # SET 在执行语句之前
    assert conn.executed.index("SET statement_timeout = 600000") < \
        conn.executed.index("CREATE INDEX idx ON t(a)")
    assert puts == [conn]


def test_execute_pg_discards_conn_when_reset_fails(monkeypatch):
    # RESET 失败 → 连接已弃、绝不回池（否则下一个借用者带着被改过的 statement_timeout）
    conn = _WConn(fail_on={"RESET": RuntimeError("reset blew up")})
    puts = []
    discarded = []
    monkeypatch.setattr(writeengine, "get_connection", lambda ds, target_db=None: conn)
    monkeypatch.setattr(writeengine, "put_connection",
                        lambda ds, c, target_db=None: puts.append(c))
    monkeypatch.setattr(writeengine, "discard_connection",
                        lambda ds, c, target_db=None: discarded.append(c))

    result = writeengine.execute_any_sql(
        "UPDATE t SET a=1", {"db_type": "postgres"}, "defdb", timeout_ms=600000)

    assert result["affected_rows"] == 0          # 写本身成功
    assert puts == []                            # 没有回池
    assert discarded == [conn]                   # 被弃


def test_execute_mysql_no_statement_timeout_touch(monkeypatch):
    conn = _WConn()
    puts = []
    _patch(monkeypatch, conn, puts)

    writeengine.execute_any_sql(
        "CREATE INDEX idx ON t(a)", {"db_type": "mysql"}, "defdb", timeout_ms=600000)

    assert not any("statement_timeout" in s.lower() for s in conn.executed)
    assert puts == [conn]


def test_execute_no_timeout_no_statement_timeout_touch(monkeypatch):
    conn = _WConn()
    puts = []
    _patch(monkeypatch, conn, puts)

    writeengine.execute_any_sql("UPDATE t SET a=1", {"db_type": "postgres"}, "defdb")

    assert not any("statement_timeout" in s.lower() for s in conn.executed)


# ---- execute 工具层接线：timeout → clamp → timeout_ms ----
async def _noop_confirm(*a, **k):
    return None


def _patch_execute_tool(monkeypatch, captured):
    from db_auto_router import tools

    def fake_exec(sql, ds, db, timeout_ms=None):
        captured["timeout_ms"] = timeout_ms
        return {"affected_rows": 1, "message": "ok", "dialect": "postgres"}

    monkeypatch.setattr(tools, "execute_any_sql", fake_exec)
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_confirm_write_if_needed", _noop_confirm)

    async def fake_resolve(db_code):
        # write_confirm=0（开发库）→ 免确认、免预估，测试只关心 timeout 接线
        return {"sources": {}, "db_code": "x", "ds": {"db_type": "postgres", "write_confirm": 0}}

    monkeypatch.setattr(tools.workspace, "_resolve_datasource", fake_resolve)
    return tools


def test_execute_tool_clamps_timeout_to_hard_cap(monkeypatch):
    import asyncio
    tools = _patch_execute_tool(monkeypatch, captured := {})

    r = asyncio.run(tools.execute("UPDATE t SET a=1", timeout=100000))

    assert captured["timeout_ms"] == 600000            # 夹到 QUERY_TIMEOUT_MAX=600s
    assert "warning" in r and "600" in r["warning"]    # 夹了必须说


def test_execute_tool_passes_timeout_through(monkeypatch):
    import asyncio
    tools = _patch_execute_tool(monkeypatch, captured := {})

    asyncio.run(tools.execute("UPDATE t SET a=1", timeout=120))

    assert captured["timeout_ms"] == 120000            # 120s → 120000ms，未夹


def test_execute_tool_no_timeout_passes_none(monkeypatch):
    import asyncio
    tools = _patch_execute_tool(monkeypatch, captured := {})

    asyncio.run(tools.execute("UPDATE t SET a=1"))

    assert captured["timeout_ms"] is None              # 不传 → 不设服务端超时
