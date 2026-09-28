--=====================================================================================
-- 用途   : 初始欠费宽带用户【明细清单】提取
-- 说明   : 只做数据提取落表，不做任何计数与占比计算，统计在 Excel 中自行透视
-- 范围   : 准预0账龄 + 后付1账龄 + 后付2账龄，分公司 45 (綦江)
-- 过滤   : 分公司ID = 45 / 欠费金额 > 10 / 各表按对应 ACCT_MONTH
--
--=====================================================================================
-- 【使用方式】
--   第一步 CREATE TABLE  —— 只跑一次，建空表
--   第二步 DELETE+INSERT —— 每月跑，改 3 处月份即可（★1 ★2 ★3，集中在同一屏内）
--
--   先 DELETE 再 INSERT 是为了幂等：同一个月重跑不会产生重复行。
--
--   跑 202609 时：★1='202609'  ★2='202609'  ★3='202608'
--=====================================================================================
-- 【月份映射】按实测数据，三张表的 ACCT_MONTH 不同
--       0账龄(准预)  RPT_OWE_ZHUNYU_0_AGE_MON_45     ACCT_MONTH = 统计月   (202608)
--       1账龄(后付)  RPT_OWE_POSTPAID_1_AGE_MON_45   ACCT_MONTH = 上一月   (202607)
--       2账龄(后付)  RPT_OWE_POSTPAID_2_AGE_MON_45   ACCT_MONTH = 上一月   (202607)
--
-- 【已按要求去掉的内容】
--   · 分子/分母计数与迎回率计算  · 是否迎回、迎回日期
--   · 支局分类(城区/二类/三类)   · 分公司ID、分公司名称(全表恒为 45/綦江，冗余)
--   ※ 因此本表【不含任何迎回信息】，单靠它无法计算迎回率。
--     若 Excel 里要做匹配，需另外提取一张迎回清单表。
--
-- 【SQL 方言】ANSI 标准写法，字符串拼接用 ||
--   PARAM 的 FROM 子句按库二选一（下面已按 DB2 写，报"表不存在"就换）：
--       DB2                            FROM SYSIBM.SYSDUMMY1
--       Oracle                         FROM DUAL
--       Teradata / GaussDB / Vertica   整行 FROM 删掉
--   INSERT 与 WITH 的顺序按库不同：
--       DB2 / Teradata      INSERT INTO t (cols) WITH cte AS (...) SELECT ...   ← 本文写法
--       GaussDB / Postgres  WITH cte AS (...) INSERT INTO t (cols) SELECT ...   ← WITH 提到最前
--   欠费金额的 DOUBLE：Teradata 用 FLOAT，GaussDB 用 DOUBLE PRECISION
--=====================================================================================


--=====================================================================================
-- 第一步：建空表（只跑一次）
--   若无 XC990026 建表权限，把表名换成你有权限的 schema 即可
--=====================================================================================

CREATE TABLE XC990026.RPT_OWE_DETAIL_45 (
    "统计月"     CHAR(6),          -- 本次提取对应的统计月，如 202608
    "账龄类型"   VARCHAR(10),      -- 0账龄 / 1账龄 / 2账龄
    "记录月"     CHAR(6),          -- 源表 ACCT_MONTH 原值，便于追溯
    "支局ID"     VARCHAR(30),
    "支局名称"   VARCHAR(50),      -- 已去掉"綦江分公司"前缀
    "支局属性"   VARCHAR(10),      -- 城市 / 农村
    "宽带ID"     VARCHAR(30),
    "付费类型"   VARCHAR(60),      -- 准预 / 后付费
    "欠费金额"   DOUBLE
)
;


--=====================================================================================
-- 第二步：每月执行（改下面 ★1 ★2 ★3 三处月份）
--=====================================================================================

DELETE FROM XC990026.RPT_OWE_DETAIL_45
WHERE  "统计月" = '202608'                      -- ★1  统计月（与 ★2 保持一致）
;


INSERT INTO XC990026.RPT_OWE_DETAIL_45
    ("统计月", "账龄类型", "记录月", "支局ID", "支局名称",
     "支局属性", "宽带ID", "付费类型", "欠费金额")

WITH PARAM AS (
    SELECT '202608' AS STAT_MON,                -- ★2  统计月
           '202607' AS PREV_MON                 -- ★3  上一月
    FROM   SYSIBM.SYSDUMMY1                     -- ← 非 DB2 请按上方【SQL 方言】调整
)

-- ① 准预 0账龄：取统计月本身
SELECT P.STAT_MON,
       CAST('0账龄' AS VARCHAR(10)),
       T.ACCT_MONTH,
       CAST(TRIM(T."支局ID")   AS VARCHAR(30)),
       CAST(REPLACE(TRIM(T."支局名称"), '綦江分公司', '') AS VARCHAR(50)),
       CAST(TRIM(T."支局属性") AS VARCHAR(10)),
       CAST(TRIM(T."宽带ID")   AS VARCHAR(30)),
       T."付费类型",
       T."欠费金额"
FROM   XC990026.RPT_OWE_ZHUNYU_0_AGE_MON_45 T
CROSS JOIN PARAM P
WHERE  T.ACCT_MONTH = P.STAT_MON
  AND  T."分公司ID" = 45
  AND  T."欠费金额" > 10                        -- 严格大于 10，正好 10 元也剔除

UNION ALL

-- ② 后付 1账龄：取上一月
SELECT P.STAT_MON,
       CAST('1账龄' AS VARCHAR(10)),
       T.ACCT_MONTH,
       CAST(TRIM(T."支局ID")   AS VARCHAR(30)),
       CAST(REPLACE(TRIM(T."支局名称"), '綦江分公司', '') AS VARCHAR(50)),
       CAST(TRIM(T."支局属性") AS VARCHAR(10)),
       CAST(TRIM(T."宽带ID")   AS VARCHAR(30)),
       T."付费类型",
       T."欠费金额"
FROM   XC990026.RPT_OWE_POSTPAID_1_AGE_MON_45 T
CROSS JOIN PARAM P
WHERE  T.ACCT_MONTH = P.PREV_MON
  AND  T."分公司ID" = 45
  AND  T."欠费金额" > 10

UNION ALL

-- ③ 后付 2账龄：同样取上一月
SELECT P.STAT_MON,
       CAST('2账龄' AS VARCHAR(10)),
       T.ACCT_MONTH,
       CAST(TRIM(T."支局ID")   AS VARCHAR(30)),
       CAST(REPLACE(TRIM(T."支局名称"), '綦江分公司', '') AS VARCHAR(50)),
       CAST(TRIM(T."支局属性") AS VARCHAR(10)),
       CAST(TRIM(T."宽带ID")   AS VARCHAR(30)),
       T."付费类型",
       T."欠费金额"
FROM   XC990026.RPT_OWE_POSTPAID_2_AGE_MON_45 T
CROSS JOIN PARAM P
WHERE  T.ACCT_MONTH = P.PREV_MON
  AND  T."分公司ID" = 45
  AND  T."欠费金额" > 10
;


--=====================================================================================
-- 第三步：取数导出到 Excel
--=====================================================================================

-- SELECT * FROM XC990026.RPT_OWE_DETAIL_45
-- WHERE  "统计月" = '202608'
-- ORDER BY "支局名称", "账龄类型", "宽带ID";


--=====================================================================================
-- 【核对 SQL】跑完后建议核对一下，确认与预期一致
--=====================================================================================

-- 1) 各账龄提取了多少条、多少个宽带用户
-- SELECT "统计月", "账龄类型", "记录月",
--        COUNT(*)                     AS 条数,
--        COUNT(DISTINCT "宽带ID")     AS 用户数,
--        MIN("欠费金额")              AS 最小欠费,
--        MAX("欠费金额")              AS 最大欠费
-- FROM   XC990026.RPT_OWE_DETAIL_45
-- WHERE  "统计月" = '202608'
-- GROUP BY "统计月", "账龄类型", "记录月"
-- ORDER BY "账龄类型";
--   → 若"条数" > "用户数"，说明同一用户同账龄有多条记录，Excel 透视时要注意去重
--   → 若"最小欠费" <= 10，说明 > 10 的过滤没生效，需检查字段名

-- 2) 三张源表的 ACCT_MONTH 分布 → 确认 202608 / 202607 / 202607 确实有量
-- SELECT 'ZHUNYU_0'   AS TB, ACCT_MONTH, COUNT(*) AS CNT FROM XC990026.RPT_OWE_ZHUNYU_0_AGE_MON_45    GROUP BY ACCT_MONTH
-- UNION ALL
-- SELECT 'POSTPAID_1' AS TB, ACCT_MONTH, COUNT(*)        FROM XC990026.RPT_OWE_POSTPAID_1_AGE_MON_45 GROUP BY ACCT_MONTH
-- UNION ALL
-- SELECT 'POSTPAID_2' AS TB, ACCT_MONTH, COUNT(*)        FROM XC990026.RPT_OWE_POSTPAID_2_AGE_MON_45 GROUP BY ACCT_MONTH
-- ORDER BY 1, 2;

-- 3) 跨账龄是否有同一宽带用户（预期为 0 行）
-- SELECT "宽带ID", COUNT(DISTINCT "账龄类型") AS 账龄数
-- FROM   XC990026.RPT_OWE_DETAIL_45
-- WHERE  "统计月" = '202608'
-- GROUP BY "宽带ID"
-- HAVING COUNT(DISTINCT "账龄类型") > 1;
