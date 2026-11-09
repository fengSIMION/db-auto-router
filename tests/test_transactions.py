# -*- coding: utf-8 -*-
"""事务提交、回滚与事务模式建立的失败语义。"""
import threading
import time

import pytest

import db_auto_router.transactions as tx
from db_auto_router.errors import ToolFailure


class FinishConn:
    def __init__(self, *, commit_error=None, rollback_error=None):
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.closed = False

    def commit(self):
        if self.commit_error:
            raise self.commit_error

    def rollback(self):
        if self.rollback_error:
            raise self.rollback_error

    def close(self):
        self.closed = True


def _register(txn_id, conn):
    tx._transactions[txn_id] = {
        "conn": conn,
        "ds": {"db_type": "mysql"},
        "db_code": "demo",
        "target_db": None,
        "created_at": time.monotonic(),
        "last_used": time.monotonic(),
        "stmt_count": 2,
        "idle_timeout": 600,
        "exec_lock": threading.Lock(),
    }


@pytest.fixture(autouse=True)
def clear_transactions():
    tx._transactions.clear()
    yield
    tx._transactions.clear()


def test_begin_rejects_when_autocommit_cannot_be_disabled(monkeypatch):
    class Conn:
        closed = False

        @property
        def autocommit(self):
            return True

        @autocommit.setter
        def autocommit(self, _value):
            raise RuntimeError("driver refused")

        def close(self):
            self.closed = True

    conn = Conn()
    monkeypatch.setattr(tx, "_open_direct_connection", lambda *_a, **_k: conn)

    with pytest.raises(ToolFailure, match="无法关闭自动提交"):
        tx.begin_transaction_impl({"db_type": "mysql"}, "demo")

    assert conn.closed is True
    assert tx._transactions == {}


def test_begin_rejects_when_driver_silently_keeps_autocommit_enabled(monkeypatch):
    class Conn:
        closed = False
        autocommit = True

        def __setattr__(self, name, value):
            if name == "autocommit" and value is False:
                return
            object.__setattr__(self, name, value)

        def close(self):
            self.closed = True

    conn = Conn()
    monkeypatch.setattr(tx, "_open_direct_connection", lambda *_a, **_k: conn)

    with pytest.raises(ToolFailure, match="自动提交状态仍为开启"):
        tx.begin_transaction_impl({"db_type": "mysql"}, "demo")

    assert conn.closed is True
    assert tx._transactions == {}


def test_commit_failure_reports_unknown_outcome_and_closes_connection():
    conn = FinishConn(commit_error=ConnectionError("connection lost"))
    _register("txn-commit", conn)

    with pytest.raises(RuntimeError, match="提交结果未知.*禁止盲目重试"):
        tx._end_transaction("txn-commit", True)

    assert conn.closed is True
    assert "txn-commit" not in tx._transactions


def test_rollback_failure_does_not_report_success():
    conn = FinishConn(rollback_error=ConnectionError("connection lost"))
    _register("txn-rollback", conn)

    with pytest.raises(RuntimeError, match="回滚结果未知"):
        tx._end_transaction("txn-rollback", False)

    assert conn.closed is True
    assert "txn-rollback" not in tx._transactions


def test_statement_failure_preserves_sql_error_when_rollback_also_fails():
    class Cursor:
        description = None

        def execute(self, _sql):
            raise ValueError("bad sql")

        def close(self):
            pass

    conn = FinishConn(rollback_error=ConnectionError("rollback lost"))
    conn.cursor = lambda: Cursor()
    _register("txn-execute", conn)

    with pytest.raises(RuntimeError) as exc:
        tx.execute_in_transaction("txn-execute", "UPDATE demo SET x=1")

    message = str(exc.value)
    assert "bad sql" in message
    assert "回滚结果未知" in message
    assert conn.closed is True
    assert "txn-execute" not in tx._transactions
