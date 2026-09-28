#!/usr/bin/env bash
# 服务器上发布（09-28 起在服务器开发）：在开发目录 /opt/feishu-bot-dev 改完、提交后执行
#     bash deploy/release.sh                 跑全套测试 -> 全过才发布到 /opt/feishu-bot 并重启 + 看启动日志
#     bash deploy/release.sh --no-restart    发布但不重启
#     bash deploy/release.sh --rev a1b2c3d   退回某个旧版本（不跑测试）
#
# 正式目录 /opt/feishu-bot 只由这个脚本（或本机的 push.sh）写，不要在里面直接改代码：
# 机器人断线看门狗随时会重启进程，撞上改了一半的代码就起不来。
# 配置和数据在 /opt/feishu-bot/data，这里不碰。
set -euo pipefail
cd "$(dirname "$0")/.."

APP=/opt/feishu-bot
PY=/opt/feishu-bot/.venv/bin/python
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

# 只从 main 发布：新版本在别的分支上开发（09-29 国庆版在 guoqing 分支），没做完别误发上线。
# 真要发某个分支，先合进 main
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$REV" == HEAD && "$BRANCH" != main ]]; then
  echo "!! 当前在分支 $BRANCH，只能从 main 发布（先 git checkout main；新版本做完合进 main 再发）"
  exit 1
fi

if [[ "$REV" == HEAD ]]; then
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "!! 有没提交的修改，先提交再发布："
    git status --short
    exit 1
  fi
  echo "== 全套测试"
  if ! "$PY" tests/run_all.py; then
    echo "!! 测试没全过，不发布"
    exit 1
  fi
fi

COMMIT=$(git rev-parse --verify "$REV^{commit}")
VERSION="$(git log -1 --format='%h %cd %s' --date=format:'%Y-%m-%d %H:%M' "$COMMIT" | tr -d "'")"
mapfile -t HAVE < <(git ls-tree --name-only "$COMMIT" -- "${PATHS[@]}")

echo "== 发布 $VERSION -> $APP"
TMP=$(mktemp -d)
git archive --format=tar "$COMMIT" -- "${HAVE[@]}" | tar -xf - -C "$TMP"
rm -rf "$APP/botcore"
cp -a "$TMP/." "$APP/"
rm -rf "$TMP"
printf '%s\n' "$VERSION" > "$APP/VERSION"

if [[ "$REV" == HEAD ]]; then
  git push -q origin HEAD:main || echo "!! 同步到服务器代码仓库失败（不影响发布）"
else
  echo "== 这是退回旧版本：正式目录现在是 $VERSION；开发目录没动，修好后正常提交再发布就回到最新"
fi

if [[ $RESTART == 0 ]]; then
  echo "== 已发布，未重启"
  exit 0
fi
echo "== 重启"
systemctl restart feishu-bot
sleep 15
systemctl is-active feishu-bot
journalctl -u feishu-bot --since '-20s' --no-pager -o cat | tail -12
