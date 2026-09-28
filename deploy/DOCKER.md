# Docker 部署

## 目录结构

代码在镜像里（只读），数据挂在 `/data` 卷里（持久化）。
重建镜像不会冲掉统计数据。

```
服务器上：
/opt/feishu-bot/
├── docker-compose.yml
└── data/                    ← 挂载到容器 /data
    ├── config.yaml          规则和话术（改完即生效，不用重启）
    ├── .env                 凭证（chmod 600）
    ├── roster.json          名册
    ├── assets/stickers/     表情包图库
    └── *.json               状态文件，自动生成
```

## 方式一：在服务器上构建（推荐）

服务器能访问 GitHub 和 PyPI 就用这个。

```bash
mkdir -p /opt/feishu-bot && cd /opt/feishu-bot
# 传代码上来（不含 .venv）
rsync -av --exclude='.venv' --exclude='__pycache__' ./ 服务器:/opt/feishu-bot/

# 准备数据目录
mkdir -p data/assets
cp config.yaml roster.json data/
cp .env data/ && chmod 600 data/.env
cp -r assets/stickers data/assets/
# 有历史数据的话一并放进去
cp tally.json campaign.json stickers_state.json data/ 2>/dev/null

docker compose up -d --build
docker compose logs -f
```

## 方式二：本地构建，打包传过去

服务器上不了外网时用这个。

```bash
# 本地（要有 Docker）
docker build -t feishu-bot:latest .
docker save feishu-bot:latest | gzip > feishu-bot.tar.gz     # 约 800MB

# 传到服务器
scp feishu-bot.tar.gz 服务器:/opt/feishu-bot/

# 服务器上
cd /opt/feishu-bot
gunzip -c feishu-bot.tar.gz | docker load
docker compose up -d          # 注意去掉 --build，直接用载入的镜像
```

用方式二时，把 `docker-compose.yml` 里的 `build: .` 那行删掉，
只留 `image: feishu-bot:latest`。

## 启动前自检

```bash
docker compose run --rm bot python deploy/preflight.py
```

十项全过再 `up -d`。这条命令用的是同一个镜像和同一份数据卷，
所以查出来的就是真实运行时的情况。

## 日常操作

```bash
docker compose logs -f              # 看日志
docker compose restart              # 重启（换了 .env 才需要）
docker compose down                 # 停
docker compose up -d --build        # 改了代码后重建
```

**改 `config.yaml` 不用重启也不用重建** —— 热更新开着，
而且它在挂载卷里，宿主机上改完容器里立刻能读到。

**改表情包图片同理** —— 往 `data/assets/stickers/` 里加图就行。

## 备份

`data/campaign.json` 存着活动期间每一单的明细，丢了要重新录。

```bash
# 每天 3 点备份，留 30 天
0 3 * * * cd /opt/feishu-bot && tar czf backup/data-$(date +\%F).tgz data/*.json \
          && find backup -name 'data-*.tgz' -mtime +30 -delete
```

## 排查

| 症状 | 原因 |
|---|---|
| 启动就退出 | `docker compose logs` 看；多半是 `.env` 没挂进去或缺字段 |
| 定时任务时间不对 | 容器时区。`docker compose exec bot date` 应该显示 UTC+8 |
| 通报图片是方块 | 字体没装进镜像，重新 build |
| 表情包发不出去 | 长连接没起，看日志有没有「长连接已就绪」 |
| 数据丢了 | 卷没挂对。`docker compose exec bot ls /data` 确认 |

## 两个提醒

**别同时跑两份**。容器和本地 Python 同时在跑的话，群里会收到双份点赞，
而且两边状态文件各存各的，统计对不上。

**`.env` 不要打进镜像**。`.dockerignore` 里已经排除了，
凭证只通过挂载卷进容器。
