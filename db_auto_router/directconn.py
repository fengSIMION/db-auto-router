# -*- coding: utf-8 -*-
"""直接连接与底层连接工厂：事务专用连接 / autocommit 设置 / 超时专用连接（均不入池）。
v4.x 清理：将原本分散在 pool.py / directconn.py 的多处方言分发逻辑收束到 _build_connection，
后续增删方言只需改这一处。
v4.1：mysql.connector 改为分支内懒加载——配置库已支持 SQLite 模式，不再保证
MySQL 驱动必装；未装该包的用户（纯 PG/达梦/Oracle 场景）不能在 import 阶段被卡死。"""

__author__ = "fengkeju"
import os
import socket
import threading
import time
from typing import Dict, Optional

from .config import CONNECT_TIMEOUT, READ_TIMEOUT
from .drivers import get_driver
from .errors import ToolFailure
from .logcfg import logger
from .util import parse_mysql_url


def _dm_host_port(url: str):
    """从达梦连接串里取 (host, port)；认不出来返回 None（调用方跳过网络探测）。

    只认 "host:port" 这一种形态——README 里配置示例就是它。带 "/" 或空格的完整
    DSN 不猜：猜错的 host 会让后面的 TCP 探测给出一个笃定的错误结论，
    比不探测更糟。
    """
    text = (url or "").strip()
    if not text or "/" in text or " " in text:
        return None
    host, sep, port_text = text.partition(":")
    if not sep or not host:
        return None
    try:
        return host, int(port_text)
    except ValueError:
        return None


# TCP 探测结果短缓存：(host, port) -> (monotonic 时间戳, 可达?, 错误文本)。
# 只在达梦连接失败路径上写、只被它读。不值得加锁：worst case 是并发下多探一次，
# 而加锁会把探测（最坏 5 秒）变成全局串行点。
_DM_PROBE_CACHE: Dict[tuple, tuple] = {}
_DM_PROBE_TTL = 30.0


def _dm_probe_tcp(host: str, port: int, timeout: float):
    """探测达梦服务器 TCP 是否可达，返回 (可达?, 错误文本)。30 秒内复用上次结论。"""
    now = time.monotonic()
    key = (host, port)
    cached = _DM_PROBE_CACHE.get(key)
    if cached is not None and now - cached[0] < _DM_PROBE_TTL:
        return cached[1], cached[2]
    try:
        with socket.create_connection((host, port), timeout=timeout):
            result = (True, "")
    except Exception as e:
        result = (False, f"{type(e).__name__}: {e}")
    _DM_PROBE_CACHE[key] = (now, result[0], result[1])
    return result


def explain_dm_connect_error(exc: BaseException, url: str) -> str:
    """把 dmPython 的不透明连接失败翻译成能排查的说明。

    为什么需要它：dmPython 的 C 层把一切连接失败都抛成同一句
    `SystemError: <class 'dmPython.Connection'> returned a result with an exception set`
    ——超时、拒登、实例名写错、连接数满，全是这一句。用户看到它等于什么都没看到，
    既不知道错在哪，也不知道下一步该查什么。mysql/oracle/PG 的驱动都会给出
    "Can't connect to ... (10060)" 这样的原文，只有达梦不会。

    做法是用一次原生 TCP 探测把两种完全不同的病因分开：
    - TCP 建不起来 → 网络层就没通（VPN/内网、防火墙、服务没起、地址端口写错）
    - TCP 能建起来 → 网络通，是达梦服务本身拒了这次连接（账号密码、连接数上限、实例名）
    探测只在失败路径上做，超时取 min(CONNECT_TIMEOUT, 5)——不能比连接尝试本身
    还慢，那会把每次失败的等待时间翻倍。结果按 (host, port) 缓存 30 秒：说明文本里
    含 "timed out"，会被 errutil.is_transient_error 判为瞬时错误，上层因此重试；
    不缓存的话同一次故障要付 3 次探测（readengine 的 MAX_RETRY_COUNT）。

    原始报错始终附在最后：不丢信息，也不用它当结论。
    """
    original = f"{type(exc).__name__}: {exc}"
    target = _dm_host_port(url)
    if target is None:
        return (f"达梦连接失败：{original}\n"
                f"（dmPython 对连接失败只回报这一句，无法进一步定位；"
                f"连接地址 {url!r} 不是 host:port 形态，已跳过网络探测）")

    host, port = target
    probe_timeout = max(1, min(CONNECT_TIMEOUT, 5))
    reachable, net_err = _dm_probe_tcp(host, port, probe_timeout)

    if not reachable:
        return (f"达梦连接失败：{host}:{port} 网络不可达"
                f"（TCP 连接 {probe_timeout} 秒内未建立）\n"
                f"  · 探测结果：{net_err}\n"
                f"  · 常见原因：VPN/内网未连接、防火墙拦截、达梦服务未启动、地址或端口写错\n"
                f"  · dmPython 原始报错（无信息量，仅存档）：{original}")
    return (f"达梦连接失败：{host}:{port} 网络可达，但达梦服务拒绝了这次连接\n"
            f"  · 常见原因：账号/密码错误、连接数已达上限、实例名或 SERVICE_NAME 不对\n"
            f"  · dmPython 原始报错（无信息量，仅存档）：{original}")


def _build_connection(
    ds: Dict[str, str],
    target_db: Optional[str] = None,
    *,
    autocommit: bool = True,
    statement_timeout: Optional[int] = None,
):
    """各方言的统一连接构造器。所有上层入口（连接池、事务直连、超时覆盖）最终都汇聚到这里。

    参数：
    - ds: 数据源配置字典
    - target_db: 可选的目标库名（跨库查询场景）
    - autocommit: 是否启用自动提交；事务模式设为 False
    - statement_timeout: 语句级超时秒数；None 表示使用默认 READ_TIMEOUT；
      dm 驱动不支持此参数会被忽略并记 warning
    """
    typ = ds.get("db_type", "").lower().strip()
    url = ds.get("conn_url", "")
    db_name = target_db if target_db else ds.get("db_code", "")
    user = ds.get("db_user", "")
    pwd = ds.get("db_password", "")

    effective_stmt_timeout = statement_timeout if statement_timeout is not None else READ_TIMEOUT

    if typ == "mysql":
        params = parse_mysql_url(url)
        driver = get_driver("mysql")  # v4.1 懒加载（sqlite 配置库模式下可能未装）
        kwargs = dict(
            host=params["host"], port=params["port"], user=user, password=pwd,
            database=db_name, charset='utf8mb4',
            connect_timeout=CONNECT_TIMEOUT, read_timeout=effective_stmt_timeout,
            autocommit=autocommit,
        )
        return driver.connect(**kwargs)

    if typ == "dm":
        dm_python = get_driver("dm")
        # dmPython 无 login_timeout 参数，用守护线程包裹实现连接超时保护
        conn_holder = [None]
        conn_error = [None]

        def _dm_connect():
            try:
                conn_holder[0] = dm_python.connect(user=user, password=pwd, dsn=url)
            except Exception as e:
                conn_error[0] = e

        t = threading.Thread(target=_dm_connect, daemon=True)
        t.start()
        t.join(timeout=CONNECT_TIMEOUT)
        if t.is_alive():
            raise TimeoutError(f"达梦连接超时({CONNECT_TIMEOUT}s): {url}")
        if conn_error[0]:
            # 不能原样抛：dmPython 只给一句 "returned a result with an exception set"，
            # 用户看不出是网络不通还是密码错。翻译成可排查的说明再抛。
            raise ToolFailure(explain_dm_connect_error(conn_error[0], url))
        conn = conn_holder[0]

        dm_encoding = os.environ.get("MCP_DM_ENCODING", "UTF-8")
        if dm_encoding.upper() == "GB18030":
            conn.local_code = dm_python.PG_GB18030
        try:
            conn.autocommit = autocommit
        except Exception:
            pass
        if statement_timeout is not None:
            # 达梦驱动不提供语句级超时参数，仅做提示
            logger.warning(f"达梦驱动不支持语句级超时参数，timeout={statement_timeout}s 未生效")
        return conn

    if typ in ("postgres", "gaussdb"):
        # GaussDB 兼容 PostgreSQL 协议，复用 psycopg2
        driver = get_driver(typ)
        conn = driver.connect(
            dsn=url, user=user, password=pwd,
            connect_timeout=CONNECT_TIMEOUT,
            options=f"-c statement_timeout={effective_stmt_timeout * 1000}",
        )
        conn.autocommit = autocommit
        return conn

    if typ == "oracle":
        driver = get_driver("oracle")
        conn = driver.connect(
            user=user, password=pwd, dsn=url,
            tcp_connect_timeout=CONNECT_TIMEOUT,
        )
        try:
            conn.autocommit = autocommit
        except Exception:
            pass
        if statement_timeout is not None and statement_timeout != READ_TIMEOUT:
            try:
                conn.call_timeout = statement_timeout * 1000
            except Exception:
                pass
        return conn

    raise ValueError(f"不支持的数据库类型: {typ}")


def _set_conn_autocommit(conn, typ: str, value: bool) -> None:
    """各方言统一设置 autocommit（尽量兼容）。"""
    try:
        if typ == "mysql":
            conn.autocommit = value
        elif typ in ("postgres", "gaussdb"):
            conn.autocommit = value
        elif typ in ("oracle", "dm"):
            try:
                conn.autocommit = value
            except Exception:
                pass  # oracledb/dmPython 旧版本可能无此属性，退化为手动 commit
    except Exception as e:
        logger.warning(f"设置 autocommit={value} 失败（{typ}）: {e}")


def _open_direct_connection(ds: Dict[str, str], target_db: str = None):
    """绕过连接池直接创建一条新连接（事务专用连接，不参与池复用）。
    autocommit=False 是事务模式的刚需。"""
    return _build_connection(ds, target_db, autocommit=False)


def _open_direct_connection_with_timeout(ds: Dict[str, str], target_db: str = None, timeout: int = READ_TIMEOUT):
    """为超时覆盖场景创建专用连接（不入池，用完即关）。"""
    return _build_connection(ds, target_db, autocommit=True, statement_timeout=timeout)
