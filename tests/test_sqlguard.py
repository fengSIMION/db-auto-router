import os, sys
# 按本文件位置定位仓库根（tests/ 的上一级），换机器/换目录都不用改这里
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db_auto_router.sqlguard as sg
from db_auto_router.util import sanitize_sql_for_scan as san

BS = chr(92); Q = chr(39)
fails = []
def chk(label, sql, want_pass, note=""):
    try:
        sg.validate_read_only_sql(sql); got = True; err = ""
    except Exception as e:
        got = False; err = str(e)[:60]
    ok = (got == want_pass)
    if not ok: fails.append((label, sql, want_pass, got, err))
    print("  %s %-46s %s" % ("✅" if ok else "❌", label, "放行" if got else "拦截"))

print("=== A. 原先复现的 4 个缺陷，现在必须被拦 ===")
chk("B1 PG '#' 运算符藏多语句", "SELECT B'1' # B'0'; DROP TABLE t", False)
chk("B2 PG 标准串吞分号", "SELECT " + Q+BS+Q + "; DELETE FROM t; --" + Q, False)
chk("B3 字面量 -- 抹掉 FOR UPDATE", "SELECT a FROM t WHERE a='1--' FOR UPDATE", False)
chk("B3b 字面量 -- 藏 INTO OUTFILE", "SELECT a FROM t WHERE a='1--' INTO OUTFILE 'C:/x.txt'", False)
chk("M10 EXPLAIN ANALYZE + DML CTE",
    "EXPLAIN ANALYZE WITH d AS (DELETE FROM t RETURNING id) SELECT * FROM d", False)

print()
print("=== B. 原先误判的合法查询，现在必须放行 ===")
chk("M4 行注释前缀", "-- 备注\nSELECT 1", True)
chk("M4b 块注释前缀", "/* 备注 */\nSELECT 1", True)
chk("M4c 多行注释前缀", "/* a\n b */\nSELECT COUNT(*) FROM t", True)

print()
print("=== C. 原有白名单行为不能回归（应放行） ===")
for label, sql in [
    ("普通 SELECT", "SELECT 1"), ("SELECT *", "SELECT * FROM t"),
    ("SHOW", "SHOW TABLES"), ("DESC", "DESC t"), ("EXPLAIN", "EXPLAIN SELECT * FROM t"),
    ("WITH 只读 CTE", "WITH x AS (SELECT 1) SELECT * FROM x"),
    ("带行注释", "SELECT 1 -- 备注"),
    ("注释里带分号", "SELECT 1 -- 备注; 完成时间"),
    ("字符串里带分号", "SELECT ';' FROM t"),
    ("字符串里带 LIMIT", "SELECT * FROM t WHERE a='LIMIT 5'"),
    ("反引号标识符", "SELECT `select` FROM `my table`"),
    ("双引号标识符(PG)", 'SELECT "col" FROM "tbl"'),
    ("字符串含 --", "SELECT 'a--b' FROM t"),
    ("纯聚合", "SELECT COUNT(*) FROM t"),
    ("CASE WHEN", "SELECT CASE WHEN a>1 THEN 1 ELSE 0 END FROM t"),
    ("派生表", "SELECT * FROM (SELECT id FROM t) x"),
    ("EXPLAIN ANALYZE SELECT", "EXPLAIN ANALYZE SELECT * FROM t"),
    ("EXPLAIN 选项语法", "EXPLAIN (ANALYZE, FORMAT JSON) SELECT * FROM t"),
]:
    chk(label, sql, True)

print()
print("=== D. 原有拦截行为不能回归（应拦截） ===")
for label, sql in [
    ("UPDATE", "UPDATE t SET x=1"), ("DELETE", "DELETE FROM t"), ("INSERT", "INSERT INTO t VALUES (1)"),
    ("DROP", "DROP TABLE t"), ("TRUNCATE", "TRUNCATE t"), ("CREATE", "CREATE TABLE x(a int)"),
    ("多语句", "SELECT 1; DROP TABLE t"), ("注释后多语句", "SELECT 1 -- x\n; DROP TABLE t"),
    ("WITH 内 DELETE", "WITH d AS (DELETE FROM t) SELECT * FROM d"),
    ("WITH 主语句 DELETE", "WITH x AS (SELECT 1) DELETE FROM t"),
    ("SELECT INTO 新表", "SELECT * INTO newtbl FROM t"),
    ("INTO OUTFILE", "SELECT * INTO OUTFILE '/tmp/x' FROM t"),
    ("EXPLAIN ANALYZE DELETE", "EXPLAIN ANALYZE DELETE FROM t"),
    ("FOR UPDATE", "SELECT * FROM t FOR UPDATE"),
    ("LOCK IN SHARE MODE", "SELECT * FROM t LOCK IN SHARE MODE"),
    ("SET GLOBAL", "SET GLOBAL general_log=ON"), ("PREPARE", 'PREPARE s FROM "DELETE FROM t"'),
    ("EXECUTE", "EXECUTE s"), ("KILL", "KILL 123"), ("DO", "DO SLEEP(1)"),
    ("USE", "USE otherdb"), ("LOAD DATA", 'LOAD DATA INFILE "/tmp/x" INTO TABLE t'),
    ("COPY FROM", "COPY t FROM '/tmp/x'"), ("FLUSH", "FLUSH TABLES WITH READ LOCK"),
    ("GRANT", "GRANT ALL ON *.* TO u"), ("CALL", "CALL my_proc()"),
    ("HANDLER", "HANDLER t OPEN"), ("ANALYZE TABLE", "ANALYZE TABLE t"),
    ("RENAME", "RENAME TABLE a TO b"), ("LOCK TABLES", "LOCK TABLES t WRITE"),
    ("注释前缀绕过", "-- x\nDELETE FROM t"), ("块注释前缀绕过", "/* x */ DELETE FROM t"),
    ("Dollar-quote 藏语句", "SELECT $$a; DROP TABLE t$$"),
]:
    chk(label, sql, False)

print()
print("=== E. 拆分器：保守默认只多拆不少拆 ===")
for label, sql, want in [
    ("MySQL # 注释单语句", "SELECT 1 # 备注", 1),
    ("PG # 运算符+分号 → 拆2", "SELECT B'1' # B'0'; DROP TABLE t", 2),
    ("PG 反斜杠串 → 拆开(≥2即拦)", "SELECT " + Q+BS+Q + "; DELETE FROM t; --" + Q, 3),
    ("字符串内分号不拆", "SELECT ';' FROM t", 1),
    ("行注释内分号不拆", "SELECT 1 -- a;b", 1),
    ("正常多语句", "SELECT 1; SELECT 2", 2),
]:
    n = len(sg.split_sql_statements(sql))
    ok = n == want
    if not ok: fails.append(("split:"+label, sql, want, n, ""))
    print("  %s %-46s 拆出 %d 条（期望 %d）" % ("✅" if ok else "❌", label, n, want))

print()
print("=" * 60)

# pytest 入口。原先 sys.exit(1) 在模块级且只在 fails 非空时执行——测试全绿时
# pytest 能收集，一旦真回归就变成 collection error，红得看不出是哪项不符。
# 本函数让回归报成正常的测试失败，并带上不符项明细。
def test_sqlguard():
    assert not fails, "%d 项不符: %s" % (
        len(fails), "; ".join("%s(期望%s实得%s)" % (f[0], f[2], f[3]) for f in fails))

if __name__ == "__main__":
    if fails:
        print("❌ %d 项不符:" % len(fails))
        for label, sql, want, got, err in fails:
            print("   %s | 期望 %s 实得 %s | %s" % (label, want, got, err or sql[:60]))
        sys.exit(1)
    print("✅ 全部通过（5 个缺陷已修 + 33 项放行 + 30 项拦截 + 6 项拆分 无回归）")
