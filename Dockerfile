# 使用 Python 3.9 slim 环境作为基础镜像
FROM python:3.9-slim-bookworm AS builder

# 1. 零网络开销安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# 2. 设置环境变量优化构建行为
# UV_COMPILE_BYTECODE=1: 编译 .pyc 文件，显著提升容器启动速度
# UV_LINK_MODE=copy: 强制使用复制模式而不是硬链接，避免跨层复制文件时的潜在问题
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 3.设置工作目录
WORKDIR /app

# . 利用 Docker 缓存层：先只复制依赖定义
COPY pyproject.toml uv.lock ./

# 4. 挂载构建缓存并安装依赖
# --mount=type=cache: 缓存 uv 下载的包，下次构建秒级完成
# --no-install-project: 先只装第三方库，不装当前项目（利用 Layer 缓存）
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 5. 复制源代码
COPY . .

# 6. 安装当前项目
# 这一步非常快，因为它只会安装你的代码，不会重新下载依赖
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev
# Stage 2: Runner (生产环境),指定基于 Debian 12(Bookworm)
FROM python:3.9-slim-bookworm

COPY --from=builder /bin/uv /usr/local/bin/uv

# 7. SRE 最佳实践：设置 Python 环境变量
# PYTHONUNBUFFERED=1: 保证日志直接输出到控制台，不被缓存（对 Docker logs 至关重要）
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 8. 直接复制构建好的虚拟环境
# 相比于“安装 wheel”，直接复制文件夹速度更快，且保证环境完全一致
COPY --from=builder /app/.venv /app/.venv

# 9. 复制源代码 (如果你的项目包含非 Python 文件或需要运行时读取源码)
# 如果你的项目完全打包进库里了，这一步可以视情况省略，但通常建议保留以防万一
COPY --from=builder /app /app

# 复制配置模板
COPY config.ini /app/config.ini.template
COPY bangumi_mapping.json /app/bangumi_mapping.json.template

# 创建启动脚本
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
# 创建必要目录\n\
mkdir -p /app/config /app/logs /app/data /app/config_backups\n\
\n\
# 检查配置文件是否存在，不存在则从模板复制\n\
if [ ! -f "/app/config/config.ini" ]; then\n\
    echo "配置文件不存在，从模板创建..."\n\
    cp /app/config.ini.template /app/config/config.ini\n\
    \n\
    # Docker环境下自动调整路径配置\n\
    echo "调整Docker环境路径配置..."\n\
    sed -i "s|local_cache_path = ./bangumi_data_cache.json|local_cache_path = /app/data/bangumi_data_cache.json|g" /app/config/config.ini\n\
    sed -i "s|log_file = ./log.txt|log_file = /app/logs/log.txt|g" /app/config/config.ini\n\
    \n\
    echo "配置文件已创建并调整：/app/config/config.ini"\n\
fi\n\
\n\
# 检查自定义映射文件是否存在，不存在则从模板复制\n\
if [ ! -f "/app/config/bangumi_mapping.json" ]; then\n\
    echo "自定义映射文件不存在，从模板创建..."\n\
    cp /app/bangumi_mapping.json.template /app/config/bangumi_mapping.json\n\
    echo "自定义映射文件已创建：/app/config/bangumi_mapping.json"\n\
fi\n\
\n\
# 检查邮件通知模板文件是否存在，不存在则从默认模板复制\n\
if [ ! -f "/app/config/email_notification.html" ]; then\n\
    echo "邮件通知模板不存在，从默认模板创建..."\n\
    cp /app/templates/email_notification.html /app/config/email_notification.html\n\
    echo "邮件通知模板已创建：/app/config/email_notification.html"\n\
    echo "提示：可以编辑此文件自定义邮件通知样式"\n\
fi\n\
\n\
# 确保日志文件存在并有正确权限\n\
touch /app/logs/log.txt\n\
chmod 666 /app/logs/log.txt\n\
\n\
# 显示配置信息用于调试\n\
echo "=== 配置信息 ==="\n\
echo "配置文件: $CONFIG_FILE"\n\
echo "工作目录: $(pwd)"\n\
echo "Python路径: $PYTHONPATH"\n\
ls -la /app/config/ /app/logs/ /app/data/ || true\n\
echo "==============="\n\
\n\
# 启动应用\n\
echo "启动应用..."\n\
exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log' > /app/start.sh && chmod +x /app/start.sh

# 设置环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV CONFIG_FILE=/app/config/config.ini
ENV DOCKER_CONTAINER=true

# 暴露端口8000
EXPOSE 8000

# 使用启动脚本
CMD ["/app/start.sh"] 