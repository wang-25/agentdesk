# -*- coding: utf-8 -*-
# ============================================================
#  AgentDesk 镜像
# ------------------------------------------------------------
#  构建：docker build -t agentdesk:1.0.0 .
#  运行：由 docker-compose.yml 负责，不单独 docker run
#
#  设计要点：
#    1. 用 slim 而不是 alpine —— numpy / langgraph 这些包在 alpine 的
#       musl 环境里没有预编译 wheel，需要现场编译，构建慢且体积更大。
#    2. 依赖先拷、代码后拷 —— 改代码不会让 pip 那层缓存失效，重建快。
#    3. 非 root 运行 —— 容器里跑的是别人能通过 HTTP 触发的代码，
#       用 root 跑等于把宿主机的命门交出去。
#    4. --workers 1 是硬要求，见文件末尾说明。
# ============================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata：容器默认 UTC，日志时间戳会比北京时间差 8 小时，排查时极易误判
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层：只要 requirements.txt 不变，这层就一直命中缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 代码层
COPY app/ ./app/
COPY data/ ./data/
COPY eval/ ./eval/
COPY scripts/ ./scripts/
COPY docs/ ./docs/
COPY README.md ./
COPY .env.example ./

# 非 root 用户 + 可写的日志目录
RUN useradd -m -u 10001 -s /usr/sbin/nologin agent \
    && mkdir -p /app/logs \
    && chown -R agent:agent /app

USER agent

EXPOSE 8000

# 健康检查：slim 镜像里没有 curl，用 python 标准库探活
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

# 【为什么必须 --workers 1】
# app/security.py 的限流和每日额度计数是**进程内内存**的。
# 开多个 worker 的话每个进程各算一份，实际额度会翻 N 倍，限流形同虚设。
# 要扩到多 worker，得先把计数器挪到 Redis。
#
# 【为什么 --proxy-headers】
# 前面挡着 Nginx Proxy Manager，真实客户端 IP 在 X-Forwarded-For 里。
# 加上这个参数，uvicorn 才会采信这些头。
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
