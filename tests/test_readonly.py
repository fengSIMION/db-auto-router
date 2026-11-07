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


# 3. 瞬时错误不缓存：一次网络抖动不许永久毒化该层 -----------------------

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

def test_release_restores_pg_original_value(monkeypatch):
    monkeypatch.setattr(readonly, "_capability", {})
    monkeypatch.setattr(readonly, "_original", {})
    c = FakeConn()
    enable_read_only(c, "postgres", "k1")
    c.executed.clear()
    assert release_read_only(c, "postgres", "k1", ds={"db_type": "postgres"}) is True
    assert any("default_transaction_read_only" in s for s in c.executed)


def test_release_noop_for_mysql():
    c = FakeConn()
    enable_read_only(c, "mysql", _key())
    c.executed.clear()
    assert release_read_only(c, "mysql", _key(), ds={"db_type": "mysql"}) is True
    assert c.executed == []


def test_release_failure_discards_connection(monkeypatch):
    """恢复失败必须弃连接：连接带着 only-read 标记回池，会让后续 execute 写库失败。"""
    monkeypatch.setattr(readonly, "_capability", {})
    monkeypatch.setattr(readonly, "_original", {"k1": "off"})
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
