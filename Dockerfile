# Stage 1: Builder
FROM python:3.9-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# 依赖安装缓存层
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ==========================================

# Stage 2: Runner (生产环境)
FROM python:3.9-slim-bookworm

# 1. 安全优化：创建非 Root 用户
# --create-home (-m) 强制创建 /home/appuser 目录
RUN groupadd -r appuser && useradd -r -g appuser --create-home appuser

# 3. 环境变量
# 将 venv 加入 PATH，确保脚本能找到 uvicorn
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DOCKER_CONTAINER=true \
    CONFIG_FILE=/app/config/config.ini

WORKDIR /app

# 4. 复制依赖和代码 (设置所有者为 appuser)
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app /app

# 5. 复制配置文件模板
COPY config.ini /app/config.ini.template
COPY bangumi_mapping.json /app/bangumi_mapping.json.template

# 6. 【关键优化】复制外部脚本，而不是在 Dockerfile 里 echo
# 记得在本地给 entrypoint.sh 加上执行权限 (chmod +x entrypoint.sh)
COPY --chown=appuser:appuser entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 7. 预创建目录并授权
# 因为随后切换成 appuser，它没有权限在根目录 mkdir，
# 所以必须先由 root 创建好这些挂载点，并把所有权交给 appuser
RUN mkdir -p /app/config /app/logs /app/data /app/config_backups && \
    chown -R appuser:appuser /app

# 8. 切换用户
USER appuser

# 暴露端口
EXPOSE 8000

# 启动
CMD ["/app/entrypoint.sh"]