# ==========================================
# Stage 1: Builder (依赖构建层)
# ==========================================
FROM python:3.11-slim-bookworm AS builder

# 1. 获取 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# 2. 环境变量
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT="/usr/local"

WORKDIR /app

# 3. 安装依赖 + 项目本身（安装到 site-packages，Runtime 无需 PYTHONPATH）
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev


# ==========================================
# Stage 2: Runtime (运行层)
# ==========================================
FROM python:3.11-slim-bookworm

# 1. 环境变量
ENV PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=true \
    BS_BASE_URL=http://bs:8000 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=3000

WORKDIR /app

# 2. 安全优化：创建非 Root 用户，支持通过环境变量 PUID/PGID 指定用户ID
ARG PUID=1000
ARG PGID=1000
RUN groupadd -g ${PGID} appuser && useradd -u ${PUID} -g appuser --create-home appuser

# 3. 安装 gosu 用于用户切换
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

# 4. 从 Builder 拷贝依赖
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 5. 拷贝业务代码
COPY --chown=appuser:appuser src/ ./src/

# 6. 暴露端口
EXPOSE 3000

# 7. 启动（gosu 切用户后由 python -m mcp_server.server 启动 Streamable HTTP）
CMD ["gosu", "appuser", "python", "-m", "mcp_server.server"]
