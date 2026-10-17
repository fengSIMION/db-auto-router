import sys, os, io, json, tempfile, threading, time, codecs
# 按本文件位置定位仓库根（tests/ 的上一级），换机器/换目录都不用改这里
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MCP_AUDIT_LOG_DIR"] = tempfile.mkdtemp(prefix="audit_")
import db_auto_router.tools as T
import db_auto_router.audit as A
import db_auto_router.transactions as TX
import db_auto_router.workspace as W
import db_auto_router.csvio as CV
ok = True
def chk(label, cond, extra=""):
    global ok
    if not cond: ok = False
    print("  %s %s %s" % ("✅" if cond else "❌", label, extra))

print("=== 1. 审计：ToolFailure 现在必须带 detail 与 db_code ===")
T._audit_error("query", "stu", "SELECT 1", Exception("未知数据源: nope"))
d = A.AUDIT_LOG_DIR
f = sorted(os.listdir(d))[-1]
line = json.loads(io.open(os.path.join(d, f), encoding="utf-8").read().strip())
chk("detail 非空", bool(line["detail"]), "→ %r" % line["detail"])
chk("db_code 记录实际数据源", line["db_code"] == "stu", "→ %r" % line["db_code"])
chk("status=error", line["status"] == "error")

class FakeTF(Exception):
    message = "用户拒绝了写操作确认"
T._audit_error("import_csv", "stu", "import a.csv -> t", FakeTF())
line2 = [json.loads(l) for l in io.open(os.path.join(d, f), encoding="utf-8")][-1]
chk("ToolFailure.message 被提取", line2["detail"] == "用户拒绝了写操作确认", "→ %r" % line2["detail"])

print()
print("=== 2. 事务：同一 txn_id 并发执行被串行化 ===")
TX._transactions.clear()
calls = {"n": 0, "max_concurrent": 0, "cur": 0}
lock = threading.Lock()
class FakeCursor:
    description = None
    rowcount = 1
    def execute(self, s):
        with lock:
            calls["cur"] += 1
            calls["max_concurrent"] = max(calls["max_concurrent"], calls["cur"])
            calls["n"] += 1
        time.sleep(0.05)   # 模拟 IO，放大竞态窗口
        with lock: calls["cur"] -= 1
    def close(self): pass
class FakeConn:
    def cursor(self): return FakeCursor()
    def rollback(self): pass
    def commit(self): pass
    def close(self): pass
TX._transactions["txn-test"] = {
    "conn": FakeConn(), "ds": {"db_type": "mysql"}, "db_code": "stu",
    "target_db": None, "created_at": time.time(), "last_used": time.time(),
    "stmt_count": 0, "idle_timeout": 600, "exec_lock": threading.Lock(),
}
ths = [threading.Thread(target=lambda: TX.execute_in_transaction("txn-test", "UPDATE t SET x=1")) for _ in range(6)]
for t in ths: t.start()
for t in ths: t.join()
chk("6 个并发全部成功", calls["n"] == 6, "→ 执行 %d 次" % calls["n"])
chk("最大并发度 = 1（未串台）", calls["max_concurrent"] == 1, "→ max_concurrent=%d" % calls["max_concurrent"])

print()
print("=== 3. 握手：roots 失败不永久判死会话 ===")
W._workspace_primed = False; W._workspace_attempts = 0; W._workspace_dir = None
import asyncio
class BadCtx:
    class session:
        @staticmethod
        async def list_roots(): raise TimeoutError("roots timeout")
for i in range(2):
    asyncio.run(W._discover_workspace_dir(BadCtx()))
chk("2 次失败后仍未 primed（还会重试）", W._workspace_primed is False, "→ attempts=%d" % W._workspace_attempts)
class GoodCtx:
    class session:
        @staticmethod
        async def list_roots():
            class R: uri = "file:///D:/work/proj"
            class Res: roots = [R()]
            return Res()
asyncio.run(W._discover_workspace_dir(GoodCtx()))
chk("第 3 次成功后 primed", W._workspace_primed is True)
chk("工作空间目录已取到", W._workspace_dir == "D:/work/proj", "→ %r" % W._workspace_dir)

print()
print("=== 4. 导出：先写 .part、成功后改名，中断不留半截文件 ===")
# 必须接管 get_connection/put_connection，否则 export_csv_impl 会真的去建连
class BoomCursor:
    description = [("a",), ("b",)]
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, s): pass
    def fetchmany(self, n): raise RuntimeError("驱动炸了")
class FakeConn:
    def cursor(self): return BoomCursor()
    def rollback(self): pass
    def commit(self): pass
    def close(self): pass
CV.get_connection = lambda ds, tdb=None: FakeConn()
CV.put_connection = lambda ds, c, tdb=None: None
tmpd = tempfile.mkdtemp(prefix="csv_")
target = os.path.join(tmpd, "out.csv")
try:
    CV.export_csv_impl("SELECT a,b FROM t", target, {"db_type": "mysql"}, "stu")
    chk("异常应向上抛", False)
except Exception as e:
    chk("异常确实抛出", "驱动炸了" in str(e), "→ %s" % e)
chk("目标文件不存在", not os.path.exists(target), "→ %s" % os.listdir(tmpd))
chk(".part 临时文件也被清掉", not any(x.endswith(".part") for x in os.listdir(tmpd)),
    "→ 残留: %s" % os.listdir(tmpd))

class OkCursor(BoomCursor):
    _n = 0
    def fetchmany(self, n):
        if self._n == 0:
            self._n = 1
            return [(1, "x"), (2, "y")]
        return []
class OkConn(FakeConn):
    def cursor(self): return OkCursor()
CV.get_connection = lambda ds, tdb=None: OkConn()
target2 = os.path.join(tmpd, "ok.csv")
r = CV.export_csv_impl("SELECT a,b FROM t", target2, {"db_type": "mysql"}, "stu")
chk("成功导出 2 行", r["rows_exported"] == 2, "→ %s 行" % r["rows_exported"])
chk("无 .part 残留", not any(x.endswith(".part") for x in os.listdir(tmpd)),
    "→ %s" % sorted(os.listdir(tmpd)))
chk("utf-8-sig BOM（Excel 可直接打开）",
    open(target2, "rb").read(3) == codecs.BOM_UTF8)

print()
print("=" * 56)

# pytest 入口。原先 sys.exit() 在模块级，pytest 在 collection 阶段就 SystemExit，
# 于是 `pytest tests/` 永远报 ERROR 而不是跑测试——本函数让回归报成正常的测试失败。
def test_mcp_fixes():
    assert ok, "test_mcp_fixes 有失败项，见上方 ❌ 明细"

if __name__ == "__main__":
    print("✅ 全部通过" if ok else "❌ 有失败项")
    sys.exit(0 if ok else 1)
