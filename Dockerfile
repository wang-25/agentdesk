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

# ★ openssh-client 单独一层，**特意放在 pip 之后**。
#
#   为什么单独放（一次实测换来的）：把它塞进上面那个 apt 层，会让 pip 层的
#   缓存**全部失效** —— 父层一变，下游每一层都要重建。实测改一行 apt，
#   就要重装全部 Python 依赖，跑了 9 分钟还在下载（几百 MB）。
#
#   分开之后：改 apt 只重建这一层（几 MB，秒级）；改依赖只重建 pip 那一层。
#   **变化频率完全不同的两件事，不该塞进同一层。**
#
#   顺带换国内源：python:3.12-slim 默认用 deb.debian.org，在阿里云 ECS 上
#   拉索引要好几分钟；mirrors.aliyun.com 走内网，快得多。
#   （只在这一层换 —— 上面那层保持原样，才不破坏它的缓存。）
RUN set -e; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        if [ -f "$f" ]; then \
            sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' "$f"; \
        fi; \
    done; \
    apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

# 代码层
COPY app/ ./app/
COPY data/ ./data/
COPY eval/ ./eval/
COPY scripts/ ./scripts/
COPY docs/ ./docs/
COPY README.md ./
COPY .env.example ./

# 非 root 用户 + 可写的日志目录 + ~/.ssh
#
# ★ 为什么必须预先建 /home/agent/.ssh：
#   ssh 后端第一次连一台新主机时会写 known_hosts（StrictHostKeyChecking=accept-new）。
#   如果这个目录不存在，写会静默失败 —— **每一次连接都会被当成"首次"**，
#   于是 accept-new 退化成无条件接受，中间人检测形同关闭。
#   （实测表现：连接完全正常，不报任何错 —— 又一处"静默失效"。）
RUN useradd -m -u 10001 -s /usr/sbin/nologin agent \
    && mkdir -p /app/logs /home/agent/.ssh \
    && chmod 700 /home/agent/.ssh \
    && chown -R agent:agent /app /home/agent

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
