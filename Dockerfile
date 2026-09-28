# ============================================================
# 业绩点赞机器人
#
# 两阶段构建：builder 装依赖（要 git 和编译工具），运行阶段只带venv。
# 这样最终镜像不含编译链，小一半左右。
#
# 关键点：
#   · 时区必须 Asia/Shanghai —— 所有定时规则用的是本机时间
#   · 要装中文字体 —— 通报图片画中文，否则全是方块
#   · onnxruntime 需要 libgomp1
#   · 代码在镜像里（只读），数据在 /data 卷里（持久化）
# ============================================================

# ---------- 构建阶段 ----------
FROM python:3.11-slim AS builder

# git：aibot SDK 是从 GitHub 装的
# build-essential：个别包没有 wheel 时要现编
RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---------- 运行阶段 ----------
FROM python:3.11-slim

# libgomp1   onnxruntime 依赖，不装的话 import 就崩
# fonts-wqy-zenhei  中文字体，通报图片要用
# tzdata     时区数据
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 fonts-wqy-zenhei tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    BOT_DATA_DIR=/data

# 非 root 运行
RUN useradd -r -u 10001 -m botuser

WORKDIR /app
COPY --chown=botuser:botuser bot.py ./
COPY --chown=botuser:botuser botcore/ ./botcore/
COPY --chown=botuser:botuser deploy/ ./deploy/

# 数据目录：config.yaml / .env / assets/stickers / 各种状态 json 都在这
# 【必须挂卷】，否则容器一删数据全没
RUN mkdir -p /data && chown botuser:botuser /data
VOLUME ["/data"]

USER botuser

# 健康检查：进程还在、且能读到配置。企微那边不查，避免频繁调接口
HEALTHCHECK --interval=5m --timeout=20s --start-period=60s --retries=3 \
    CMD python -c "from botcore.config import ConfigStore; ConfigStore()" || exit 1

CMD ["python", "bot.py"]
