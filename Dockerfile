# 两阶段构建：builder 装依赖，runtime 只带走虚拟环境与代码
# 这样最终镜像里没有编译器、没有 pip 缓存，体积与攻击面都更小

# 基础镜像走镜像源：本机直连 docker.io 会超时，与 compose 里 pgvector 的做法一致
# 海外环境可 --build-arg BASE_IMAGE=python:3.13-slim 覆盖
ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim

FROM ${BASE_IMAGE} AS builder

# 国内网络直连 PyPI 较慢，构建时可通过 --build-arg 覆盖
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 依赖列表与 pyproject.toml 保持一致；只拷 pyproject 是为了让这一层能被缓存
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        "fastapi>=0.110" \
        "python-multipart>=0.0.9" \
        "uvicorn[standard]>=0.30" \
        "httpx>=0.27" \
        "pypdf>=4.0" \
        "python-docx>=1.1" \
        "python-pptx>=1.0" \
        "openpyxl>=3.1" \
        "numpy>=1.26" \
        "pydantic>=2.0" \
        "sqlalchemy>=2.0" \
        "alembic>=1.13" \
        "psycopg[binary]>=3.1" \
        "pgvector>=0.3"


ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
FROM ${BASE_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# 非 root 运行：容器被攻破时降低影响面
RUN useradd --create-home --shell /usr/sbin/nologin appuser
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY src ./src
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./

# named volume 首次挂载会继承镜像内目录的属主；预建并 chown 后，
# 非 root 的 api/worker 才能写入共享上传目录。
RUN mkdir -p /app/data/uploads && chown -R appuser:appuser /app/data

USER appuser
EXPOSE 8000

# 用 /live 而不是 /ready：健康检查失败会重启容器，不能因为数据库抖动被反复杀
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/live', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
