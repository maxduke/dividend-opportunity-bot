# --- Stage 1: Builder ---
# 使用一个更新的、受支持的基础镜像
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

# 安装构建依赖并清理缓存
# 对于 Debian Bookworm, build-essential 通常是足够的
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

# 首先只复制 requirements.txt 以利用 Docker 缓存
COPY requirements.txt .

# 直接安装到可复制的前缀，避免运行时镜像保留 wheel 层和 .pyc
RUN pip install --no-cache-dir --no-compile --prefix=/install -r requirements.txt

# --- Stage 2: Final Image ---
# 确保最终镜像和构建镜像使用相同的基础
FROM python:3.12-slim-bookworm

# 创建固定 UID/GID 的非 root 用户，便于 bind mount 在宿主机上预先授权
RUN groupadd --gid 10001 appuser && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

# 运行时镜像只保留已安装依赖
COPY --from=builder --chown=appuser:appuser /install /usr/local

# 创建数据目录并设置权限
RUN mkdir -p /app/data && \
    chown appuser:appuser /app/data
VOLUME /app/data

# 复制应用代码
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser scripts/ ./scripts/

# 环境变量
ENV DB_FILE=/app/data/rules.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NUMBA_CACHE_DIR=/tmp/numba_cache

# 切换到非特权用户
USER appuser

# 入口点
CMD ["python", "-m", "src.main"]
