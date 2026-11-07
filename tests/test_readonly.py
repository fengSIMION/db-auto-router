# -*- coding: utf-8 -*-
"""只读通道第三层（数据库只读兜底）测试。

设计文档：docs/superpowers/specs/2026-11-07-read-guard-three-layers-design.md
第三层契约见该文档；本文件用假连接验证"按方言开关只读 + 能力缓存 + 恢复/弃连"，
不碰真数据库（真机验证由设计文档的验收清单负责）。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from db_auto_router import readonly
from db_auto_router.readonly import enable_read_only, release_read_only, readonly_field
from tests.dbfakes import FakeConn


def _key():
    return "mysql:10.0.0.1:3306:db:user:abc123"   # 形状同 pool._make_pool_key


# 1. 各方言发出的开关语句 ------------------------------------------------

def test_enable_mysql_issues_start_transaction_read_only():
    c = FakeConn()
    assert enable_read_only(c, "mysql", _key()) is True
    assert any("START TRANSACTION READ ONLY" in s for s in c.executed)


def test_enable_pg_issues_set_default():
    c = FakeConn()
    assert enable_read_only(c, "postgres", _key()) is True
    assert any("default_transaction_read_only" in s for s in c.executed)


@pytest.mark.parametrize("d", ["oracle", "dm"])
def test_enable_oracle_dm_issues_set_transaction(d):
    c = FakeConn()
    assert enable_read_only(c, d, _key()) is True
    assert any("SET TRANSACTION READ ONLY" in s for s in c.executed)


def test_unknown_dialect_returns_false_without_executing():
    c = FakeConn()
    assert enable_read_only(c, "sqlserver", _key()) is False
    assert c.executed == []


# 2. 能力缓存：只有"明确拒绝"才缓存，且按 pool_key 隔离 -----------------

def test_explicit_rejection_cached_per_pool_key(monkeypatch):
    monkeypatch.setattr(readonly, "_capability", {})
    boom = PermissionError("READ ONLY transaction not supported")
    c = FakeConn(fail_on={"READ ONLY": boom})
    assert enable_read_only(c, "mysql", "k1") is False
    c2 = FakeConn(fail_on={"READ ONLY": boom})
    assert enable_read_only(c2, "mysql", "k1") is False
    assert c2.executed == []                       # 第二次不再探测（缓存命中）
    c3 = FakeConn(fail_on={"READ ONLY": boom})
    assert enable_read_only(c3, "mysql", "k2") is False
    assert c3.executed != []                       # 换 pool_key 重新探测（不连坐）


# 3. 瞬时/未识别故障绝不缓存；只有"明确被告知不支持"才缓存 ---------------
#
# 原实现用"不是瞬态就缓存"的默认方向，被评审抓出漏洞：PG 断开连接的消息
# （server closed the connection unexpectedly 等）不在瞬态标记表里，会被
# 误判成"库不支持只读"并永久缓存——MCP 进程活数天，第三层静默关闭一整周。
# 改为反向默认：只有**正面识别**为永久拒绝才缓存，其余一律重新探测。

def test_syntax_rejection_is_cached(monkeypatch):
    monkeypatch.setattr(readonly, "_capability", {})
    boom = Exception("You have an error in your SQL syntax near 'START TRANSACTION READ ONLY'")
    c = FakeConn(fail_on={"READ ONLY": boom})
    assert enable_read_only(c, "mysql", "k1") is False
    c2 = FakeConn(fail_on={"READ ONLY": boom})
    assert enable_read_only(c2, "mysql", "k1") is False
    assert c2.executed == []                       # 明确不支持 → 缓存，不再探测


def test_unrecognized_connection_drop_not_cached(monkeypatch):
    monkeypatch.setattr(readonly, "_capability", {})
    c = FakeConn(fail_on={"READ ONLY":
                          ConnectionError("server closed the connection unexpectedly")})
    assert enable_read_only(c, "mysql", "k1") is False
    c2 = FakeConn()
    assert enable_read_only(c2, "mysql", "k1") is True    # 未缓存 → 重新探测并生效


def test_transient_error_not_cached(monkeypatch):
    monkeypatch.setattr(readonly, "_capability", {})
    c = FakeConn(fail_on={"READ ONLY":
                          ConnectionError("Lost connection to MySQL server during query")})
    assert enable_read_only(c, "mysql", "k1") is False
    c2 = FakeConn()
    assert enable_read_only(c2, "mysql", "k1") is True    # 恢复了就重新探测并生效


# 4. 总开关 --------------------------------------------------------------

def test_guard_off(monkeypatch):
    monkeypatch.setattr(readonly, "READONLY_DB_GUARD", "off")
    c = FakeConn()
    assert enable_read_only(c, "mysql", _key()) is False
    assert c.executed == []


# 5. 恢复：PG 恢复原值；MySQL 无需恢复；恢复失败弃连接 -------------------

def test_release_resets_pg_setting_to_session_default(monkeypatch):
    """C1 回归：恢复必须用 RESET（回到会话默认值），不许把"读到的原值"写回去。

    原值若在 SET 之后读，current_setting 返回的就是 on——恢复语句会把自己
    刚打开的只读状态原样写回去，连接带着 only-read 回池，后续 execute 写库失败。
    断言精确到整条语句，含糊的"包含 default_transaction_read_only"抓不到这个 bug。
    """
    monkeypatch.setattr(readonly, "_capability", {})
    c = FakeConn()
    enable_read_only(c, "postgres", "k1")
    c.executed.clear()
    assert release_read_only(c, "postgres", "k1", ds={"db_type": "postgres"}) is True
    assert c.executed == ["RESET default_transaction_read_only"]


def test_release_noop_for_mysql():
    c = FakeConn()
    enable_read_only(c, "mysql", _key())
    c.executed.clear()
    assert release_read_only(c, "mysql", _key(), ds={"db_type": "mysql"}) is True
    assert c.executed == []


@pytest.mark.parametrize("d", ["oracle", "dm"])
def test_oracle_dm_wrap_query_in_readonly_transaction(d):
    """Oracle/达梦：autocommit 下 SET TRANSACTION READ ONLY 会被驱动立刻提交，
    只读标记随那个只含 SET 的事务一起失效——必须先把 autocommit 关掉，让 SET
    与查询落在同一个事务里；释放时回滚并恢复 autocommit。"""
    c = FakeConn()
    assert enable_read_only(c, d, _key()) is True
    assert c.autocommit is False                      # 已关，SET 才会留在事务里
    assert any("SET TRANSACTION READ ONLY" in s for s in c.executed)
    c.executed.clear()
    assert release_read_only(c, d, _key(), ds={"db_type": d}) is True
    assert "--rollback--" in c.executed               # 回滚，只读标记随之失效
    assert c.autocommit is True                       # 恢复，连接才能回池


def test_release_failure_discards_connection(monkeypatch):
    """恢复失败必须弃连接：连接带着 only-read 标记回池，会让后续 execute 写库失败。"""
    monkeypatch.setattr(readonly, "_capability", {})
    c = FakeConn()
    enable_read_only(c, "postgres", "k1")
    c.fail_on["default_transaction_read_only"] = ConnectionError("connection reset")
    assert release_read_only(c, "postgres", "k1", ds={"db_type": "postgres"}) is False
    assert c.closed is True


# 6. 返回值契约 ----------------------------------------------------------

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
