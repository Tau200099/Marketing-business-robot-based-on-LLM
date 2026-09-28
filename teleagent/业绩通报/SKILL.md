---
name: 业绩通报
description: 用「业绩群台账」MCP 工具查各战队/小组/个人业绩，生成通报文字、周报 PPT，或把通报发到业绩群。适用于中国电信营销活动的晒单统计。
---

# 业绩通报

## 什么时候用

用户问业绩、排名、谁没晒单、某人几单几分、要通报 / 周报 / PPT / Excel、要把通报发到群里、要更新名册。

## 前提

「业绩群台账」（yeji）MCP 工具已连接，工具有：
team_stats、group_stats、person_stats、list_orders、roster、bulletin_message、export_report、parse_order_text、send_bulletin_to_group、import_roster。

## 铁规矩

1. **所有数字只能来自工具返回**，不要自己加总、估算或按套餐推积分。
2. 时间范围：问「今天」用 `today`，「本周 / 这周」用 `week`，「昨天」`yesterday`，「上周」`last_week`，「本月」`month`，具体日期用 `YYYY-MM-DD`，一段用 `YYYY-MM-DD~YYYY-MM-DD`。没说时间：排名类默认 today，「累计 / 一共」默认 week。
3. 排名为 null 的战队进度是 0，不要给它编名次。
4. 「待核对」的单是缺字段等人补，计数照常，可以提一句「其中 N 单待核对」。
5. 涉及发群（send_bulletin_to_group）必须是用户明确说了「发到群」才调，调之前先把要发的文字给用户看一眼。

## 常见任务怎么做

### 查业绩 / 排名

1. `team_stats(period)` 取各战队；需要细到小组再 `group_stats(period, team)`；问某人用 `person_stats(name, period)`。
2. 回答先给结论（谁第一、进度多少），再给关键数字，一行一条。

### 哪些组没晒单

`group_stats(period, only_zero=true)`，按战队分行列出小组和成员。

### 通报文字

`bulletin_message(period)`，原样给用户；用户要改语气就在这个基础上改，**数字和名次不动**。

### 周报 / PPT / Excel

1. `export_report(rank_period, ledger_period)` 拿到 Excel 和表格图 PNG 的路径（默认排名按天、清单按周；做周报两者都传 `week`）。
2. `team_stats(week)` 取本周汇总，`group_stats(week)` 取小组明细。
3. 用演示制作技能生成 PPT，建议结构：
   - 第 1 页 标题：「{活动名} 第 N 周业绩周报」+ 日期范围
   - 第 2 页 战队排名：表格图 PNG 整页放
   - 第 3 页 关键数字：新增宽带 / 中高套 / 云盒 / 积分 各战队对比（用 team_stats 的数）
   - 第 4 页 亮点与不足：第一名、进步最大的、一单没有的小组（来自 group_stats only_zero）
   - 第 5 页 下周目标：目标值（team_stats 的「目标」）
4. Excel 直接给路径，不要重新造表。

### 发到群

先 `bulletin_message(today)` 给用户看文字，确认后 `send_bulletin_to_group()`。

### 更新名册

用户给 Excel 路径 → `import_roster(path)` → 把返回的「名册」摘要（多少人 / 战队 / 小组）告诉用户。

### 核对一条晒单

用户贴一段晒单文字问「这样会怎么记」→ `parse_order_text(text)`，把类型 / 套餐 / 各项计数说清楚；「能否严格解析」为 false 时说明机器人会交给模型容错解析。

## 口径速查

- 战队 = 支局 = 战区；小组是两人一组，用两个人的名字连着叫
- 号卡类（山城卡 / 单C / 携转 / 副卡 / 橙分期）一律「单C」，不算宽带、不算中高套
- 中高套 = 139 及以上；千兆/500兆 = 139 及以上（只对新增记）
- 综合进度 = 新增宽带/8×25% + 中高套/4×30% + 云盒AI智屏/5×15% + 积分/1000×20%（目标值以 team_stats 返回为准）
- 排名按天，清单按周（周一自动翻页）
