# TeleAgent（星辰超级智能体）接入手册

把业绩群机器人的台账能力接进 TeleAgent 桌面版：主管在电脑上一句话查业绩、出周报 PPT、把通报发到群。

```
业绩群（飞书 / 企微）                       主管的电脑（TeleAgent 桌面版）
   │ 晒单                                        │「本周各战队业绩做成周报 PPT」
   ▼                                             ▼
 bot.py ──写──▶ campaign.json ◀──读── mcp_server.py ◀── MCP 工具（10 个）
 点赞 / 记单 / 通报                                    + 「业绩通报」技能
```

两边共用同一份台账、同一段查询代码（`botcore/ledger_tools.py`）—— 员工在群里 @机器人 问，
和主管在 TeleAgent 里问，答案一定一致。

菜单名称以官方《星辰超级智能体桌面版操作手册》为准（头像浮窗 →【设置】→【工具设置】），
你装的版本按钮位置略有不同的话按名字找。

---

## 第 0 步：确认机器人这边准备好了（1 分钟）

在 `D:\feishu_bot` 目录打开 PowerShell：

```powershell
.venv\Scripts\python mcp_server.py --check
```

看到下面这样就是好的：

```
[OK] 服务端启动正常，10 个工具：team_stats、group_stats、…
[OK] roster({}) -> { "战队": [ "通惠战队", … ], "人数": 62, "小组数": 31 }…
[OK] team_stats({'period': 'today'}) -> …
可以导入 TeleAgent 了。配置里 command 填（JSON 里反斜杠要写成两个）：
  "D:\\feishu_bot\\.venv\\Scripts\\python.exe"
```

报 `[FAIL]` 就先看这一步的报错，别往下走。
台账是机器人进程（`bot.py`）在写，**机器人不跑就没人记单**，但导入和查询不需要它在线。

---

## 第 1 步：导入 MCP 工具（3 分钟）

1. 打开 TeleAgent 桌面版，点右上角**头像**，浮窗里点【设置】。
2. 左侧选【工具设置】。
3. 点【从JSON导入/添加】。
4. 把 [teleagent/mcp_config.json](../teleagent/mcp_config.json) 的内容整个粘进去（就是下面这段）：

   ```json
   {
     "mcpServers": {
       "yeji": {
         "command": "D:\\feishu_bot\\.venv\\Scripts\\python.exe",
         "args": ["D:\\feishu_bot\\mcp_remote.py"],
         "env": {
           "PYTHONIOENCODING": "utf-8",
           "PROGRAMDATA": "C:\\ProgramData"
         }
       }
     }
   }
   ```

   三个字段的意思（官方文档的定义）：`command` 启动程序路径、`args` 命令行参数、`env` 环境变量。
   **路径里的反斜杠必须写两个**（JSON 规范），项目不在 `D:\feishu_bot` 的话把两处路径改掉。

   > 2026-09-23 起台账在服务器上。`mcp_remote.py` 是本机的转接器：查数原样转发给服务器（走 SSH，
   > 服务器不开端口）；导出报表时把 Excel / 表格图用 scp 拉回本机 `reports\`，好让 TeleAgent 做 PPT；
   > 用本机 Excel 更新名册时先传上服务器再导入。`PROGRAMDATA` 那行不能省 —— Windows 自带的 ssh
   > 缺这个变量会一声不响地连不上。接入前先在 PowerShell 跑 `ssh root@47.109.158.170 echo ok`，
   > 能打出 ok 才行；再跑 `.venv\Scripts\python mcp_remote.py --check` 自检。
5. 保存。工具列表里出现 `yeji`，状态变成绿色**「已连接」**。
   没变绿就点它的【连接】按钮；还不行看本文末尾的排查表。
6. 回到首页，点输入框附近的 **MCP** 按钮，能看到 yeji 下面 10 个工具：

   | 工具 | 干什么 |
   |---|---|
   | team_stats | 各战队汇总 + 综合进度 + 排名 + 目标值 |
   | group_stats | 各小组汇总，可只看某战队 / 只看一单没有的 |
   | person_stats | 某人的单数、积分、明细、所在小组 |
   | list_orders | 某时段的单，按时间倒序 |
   | roster | 查名册 |
   | bulletin_message | 通报文字（前两名恭喜、末两名加油） |
   | export_report | 生成 Excel + 表格图，返回文件路径 |
   | parse_order_text | 一段晒单文字会怎么记（核对用） |
   | send_bulletin_to_group | 把通报发到群（明确要求时才用） |
   | import_roster | 从 Excel 更新名册 |

📸 竞赛截图 2：这个工具列表。

---

## 第 2 步：设工作空间（1 分钟，可选但建议）

TeleAgent 生成的文件（PPT、文档）都落在「工作空间」里，它本质是一个文件夹。
建议设成机器人的报表目录，这样 Excel、表格图、PPT 都在一处：

1. 首页输入框**左下角**的工作空间按钮 → 【选择文件夹】。
2. 选 `D:\feishu_bot\reports`，确认。

注意：任务发起后不能再改工作空间，所以先设再问。

---

## 第 3 步：第一次对话验证（2 分钟）

在输入框里依次试：

```
9月3日各战队业绩怎么样
```

TeleAgent 应该调 `team_stats`（对话里能看到工具调用），回答东林战队第一、2 单、进度 33.2%。
📸 竞赛截图 7。

```
本周哪些小组一单都没有
```

调 `group_stats(only_zero=true)`，按战队列小组。

```
把本周业绩导出 Excel
```

调 `export_report`，回两个路径：`reports\业绩通报_xxxx.xlsx` 和 `.png`。

---

## 第 4 步：导入「业绩通报」技能（2 分钟）

技能是告诉 TeleAgent「遇到业绩类任务该怎么用这些工具、周报 PPT 怎么排」。
不导也能用，导了它做周报更规矩、不会自己算数。

1. 左侧【技能】→【技能广场】→ 右上角【导入技能】。
2. 选 [teleagent/业绩通报技能.zip](../teleagent/业绩通报技能.zip)（根目录是 `SKILL.md`，官方要求的格式）。
3. 导入后在「我的技能」里能看到「业绩通报」。
4. 用的时候在输入框里打 `@`，用上下键选「业绩通报」回车，再写任务；
   或者不选 —— 官方说「未明确指定技能时，系统会根据任务描述自动选择技能」。

---

## 第 5 步：做一份周报 PPT（案例核心）

```
@业绩通报 把本周各战队业绩做成周报 PPT，表格图放第二页
```

TeleAgent 会：`export_report(week, week)` 拿 Excel 和 PNG → `team_stats(week)` / `group_stats(week)` 取数 →
用它自带的演示制作技能出 PPT，落在工作空间。
📸 竞赛截图 8：对话过程 + 打开的 PPT。

其它常用问法：

```
生成今天的通报文字，语气再鼓劲一点          → bulletin_message，数字和名次不会动
把通报发到群里                              → 先给你看文字，确认后 send_bulletin_to_group
用 D:\活动\新分组.xlsx 更新名册               → import_roster
这条晒单会怎么记：支局：文龙支局 业务类型:新增 办理套餐：19山城卡 揽收人:郭静
                                            → parse_order_text
```

`period` 参数它自己会填：今天 / 昨天 / 本周 / 上周 / 本月 / 2026-09-14 / 2026-09-08~2026-09-14。

---

## 第 6 步（可选）：换成星辰大模型

两处，互不影响：

- **TeleAgent 自己的模型**：首页右下角模型切换按钮选（任务进行中不能切，下一条生效）。
- **机器人的三个模型能力**（判报单 / 群里问答 / 容错解析）：`config.yaml` 的 `agent` 段已写好电信星辰的两条接入路径，
  改 `base_url` / `model`，`.env` 填 key，`python test_llm.py` 验通。MCP 工具和模型无关，不用改。

---

## 排查

| 现象 | 看哪里 |
|---|---|
| 导入后不是「已连接」 | 先跑第 0 步的 `--check`；JSON 里反斜杠是不是写成了一个；点【编辑】看路径有没有被改 |
| 工具列表是空的 | 点工具的【连接】；还不行就【删除】重新【从JSON导入】 |
| 工具能调但数据是空的 | 台账在 `D:\feishu_bot\campaign.json`；机器人没跑就没人记单；时间范围对不对（排名默认 today） |
| 查到的单和群里对不上 | `config.yaml` 的 `campaign` 段：`rank_period: day`、`ledger_period: week` |
| send_bulletin_to_group 说没配群 | `.env` 里 `FEISHU_CHAT_IDS`（用 `python diagnose.py` 列群 ID） |
| 中文乱码 | 配置里 `env` 的 `PYTHONIOENCODING` 要在 |
| 改了工具名 | 官方提醒：改名后历史任务可能无法正确引用，别改 `yeji` |

官方手册：[星辰超级智能体桌面版操作手册](https://www.teleai.com.cn/doc/7czHMDStGDQr1JsAMKcZ/Gik5VRTWAer7xDrqNBS7)
