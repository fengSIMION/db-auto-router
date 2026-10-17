# -*- coding: utf-8 -*-
"""连接池管理：上限控制 + 定期清理 + 进程退出释放 + 全方言健康检查
——原 359–605 行。v4.x 清理：方言分发统一委托给 directconn._build_connection，
自身只关心池的存取/淘汰/探活逻辑。"""

__author__ = "fengkeju"
import time
import atexit
import hashlib
import threading
from contextlib import contextmanager
from typing import Dict

from .config import MAX_POOL_SIZE, POOL_IDLE_TIME
from .directconn import _build_connection
from .errutil import safe_close
from .logcfg import logger


# ------------------------------------------------------------------------------
# 连接池状态（线程安全 + 容量上限 + 空闲回收）
# ------------------------------------------------------------------------------
# 语义：_pool 只存"空闲"连接。get_connection 是真正的借出（摘出后不放回），
# 调用方必须用 put_connection 归还（或用 checkout() 上下文管理器，忘不了）。
#
# 此前 get_connection 把连接放回池的同时又 return 给调用方，条纹锁在 return 时
# 即释放——只串行化了"取连接"，没串行化"用连接"。实测两个并发调用会拿到同一条
# 物理连接：mysql-connector 的 C 扩展非线程安全，同连接并发游标会交错协议帧；
# import_csv 关掉共享连接的 autocommit 后，另一线程的 execute_any_sql 在同一条
# 连接上 commit()，会把前者未提交的半成品批次提交掉。且借出的连接仍在 _pool 里，
# LRU 淘汰与空闲清理都会 close() 掉别人正在飞的连接（实测复现）。
_pool = {}           # {pool_key: connection} —— 仅空闲连接
_last_used = {}      # {pool_key: timestamp}
_in_use = {}         # {pool_key: int} 借出计数（容量 = 空闲 + 借出）
_checked_out = []    # 借出中的连接对象，供进程退出时兜底关闭
_lock = threading.Lock()
_cleanup_thread = None
_cleanup_running = False

# 按 pool_key 条纹化的锁：同一 key 的"借出/验活/建连/归还"整体串行。
# 固定数量、内存有界；哈希碰撞仅导致不必要的串行，不影响正确性。
_KEY_LOCK_STRIPES = 64
_key_locks = [threading.Lock() for _ in range(_KEY_LOCK_STRIPES)]


def cleanup_connection_pool():
    """进程退出时释放所有连接（空闲 + 仍被借出的）"""
    global _cleanup_running
    logger.info("进程退出，释放连接池...")
    _cleanup_running = False
    # 先摘出再关：close() 是 IO，不应在持锁状态下做
    with _lock:
        conns = list(_pool.items())
        _pool.clear()
        _last_used.clear()
        in_use = list(_checked_out)
        _checked_out.clear()
        _in_use.clear()
    for pool_key, conn in conns:
        try:
            conn.close()
            logger.debug(f"关闭连接: {pool_key}")
        except Exception as e:
            logger.warning(f"关闭连接失败 {pool_key}: {e}")
    # 借出中的连接：atexit 时调用方大抵已经不在了，这里兜底关掉，
    # 否则行锁/表锁要等数据库侧 wait_timeout 或 TCP 复位才释放
    for conn in in_use:
        try:
            conn.close()
        except Exception:
            pass


def start_pool_cleanup_thread():
    """启动连接池定期清理线程（幂等）"""
    global _cleanup_thread, _cleanup_running
    if _cleanup_thread is not None:
        return
    _cleanup_running = True

    def cleanup_loop():
        consecutive_failures = 0
        while _cleanup_running:
            try:
                time.sleep(60)
                if not _cleanup_running:
                    break

                now = time.time()
                # 持锁只做摘出；close() 移到锁外，避免慢关闭堵住整个池
                with _lock:
                    expired = [
                        (k, _pool.pop(k, None)) for k, ts in list(_last_used.items())
                        if now - ts > POOL_IDLE_TIME
                    ]
                    for k, _ in expired:
                        _last_used.pop(k, None)
                for pool_key, conn in expired:
                    if conn is None:
                        continue
                    try:
                        conn.close()
                        logger.info(f"清理过期连接: {pool_key}")
                    except Exception as close_err:
                        logger.warning(f"关闭过期连接失败: {pool_key}: {close_err}")
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                logger.error(f"连接池清理异常 (第{consecutive_failures}次): {e}", exc_info=True)
                if consecutive_failures >= 5:
                    logger.critical("连接池清理连续失败5次，清理线程退出")
                    break

    _cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    _cleanup_thread.start()
    logger.info(f"连接池清理线程已启动，最大连接数: {MAX_POOL_SIZE}, 空闲超时: {POOL_IDLE_TIME}s")


atexit.register(cleanup_connection_pool)


def _is_conn_healthy(conn, typ: str) -> bool:
    """检测池内连接是否仍然有效（各驱动用各自的原生健康检查）。
    目的：数据库端 wait_timeout 掐掉连接后，避免把死连接交给调用方导致执行阶段报错。"""
    try:
        if typ == "mysql":
            conn.ping(reconnect=True)
            return True
        if typ in ("postgres", "gaussdb"):
            if getattr(conn, "closed", 0):
                return False
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            return True
        if typ == "oracle":
            return bool(conn.is_healthy())
        if typ == "dm":
            if hasattr(conn, "alive"):
                return bool(conn.alive())
            conn.ping()
            return True
    except Exception as e:
        logger.debug(f"连接健康检查失败 ({typ}): {e}")
        return False
    return True


def _make_pool_key(ds: Dict[str, str], target_db: str = None) -> str:
    """生成连接池 key。

    凭证指纹必须纳入 key：用户在配置库里改了密码，url/user/db 都没变，
    不纳指纹会命中池中带着旧凭证的连接，表现为"密码已经改了却还是连不上"。
    取 sha256 前 12 位——足够区分，也不把密码本身带进日志。
    """
    typ = ds.get("db_type", "").lower().strip()
    url = ds.get("conn_url", "")
    db_name = target_db if target_db else ds.get("db_code", "")
    user = ds.get("db_user", "")
    pwd_fp = hashlib.sha256((ds.get("db_password") or "").encode("utf-8")).hexdigest()[:12]
    return f"{typ}:{url}:{db_name}:{user}:{pwd_fp}"


def _evict_lru_locked() -> None:
    """池满时踢掉最久未使用的连接（调用方必须已持有 _lock）。

    只从 _pool（空闲连接）里挑选：借出中的连接不在这里，所以正在被使用的
    连接永远不会被淘汰掉。某个 key 可能已被其它线程摘出正在建连、尚未入池，
    选中它会让 _pool.pop 拿到 None，池容量判定随之失真。
    """
    candidates = [k for k in _pool if k in _last_used]
    if not candidates:
        return
    oldest_key = min(candidates, key=lambda k: _last_used[k])
    old_conn = _pool.pop(oldest_key, None)
    _last_used.pop(oldest_key, None)
    if old_conn is not None:
        try:
            old_conn.close()
            logger.info(f"连接池达到上限({MAX_POOL_SIZE})，清理最旧连接: {oldest_key}")
        except Exception:
            pass


def get_connection(ds: Dict[str, str], target_db: str = None):
    """从池中借出或新建一条数据库连接。

    **借出语义**：摘出后不放回池。调用方必须用 put_connection(ds, conn, target_db)
    归还，或直接用 checkout() 上下文管理器。池里只放空闲连接，因此 LRU 淘汰与
    空闲清理都只会碰到没人用的连接。

    并发设计（_lock 全程只做 O(1) 字典操作，绝不做网络 IO）：
    - 条纹锁按 key 哈希取模：同一 key 的借出/归还被串行化，不同 key 仍可并行。
      固定 64 把、内存有界，哈希碰撞只会造成不必要的串行，不影响正确性。
    - 健康检查（ping / SELECT 1）在锁外执行：PG 的健康检查受 statement_timeout
      约束，最坏能卡 30 秒，持锁做会把整个池（含其它数据源）一起堵死。
    - 建连接同样在条纹锁内（同一 key 的冷启动建连会串行，换来看得见的正确性：
      容量判定不会因并发建连而超限）。
    - 容量 = 空闲 + 借出，两者一起计数，保证在册连接不超 MAX_POOL_SIZE。
    """
    typ = ds.get("db_type", "").lower().strip()
    pool_key = _make_pool_key(ds, target_db)

    with _key_locks[hash(pool_key) % _KEY_LOCK_STRIPES]:
        # --- 快路径：摘出 → 锁外验活 → 健康则直接交给调用方（不放回）---
        with _lock:
            conn = _pool.pop(pool_key, None)
            last_used = _last_used.pop(pool_key, 0)
            if conn is not None:
                _in_use[pool_key] = _in_use.get(pool_key, 0) + 1
                _checked_out.append(conn)

        if conn is not None:
            healthy = False
            if time.time() - last_used < POOL_IDLE_TIME:
                try:
                    healthy = _is_conn_healthy(conn, typ)
                except Exception as e:
                    logger.warning(f"连接健康检查异常({typ}): {e}")
            if healthy:
                logger.debug(f"使用连接池中的连接: {pool_key}")
                return conn
            logger.info(f"连接失效或空闲超时，关闭重建: {pool_key}")
            safe_close(conn)
            with _lock:
                _release_slot_locked(pool_key, conn)

        # --- 慢路径：容量淘汰（持锁，无 IO）→ 建连 ---
        with _lock:
            if len(_pool) + sum(_in_use.values()) >= MAX_POOL_SIZE:
                _evict_lru_locked()
            _in_use[pool_key] = _in_use.get(pool_key, 0) + 1

        logger.debug(f"创建新连接: {pool_key}")
        try:
            conn = _build_connection(ds, target_db, autocommit=True)
        except Exception:
            # 建连失败必须把借出计数还回去，否则容量被永久占住
            with _lock:
                _release_slot_locked(pool_key, None)
            raise
        with _lock:
            _checked_out.append(conn)
        return conn


def _release_slot_locked(pool_key: str, conn) -> None:
    """归还一个借出名额（调用方必须已持有 _lock）。conn 非 None 时同步注销登记。"""
    remaining = _in_use.get(pool_key, 0) - 1
    if remaining > 0:
        _in_use[pool_key] = remaining
    else:
        _in_use.pop(pool_key, None)
    if conn is not None:
        for i, c in enumerate(_checked_out):
            if c is conn:
                _checked_out.pop(i)
                break


def put_connection(ds: Dict[str, str], conn, target_db: str = None) -> None:
    """归还一条借出的连接。

    - 先 rollback：清掉调用方可能留下的未提交事务与行锁。没有这一步，一条在
      半途异常的连接会把隐式事务（PG 下是 idle in transaction）带回池里，
      下一个借用者继承它，扛快照、堵 VACUUM，直到空闲超时被关掉。
    - 同一 key 只保留一条空闲连接：并发借出 N 条同 key 连接后依次归还时，
      直接覆盖会让前 N-1 条被静默泄漏（既不复用也没人关）。此时关掉新回来的
      这条、保留已在池里的那条——两者健康度等价，借用时的健康检查会兜底。
    - 池已满时同样直接关闭，而不是挤掉别人。
    - 连接已坏也无所谓：下一个借用者的健康检查会验出来并重建。
    """
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass
    pool_key = _make_pool_key(ds, target_db)
    close_it = False
    with _lock:
        _release_slot_locked(pool_key, conn)
        if pool_key in _pool or len(_pool) >= MAX_POOL_SIZE:
            close_it = True
        else:
            _pool[pool_key] = conn
            _last_used[pool_key] = time.time()
    if close_it:
        logger.debug(f"该 key 已有空闲连接或池已满，归还时直接关闭: {pool_key}")
        safe_close(conn)


@contextmanager
def checkout(ds: Dict[str, str], target_db: str = None):
    """借出连接的上下文管理器：退出时自动归还，忘记归还的概率为零。

        with checkout(ds, db) as conn:
            with safe_cursor(conn) as cur:
                cur.execute(sql)
    """
    conn = get_connection(ds, target_db)
    try:
        yield conn
    finally:
        put_connection(ds, conn, target_db)
