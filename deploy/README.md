# 部署到服务器（全天运行）

## 先说三件最要紧的

**① 不需要公网 IP，不需要开任何端口**

机器人全部走**出站**连接：轮询企微 MCP、长连接 WebSocket、调大模型 API。
没有任何服务需要被外部访问。所以内网机器、家里的小主机都能跑，
不用备案、不用域名、不用配 nginx。

只要能出网访问这两个地址就行：

```
qyapi.weixin.qq.com     企微（拉消息、发消息、上传表情包）
api.deepseek.com        大模型（或你换的那家）
```

**② 时区必须是 UTC+8，这是最容易翻车的一项**

所有定时规则用的都是本机时间：

| | |
|---|---|
| 早上加油 | 8:30 |
| 下午加油 | 14:00 |
| 活跃时段 | 8:30–20:30 |
| 业绩通报 | 11:50 / 21:00 |

云服务器默认常是 UTC，不改的话**全部差 8 小时**——早上加油会在下午 4:30 发。

```bash
sudo timedatectl set-timezone Asia/Shanghai
```

systemd 配置里也设了 `TZ=Asia/Shanghai` 双保险。

**③ 服务器上要装中文字体**

通报图片要画中文，裸机 Linux 一个中文字体都没有，画出来全是方块。

```bash
sudo apt install -y fonts-wqy-zenhei      # Debian/Ubuntu
sudo yum install -y wqy-zenhei-fonts      # CentOS
```

---

## 配置要求

| | 建议 |
|---|---|
| 系统 | Ubuntu 22.04 / Debian 12 / CentOS 7+ 都行 |
| CPU | **2 核起**。OCR 是 CPU 密集的，一张照片约 4–5 秒 |
| 内存 | **2 GB 起**。onnxruntime 加载模型约占 500 MB |
| 磁盘 | 5 GB 够。主要是依赖和 OCR 模型 |
| Python | 3.10+（开发用的 3.11） |

OCR 单线程约 4–5 秒一张。如果高峰期一分钟涌进十几张照片，
2 核会排队；照片量大就上 4 核。

---

## 部署步骤

### 1. 建目录和用户

```bash
sudo useradd -r -m -d /opt/feishu-bot -s /bin/bash botuser
sudo mkdir -p /opt/feishu-bot
sudo chown botuser:botuser /opt/feishu-bot
```

### 2. 传代码

把整个项目目录传上去（**不要传 `.venv`**，那是 Windows 的，用不了）：

```bash
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
      ./ botuser@服务器:/opt/feishu-bot/
```

**这些文件必须一起传**，否则机器人会当作全新开始：

```
config.yaml            规则和话术
.env                   凭证（注意权限，见下）
assets/stickers/       表情包图库
roster.json            名册
tally.json             当日战报计数
campaign.json          活动订单明细
stickers_cache.json    media_id 缓存（可以不传，会自动重传）
stickers_state.json    定时发送状态（不传的话当天可能重发一次）
```

### 3. 装依赖

```bash
cd /opt/feishu-bot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

`wecom-aibot-python-sdk` 是从 GitHub 装的，服务器要能访问 github.com。
访问不了的话，在本地 `pip download` 后传上去装。

### 4. 收紧凭证权限

`.env` 里有企微 apikey、机器人 Secret、大模型 key：

```bash
chmod 600 /opt/feishu-bot/.env
chown botuser:botuser /opt/feishu-bot/.env
```

### 5. 自检

**启动前先跑这个**，十项全过再启动：

```bash
cd /opt/feishu-bot && .venv/bin/python deploy/preflight.py
```

会检查依赖、时区、凭证、配置、字体、企微连通、大模型连通。
任何一项 ✗ 都先解决，别硬启。

### 6. 装成服务

```bash
sudo cp deploy/feishu-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feishu-bot
sudo systemctl status feishu-bot
```

看实时日志：

```bash
sudo journalctl -u feishu-bot -f
```

启动成功的话，日志里会依次出现：

```
连接企业微信 MCP 服务器……
MCP 服务器提供 6 个工具：chat_groups_list, ...
工具映射：{'chat_list': 'chat_groups_list', ...}
长连接已就绪，表情包功能可用
表情包定时任务已启动：活跃时段 周四五六日 08:30-20:30
企业微信轮询启动：间隔 15s，重叠窗 60s
```

---

## 日常运维

**改配置不用重启** —— `config.yaml` 开了热更新，改完存盘就生效。
改 `.env`（换凭证）才需要重启：

```bash
sudo systemctl restart feishu-bot
```

**备份状态文件**。`campaign.json` 存着活动期间每一单的明细，丢了要重新录：

```bash
# 每天 3 点备份，保留 30 天
0 3 * * * cd /opt/feishu-bot && tar czf backup/state-$(date +\%F).tgz \
          *.json && find backup -name 'state-*.tgz' -mtime +30 -delete
```

**日志**默认进 journald，自带轮转。如果你重定向到文件，用
`deploy/logrotate.conf`。

---

## 排查

| 症状 | 先看什么 |
|---|---|
| 机器人不回消息 | `journalctl -u feishu-bot -n 100`，看有没有 errcode |
| 能看到报单但不回 | 多半是 `853008`——群不是机器人的「最近会话」，让人 @ 一下机器人 |
| 定时的表情包不发 | 时区对不对（`timedatectl`）；今天是不是在 `active_days` 里 |
| 通报图片是方块 | 没装中文字体 |
| 发不了表情包 | 长连接没起来，看日志有没有「长连接已就绪」 |
| 反复重启 | `systemctl status` 看退出码；5 分钟失败 5 次会自动停，是配置问题 |

---

## 一个提醒

活动期间**别同时在两台机器上跑**（比如本地调试忘了关，服务器也在跑）。
两个进程会各自轮询、各自回复，群里会收到双份点赞，
而且 `tally.json` / `campaign.json` 各存各的，统计会对不上。
