# vococo 服务器版镜像(VOCOCO_MODE=server)
# 构建:docker build -t vococo-server .
# 运行:docker run -d --name vococo -p 8848:8848 -v vococo-data:/app/data \
#         --env-file .env.server --restart always vococo-server
FROM python:3.12-slim

# 系统依赖:git(agent 在租户沙箱里可能做版本操作)+ ca-certificates(HTTPS)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# uv 管依赖(与本地开发同一条路径,锁文件保证一致)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
# 先拷依赖清单充分利用构建缓存;--locked 严格按 uv.lock 装,不带 dev
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY vococo/ ./vococo/
COPY agents/ ./agents/
RUN uv sync --locked --no-dev

# 非 root 运行:claude CLI 拒绝以 root 使用 --dangerously-skip-permissions
# (2026-08-06 首次部署实测崩 root)。数据目录先 chown,docker 命名卷首次挂载
# 会继承镜像里的属主。
RUN useradd -m -u 10001 vococo \
    && mkdir -p /app/data \
    && chown -R vococo:vococo /app
USER vococo

# 运行时数据(平台库 + 租户目录)全部落 /app/data,挂卷持久化
VOLUME ["/app/data"]

ENV VOCOCO_MODE=server \
    WEB_ENABLED=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8848

EXPOSE 8848

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8848/healthz || exit 1

CMD ["uv", "run", "--no-sync", "vococo", "serve"]
