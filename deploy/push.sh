#!/usr/bin/env bash
# 把本机改好的【代码】推到服务器并重启机器人。在 D:\feishu_bot 下用 Git Bash 执行：
#     bash deploy/push.sh                 推当前提交（HEAD）+ 重启 + 看启动日志
#     bash deploy/push.sh --no-restart    只推不重启
#     bash deploy/push.sh --rev a1b2c3d   退回：把服务器换成某个旧版本（版本号看 git log --oneline）
#
# 09-28 起代码进了 git：推的是【已提交】的版本（git archive），有没提交的修改会拒绝推，
# 保证服务器上跑的每一版都有记录。服务器 /opt/feishu-bot/VERSION 记着现在跑的是哪一版，机器人启动日志也会打出来。
# 推 HEAD 时顺便把提交同步到服务器上的代码仓库 /opt/feishu-bot.git 和外部仓库（配了 origin 才推）。
#
# 只推代码。服务器上的配置和数据都在 /opt/feishu-bot/data，这里【不碰】：
#     config.yaml / .env / roster.json / campaign.json / 各种状态文件 / 表情包 / 打卡样本
# 改 config.yaml 请直接改服务器上那份（热加载，不用重启）：
#     ssh root@47.109.158.170  然后  nano /opt/feishu-bot/data/config.yaml
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=root@47.109.158.170
APP=/opt/feishu-bot
PATHS=(botcore deploy teleagent docs bot.py mcp_server.py diagnose.py preflight.py
       wecom_discover.py test_bulletin.py test_llm.py test_parse.py test_rules.py
       requirements.txt README.md 常用命令.txt .env.example .gitignore)

REV=HEAD
RESTART=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-restart) RESTART=0 ;;
    --rev) REV="${2:?--rev 后面要跟版本号}"; shift ;;
    *) echo "不认识的参数：$1"; exit 1 ;;
  esac
  shift
done

if [[ "$REV" == HEAD ]]; then
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "!! 有没提交的修改，先提交再推（git status 看是哪些）："
    git status --short
    exit 1
  fi
  echo "== 语法检查"
  .venv/Scripts/python -m compileall -q botcore bot.py mcp_server.py deploy/preflight.py >/dev/null
fi

COMMIT=$(git rev-parse --verify "$REV^{commit}")
VERSION="$(git log -1 --format='%h %cd %s' --date=format:'%Y-%m-%d %H:%M' "$COMMIT" | tr -d "'")"
# 旧版本里可能还没有某些文件，只打包那一版里有的
mapfile -t HAVE < <(git ls-tree --name-only "$COMMIT" -- "${PATHS[@]}")

echo "== 上传 $VERSION -> $HOST:$APP"
git archive --format=tar "$COMMIT" -- "${HAVE[@]}" | gzip \
  | ssh -o BatchMode=yes "$HOST" "rm -rf $APP/botcore && tar -xzf - -C $APP && printf '%s\n' '$VERSION' > $APP/VERSION"

if [[ "$REV" == HEAD ]]; then
  echo "== 同步提交到服务器代码仓库"
  git push -q server HEAD:main || echo "!! 服务器代码仓库同步失败（不影响部署）"
  if git remote get-url origin >/dev/null 2>&1; then
    echo "== 同步提交到外部仓库"
    git push -q origin HEAD:main || echo "!! 外部仓库同步失败（不影响部署），网络好了再 git push origin main"
  fi
else
  echo "== 这是退回旧版本：服务器现在跑 $VERSION；本机代码没动，修好后正常提交再推就回到最新"
fi

if [[ $RESTART == 0 ]]; then
  echo "== 已上传，未重启"
  exit 0
fi

echo "== 重启"
ssh -o BatchMode=yes "$HOST" "systemctl restart feishu-bot && sleep 15 && systemctl is-active feishu-bot \
  && journalctl -u feishu-bot --since '-20s' --no-pager -o cat | tail -12"
