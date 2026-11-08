# -*- coding: utf-8 -*-
"""引擎侧的 report_progress 接线：readengine / export_csv 按约定调 progress_cb。

约定：引擎拿到 progress_cb(done, total=None, final=False)，在批次循环里调
progress_cb(已处理, total)，收尾调一次 final=True。节流在 tools.py 的 reporter
里做，引擎只管如实上报（测试直接传记录假件，不起真 loop）。

分母规则（设计定）：
- query 带用户手写 LIMIT → total = min(LIMIT, 行数硬顶)，确定性；
- query 无 LIMIT（引擎自动注入上限）→ total = None，心跳；
- export → 先 COUNT 拿精确总数，COUNT 失败退化成心跳。
"""
from db_auto_router import readengine, csvio
from tests.dbfakes import FakeConn, FakeCursor


# ---- readengine：只读查询 ----
class _QueryCursor(FakeCursor):
    def __init__(self, conn):
        super().__init__(conn)
        self.description = [("id",)]
        self._rows = [[(1,), (2,), (3,)]]   # 一批 3 行，随后取空


class _QueryConn(FakeConn):
    def cursor(self):
        return _QueryCursor(self)


def _patch_query_conn(monkeypatch):
    conn = _QueryConn()
    monkeypatch.setattr(readengine, "get_connection", lambda ds, target_db=None: conn)
    monkeypatch.setattr(readengine, "put_connection", lambda ds, c, target_db=None: None)
    return conn


def _recorder():
    calls = []

    def cb(done, total=None, final=False):
        calls.append((done, total, final))

    return calls, cb


def test_query_with_limit_reports_determinate_progress(monkeypatch):
    _patch_query_conn(monkeypatch)
    calls, cb = _recorder()

    result = readengine.execute_read_only_query(
        "SELECT id FROM t LIMIT 5", {"db_type": "postgres"}, "defdb", progress_cb=cb)

    assert result["total_rows"] == 3
    # 分母 = min(用户 LIMIT 5, 行数硬顶) = 5，全程一致
    assert all(t == 5 for (_, t, _) in calls), calls
    assert calls[-1] == (3, 5, True)                 # 收尾 final
    assert any(not f for (_, _, f) in calls)         # 循环里至少一次非 final


def test_query_without_limit_reports_heartbeat(monkeypatch):
    _patch_query_conn(monkeypatch)
    calls, cb = _recorder()

    result = readengine.execute_read_only_query(
        "SELECT id FROM t", {"db_type": "postgres"}, "defdb", progress_cb=cb)

    assert result["total_rows"] == 3
    # 无 LIMIT → 心跳，total 全程 None
    assert all(t is None for (_, t, _) in calls), calls
    assert calls[-1][2] is True                      # 仍有一次 final


def test_query_no_progress_cb_is_fine(monkeypatch):
    """不传 progress_cb（默认 None）时行为不变——纯附加，不能因为没进度器就崩。"""
    _patch_query_conn(monkeypatch)
    result = readengine.execute_read_only_query(
        "SELECT id FROM t", {"db_type": "postgres"}, "defdb")
    assert result["total_rows"] == 3


# ---- export_csv：流式导出 ----
class _ExportCursor(FakeCursor):
    """按 SQL 分派：COUNT 查询返总数(7)，主查询返 3 行数据。"""

    def execute(self, sql, params=None):
        super().execute(sql, params)
        if sql.lower().lstrip().startswith("select count("):
            self._is_count = True
            self._count_value = 7
            self.description = [("c",)]
            self._rows = []
        else:
            self._is_count = False
            self.description = [("id",), ("name",)]
            self._rows = [[(1, "a"), (2, "b"), (3, "c")]]

    def fetchone(self):
        if getattr(self, "_is_count", False):
            self._is_count = False
            return (self._count_value,)
        return None


class _ExportConn(FakeConn):
    def __init__(self, fail_on=None):
        super().__init__(fail_on)
        self.cursors = []

    def cursor(self):
        c = _ExportCursor(self)
        self.cursors.append(c)
        return c


def _patch_export_env(monkeypatch, conn):
    monkeypatch.setattr(csvio, "get_connection", lambda ds, target_db=None: conn)
    monkeypatch.setattr(csvio, "put_connection", lambda ds, c, target_db=None: None)
    monkeypatch.setattr(csvio, "enable_read_only", lambda *a, **k: False)
    monkeypatch.setattr(csvio, "release_read_only", lambda *a, **k: True)
    # 隔离：只读守卫在 test_astguard/test_guard_wiring 已覆盖，这里专注进度
    monkeypatch.setattr(csvio, "validate_read_statement_ast", lambda *a, **k: None)


def test_export_reports_determinate_progress(monkeypatch, tmp_path):
    conn = _ExportConn()
    _patch_export_env(monkeypatch, conn)
    calls, cb = _recorder()
    out = tmp_path / "out.csv"

    result = csvio.export_csv_impl(
        "SELECT id, name FROM t", str(out), {"db_type": "postgres"}, "defdb", progress_cb=cb)

    assert result["rows_exported"] == 3
    # 先 COUNT 得到总数 7 当分母，全程一致
    assert all(t == 7 for (_, t, _) in calls), calls
    assert calls[-1] == (3, 7, True)                 # final
    # 确实先跑了一条 COUNT（连接视角看得到）
    assert any("count(" in s.lower() for s in conn.executed)


def test_export_falls_back_to_heartbeat_when_count_fails(monkeypatch, tmp_path):
    # COUNT 包裹这条方言不认（如 Oracle 禁 WITH 子查询）→ 退化成心跳，不卡导出
    conn = _ExportConn(fail_on={"COUNT": RuntimeError("count wrap unsupported")})
    _patch_export_env(monkeypatch, conn)
    calls, cb = _recorder()
    out = tmp_path / "out.csv"

    result = csvio.export_csv_impl(
        "SELECT id, name FROM t", str(out), {"db_type": "postgres"}, "defdb", progress_cb=cb)

    assert result["rows_exported"] == 3             # 导出照常完成
    assert all(t is None for (_, t, _) in calls), calls  # 心跳：无分母
    assert calls[-1][2] is True


def test_export_without_progress_cb_skips_count(monkeypatch, tmp_path):
    """不传 progress_cb 时绝不跑 COUNT——不能为用不到的进度白扫一遍大表。"""
    conn = _ExportConn()
    _patch_export_env(monkeypatch, conn)
    out = tmp_path / "out.csv"

    result = csvio.export_csv_impl(
        "SELECT id, name FROM t", str(out), {"db_type": "postgres"}, "defdb")

    assert result["rows_exported"] == 3
    assert not any("count(" in s.lower() for s in conn.executed)
