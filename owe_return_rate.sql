--=====================================================================================
-- 指标   : 欠费迎回率（分支局）
-- 口径   : 准预0账龄 + 后付1账龄 + 后付2账龄 的初始欠费宽带用户中，
--          当月由欠费状态回归为正常状态的用户占比（不含欠费金额在10元以内）
-- 分公司 : 45 (綦江)
--
--=====================================================================================
-- 【每月只需改两处】
--   1. PARAM 里的 STAT_MON / PREV_MON      ← 月份参数
--   2. CREATE TABLE 的表名后缀 _202608     ← 表名里带月份，SQL 无法参数化表名
--
--   跑 202609 时：STAT_MON='202609'  PREV_MON='202608'  表名后缀 _202609
--
--   若嫌表名也要改麻烦，见文末【方案B：固定表名 + 追加】，那样每月只改 1 处。
--=====================================================================================
-- 【月份映射】
--   分母（欠费清单，各表 ACCT_MONTH 不同，按实测数据）：
--       0账龄(准预)  RPT_OWE_ZHUNYU_0_AGE_MON_45     ACCT_MONTH = STAT_MON  (202608)
--       1账龄(后付)  RPT_OWE_POSTPAID_1_AGE_MON_45   ACCT_MONTH = PREV_MON  (202607)
--       2账龄(后付)  RPT_OWE_POSTPAID_2_AGE_MON_45   ACCT_MONTH = PREV_MON  (202607)
--
--   分子（迎回清单，"账期"是迎回发生日期，如 20260802 → 归入 202608）：
--       三张 *_SUCCESS_LIST_45 统一取  "账期" 落在 STAT_MON 这一整月
--       写成  >= STAT_MON||'01'  AND  <= STAT_MON||'99'
--       '20260899' 非真实日期，但作为字符串上界可盖住所有 '202608xx'，
--       这样不必计算次月月初，年底 12→1 月的跨年进位也自动规避。
--=====================================================================================
-- 【性能设计】
--   1) WHERE 里字段不套函数(不 TRIM / 不 SUBSTR)，让分区裁剪与索引生效。
--      ACCT_MONTH 是 CHAR(6)、"账期" 是 CHAR(8)，定长字段与定长字面量比较时
--      ANSI 按空格补齐语义处理，TRIM 本就多余。
--   2) 迎回日期用范围比较而非 SUBSTR(...)=...，同为走索引。
--   3) "欠费金额" > 10 直接下推到三张分母表的 WHERE，扫描阶段即过滤。
--      每月每用户仅一条记录，故无需先做 GROUP BY SUM 汇总。
--   4) 迎回集先 DISTINCT 去重再参与关联，防一对多把分子放大。
--   5) 关联走 Hash Join，O(N+M)，非逐条遍历。
--   6) 支局名称用 REPLACE 去掉 '綦江分公司' 前缀。用 REPLACE 而非 SUBSTR：
--      前缀缺失的行会原样保留不被截断，且 SUBSTR 在 UTF-8 库按字节计数易错。
--      该处理只在 SELECT 列表，不在 WHERE，不影响索引。
--
--   ※ PARAM 是单行 CTE，优化器通常能在运行期把值折叠进谓词、正常做分区裁剪。
--     万一实测发现比写死字面量慢很多，把 P.STAT_MON / P.PREV_MON 各处替换回
--     '202608' / '202607' 字面量即可（回到改 9 处但绝对走索引的版本）。
--=====================================================================================
-- 【SQL 方言】ANSI 标准写法，字符串拼接用 ||
--   PARAM 的 FROM 子句按库二选一（下面已按 DB2 写，报"表不存在"就换）：
--       DB2                            FROM SYSIBM.SYSDUMMY1
--       Oracle                         FROM DUAL
--       Teradata / GaussDB / Vertica   整行 FROM 删掉
--   DB2 可在末尾加      FOR FETCH ONLY WITH UR       (脏读免锁)
--   Teradata 可在开头加  LOCKING ROW FOR ACCESS;
--=====================================================================================

CREATE TABLE XC990026.RPT_OWE_RETURN_RATE_45_202608 AS (

WITH

-- ⓪ 月份参数：全脚本唯一需要修改月份的地方
PARAM AS (
    SELECT '202608' AS STAT_MON,      -- ★ 统计月
           '202607' AS PREV_MON       -- ★ 上一月
    FROM   SYSIBM.SYSDUMMY1           -- ← 非 DB2 请按上方【SQL 方言】调整
),

-- ① 分母：三张账龄清单纵向合并，各取对应 ACCT_MONTH，扫描时即滤掉 ≤10 元
BASE AS (
    SELECT CAST('0账龄(准预)' AS VARCHAR(60))   AS AGE_TYPE,
           1                                    AS AGE_SORT,
           CAST(TRIM(T."支局ID")   AS VARCHAR(30)) AS BR_ID,
           CAST(REPLACE(TRIM(T."支局名称"), '綦江分公司', '') AS VARCHAR(50)) AS BR_NAME,
           TRIM(T."宽带ID")                     AS BB_ID
    FROM   XC990026.RPT_OWE_ZHUNYU_0_AGE_MON_45 T
    CROSS JOIN PARAM P
    WHERE  T.ACCT_MONTH = P.STAT_MON                     -- 0账龄取统计月本身
      AND  T."分公司ID" = 45
      AND  T."欠费金额" > 10                             -- 严格大于 10，正好 10 元也剔除

    UNION ALL

    SELECT CAST('1账龄(后付)' AS VARCHAR(60)),
           2,
           CAST(TRIM(T."支局ID")   AS VARCHAR(30)),
           CAST(REPLACE(TRIM(T."支局名称"), '綦江分公司', '') AS VARCHAR(50)),
           TRIM(T."宽带ID")
    FROM   XC990026.RPT_OWE_POSTPAID_1_AGE_MON_45 T
    CROSS JOIN PARAM P
    WHERE  T.ACCT_MONTH = P.PREV_MON                     -- 1账龄取上一月
      AND  T."分公司ID" = 45
      AND  T."欠费金额" > 10

    UNION ALL

    SELECT CAST('2账龄(后付)' AS VARCHAR(60)),
           3,
           CAST(TRIM(T."支局ID")   AS VARCHAR(30)),
           CAST(REPLACE(TRIM(T."支局名称"), '綦江分公司', '') AS VARCHAR(50)),
           TRIM(T."宽带ID")
    FROM   XC990026.RPT_OWE_POSTPAID_2_AGE_MON_45 T
    CROSS JOIN PARAM P
    WHERE  T.ACCT_MONTH = P.PREV_MON                     -- 2账龄同样取上一月
      AND  T."分公司ID" = 45
      AND  T."欠费金额" > 10
),

-- ② 分子：三张迎回清单统一取【迎回发生月 = 统计月】，先去重
BACK AS (
    SELECT DISTINCT AGE_TYPE, BB_ID
    FROM (
        SELECT CAST('0账龄(准预)' AS VARCHAR(60)) AS AGE_TYPE,
               TRIM(S."宽带ID")                   AS BB_ID
        FROM   XC990026.RPT_OWE_ZHUNYU_0_AGE_SUCCESS_LIST_45 S
        CROSS JOIN PARAM P
        WHERE  S."账期" >= P.STAT_MON || '01'
          AND  S."账期" <= P.STAT_MON || '99'
          AND  S."分公司ID" = 45
          AND (S."是否迎回" = '1'
               OR S."是否迎回" IS NULL
               OR TRIM(S."是否迎回") = '')

        UNION ALL

        SELECT CAST('1账龄(后付)' AS VARCHAR(60)),
               TRIM(S."宽带ID")
        FROM   XC990026.RPT_OWE_POSTPAID_1_AGE_SUCCESS_LIST_45 S
        CROSS JOIN PARAM P
        WHERE  S."账期" >= P.STAT_MON || '01'
          AND  S."账期" <= P.STAT_MON || '99'
          AND  S."分公司ID" = 45
          AND (S."是否迎回" = '1'
               OR S."是否迎回" IS NULL
               OR TRIM(S."是否迎回") = '')

        UNION ALL

        SELECT CAST('2账龄(后付)' AS VARCHAR(60)),
               TRIM(S."宽带ID")
        FROM   XC990026.RPT_OWE_POSTPAID_2_AGE_SUCCESS_LIST_45 S
        CROSS JOIN PARAM P
        WHERE  S."账期" >= P.STAT_MON || '01'
          AND  S."账期" <= P.STAT_MON || '99'
          AND  S."分公司ID" = 45
          AND (S."是否迎回" = '1'
               OR S."是否迎回" IS NULL
               OR TRIM(S."是否迎回") = '')
    ) X
    WHERE BB_ID IS NOT NULL
      AND BB_ID <> ''
),

-- ③ 以分母为主表左连迎回集（保证分子 ⊆ 分母，迎回率不可能 >100%）
JOINED AS (
    SELECT A.BR_ID,
           A.BR_NAME,
           A.AGE_TYPE,
           A.AGE_SORT,
           A.BB_ID,
           CASE WHEN B.BB_ID IS NOT NULL THEN 1 ELSE 0 END AS IS_BACK
    FROM   BASE A
    LEFT JOIN BACK B
           ON A.BB_ID    = B.BB_ID
          AND A.AGE_TYPE = B.AGE_TYPE
),

-- ④ 支局内用户级聚合（严格口径：名下所有账龄全部迎回才算回归正常）
USER_BR AS (
    SELECT BR_ID,
           BR_NAME,
           BB_ID,
           COUNT(*)     AS AGE_CNT,
           SUM(IS_BACK) AS BACK_CNT
    FROM   JOINED
    GROUP BY BR_ID, BR_NAME, BB_ID
),

-- ⑤ 全分公司用户级聚合（跨支局按宽带ID全局去重）
USER_ALL AS (
    SELECT BB_ID,
           COUNT(*)     AS AGE_CNT,
           SUM(IS_BACK) AS BACK_CNT
    FROM   JOINED
    GROUP BY BB_ID
),

-- ⑥ 支局×账龄明细 + 支局小计 + 全分公司合计
RST AS (
    -- 支局 × 账龄 明细
    SELECT 0                                                         AS LVL,
           J.BR_ID                                                   AS BR_ID,
           J.BR_NAME                                                 AS BR_NAME,
           J.AGE_SORT                                                AS SORT_KEY,
           J.AGE_TYPE                                                AS AGE_TYPE,
           COUNT(DISTINCT J.BB_ID)                                   AS DENO_CNT,
           COUNT(DISTINCT CASE WHEN J.IS_BACK = 1 THEN J.BB_ID END)  AS NUME_CNT
    FROM   JOINED J
    GROUP BY J.BR_ID, J.BR_NAME, J.AGE_SORT, J.AGE_TYPE

    UNION ALL

    -- 支局小计（该支局内宽带ID去重）
    SELECT 0,
           U.BR_ID,
           U.BR_NAME,
           9,
           CAST('支局合计' AS VARCHAR(60)),
           COUNT(*),
           SUM(CASE WHEN U.AGE_CNT = U.BACK_CNT THEN 1 ELSE 0 END)
    FROM   USER_BR U
    GROUP BY U.BR_ID, U.BR_NAME

    UNION ALL

    -- 全分公司合计（跨支局宽带ID去重）
    SELECT 1,
           CAST('' AS VARCHAR(30)),
           CAST('全分公司合计' AS VARCHAR(50)),
           99,
           CAST('合计' AS VARCHAR(60)),
           COUNT(*),
           SUM(CASE WHEN U.AGE_CNT = U.BACK_CNT THEN 1 ELSE 0 END)
    FROM   USER_ALL U
)

SELECT R.BR_ID                                    AS "支局ID",
       R.BR_NAME                                  AS "支局名称",
       R.AGE_TYPE                                 AS "账龄类型",
       R.DENO_CNT                                 AS "初始欠费用户数",
       R.NUME_CNT                                 AS "迎回用户数",
       CASE WHEN R.DENO_CNT > 0
            THEN ROUND(CAST(R.NUME_CNT AS DECIMAL(18,4)) * 100 / R.DENO_CNT, 2)
       END                                        AS "迎回率百分比",
       -- 下面两列仅供排序：CREATE TABLE AS 内不允许写 ORDER BY，表本身无序，
       -- 查这张表时用  ORDER BY "层级","支局名称","排序"  即可还原报表顺序
       R.LVL                                      AS "层级",
       R.SORT_KEY                                 AS "排序"
FROM   RST R

) WITH DATA
;

-- 建表后按报表顺序查看：
-- SELECT * FROM XC990026.RPT_OWE_RETURN_RATE_45_202608
-- ORDER BY "层级", "支局名称", "排序";


--=====================================================================================
-- 【方案B：固定表名 + 每月追加】—— 想每月只改 1 处、且要做月度趋势对比时用这个
--
-- 与上面方案的区别：表名不带月份，改为在表里存一列"统计月"，每月 INSERT 追加。
-- 这样每月只需改 PARAM 里的两个值，表名永远不动，还能一张表看全年趋势。
--
-- 首次建空表（只跑一次）：把上面的 CREATE TABLE 行改成
--     CREATE TABLE XC990026.RPT_OWE_RETURN_RATE_45 AS (
-- 并在最终 SELECT 里加一列  P2.STAT_MON AS "统计月"（需 CROSS JOIN PARAM P2）。
--
-- 之后每月执行：
--     DELETE FROM XC990026.RPT_OWE_RETURN_RATE_45 WHERE "统计月" = '202609';  -- 幂等，可重跑
--     INSERT INTO XC990026.RPT_OWE_RETURN_RATE_45
--     WITH PARAM AS ( SELECT '202609' AS STAT_MON, '202608' AS PREV_MON FROM SYSIBM.SYSDUMMY1 ),
--          BASE AS ( ... 以下与上面完全相同 ... )
--     SELECT ... ;
--
-- 先 DELETE 再 INSERT 是为了幂等：同一个月重跑不会产生重复行。
-- 需要我把方案B完整写出来的话说一声。
--=====================================================================================


--=====================================================================================
-- 【分步排查 SQL】结果异常时按 1→4 顺序单独跑，能精确定位断在哪一环
--=====================================================================================

-- 1) 三张分母表的 ACCT_MONTH 分布 → 确认 202608 / 202607 / 202607 确实有量，
--    尤其确认 2账龄表取 202607 而非 202606
-- SELECT 'ZHUNYU_0'   AS TB, ACCT_MONTH, COUNT(*) AS CNT FROM XC990026.RPT_OWE_ZHUNYU_0_AGE_MON_45    GROUP BY ACCT_MONTH
-- UNION ALL
-- SELECT 'POSTPAID_1' AS TB, ACCT_MONTH, COUNT(*)        FROM XC990026.RPT_OWE_POSTPAID_1_AGE_MON_45 GROUP BY ACCT_MONTH
-- UNION ALL
-- SELECT 'POSTPAID_2' AS TB, ACCT_MONTH, COUNT(*)        FROM XC990026.RPT_OWE_POSTPAID_2_AGE_MON_45 GROUP BY ACCT_MONTH
-- ORDER BY 1, 2;

-- 2) 迎回清单的"账期"分布 → 确认 202608 那批存在
-- SELECT "账期", COUNT(*) AS CNT, COUNT(DISTINCT "宽带ID") AS USR_CNT
-- FROM   XC990026.RPT_OWE_POSTPAID_1_AGE_SUCCESS_LIST_45
-- GROUP BY "账期"
-- ORDER BY "账期";

-- 3) "是否迎回" 的实际取值 → 若全为 NULL/空串，脚本的 null-safe 写法已兜住
-- SELECT "是否迎回", COUNT(*) AS CNT
-- FROM   XC990026.RPT_OWE_POSTPAID_1_AGE_SUCCESS_LIST_45
-- GROUP BY "是否迎回";

-- 4) 分母 / 分子 各自的量 → 分母有量而分子为 0，问题必在月份映射
-- SELECT COUNT(DISTINCT TRIM("宽带ID")) AS DENO_1AGE
-- FROM   XC990026.RPT_OWE_POSTPAID_1_AGE_MON_45
-- WHERE  ACCT_MONTH = '202607' AND "分公司ID" = 45 AND "欠费金额" > 10;
--
-- SELECT COUNT(DISTINCT TRIM("宽带ID")) AS BACK_1AGE
-- FROM   XC990026.RPT_OWE_POSTPAID_1_AGE_SUCCESS_LIST_45
-- WHERE  "账期" >= '20260801' AND "账期" <= '20260899' AND "分公司ID" = 45;
