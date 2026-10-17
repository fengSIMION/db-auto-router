# -*- coding: utf-8 -*-
"""审计日志：每条工具调用落一行 JSON（时间/工具/数据源/SQL/耗时/结果），事后可追溯
——原 134–163 行"""

__author__ = "fengkeju"
import json
import time
import threading
from pathlib import Path

from .config import AUDIT_ENABLED, AUDIT_LOG_DIR
from .logcfg import logger

_audit_lock = threading.Lock()


def audit_log(tool: str, db_code: str, sql: str, *, status: str, detail: str = "", execution_time_ms: float = None, rows: int = None, txn_id: str = None) -> None:
    """写审计日志（JSONL，按天分文件）。失败不抛异常，只记 stderr，不影响主流程"""
    if not AUDIT_ENABLED:
        return
    try:
        audit_dir = Path(AUDIT_LOG_DIR)
        audit_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tool": tool,
            "db_code": db_code or "",
            "sql": (sql or "").strip()[:4000],
            "status": status,
            "detail": detail[:500],
            "execution_time_ms": execution_time_ms,
            "rows": rows,
        }
        if txn_id:
            entry["txn_id"] = txn_id
        fname = audit_dir / f"audit-{time.strftime('%Y%m%d')}.jsonl"
        with _audit_lock:
            with open(fname, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning(f"审计日志写入失败（不影响主流程）: {e}")
