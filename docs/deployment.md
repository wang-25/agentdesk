# 部署到公网（Day 10）

把本地跑通的 AgentDesk 部署到一台 2 核 2G 的阿里云 ECS 上，通过 HTTPS 域名对外服务。

本文记录的是**真实部署过程**，包含踩到的约束和当时的判断依据，不是理想化的教程。

---

## 一、这台机器的处境

部署前先量了一遍，因为「能不能装」不是凭感觉猜的：

```
CPU     2 核
内存    1870 MB 总量，已用 1511 MB，可用 359 MB
Swap    0                     ← 没有 swap
磁盘    40G，已用 11G

已运行的容器（7 个）：
  wp-app          173.9 MB   WordPress 应用
  wp-npm           97.9 MB   Nginx Proxy Manager（占着 80/443/81）
  zabbix-web       76.1 MB
  zabbix-server    33.3 MB
  zabbix-mysql    492.0 MB   ← 全家最大户
  wp-db           138.9 MB
  zabbix-agent      4.2 MB
```

**结论：瓶颈是内存，不是 CPU，也不是磁盘。**

CPU 只花在 jieba 分词和 BM25 上，毫秒级；磁盘还有 24G。
真正的问题是**可用内存只剩 359MB，而且 swap 是 0**。

这里有个容易被忽略的机制：**swap 为 0 时，物理内存耗尽不会让程序变慢，而是直接触发 OOM killer**。
Linux 会挑一个"最值钱"的进程杀掉 —— 在这台机器上很可能是 `zabbix-mysql`（492MB）
或 `wp-db`（139MB）。表现是**博客突然 502，而且 dmesg 之外看不出是谁干的**。

所以第一件事不是部署，是消除这个悬崖。

---

## 二、先垫 swap，再谈部署

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

# 默认 swappiness=60 会"过早"换出，机械/云盘上反而拖慢响应。
# 设成 10：平时不动，只在真正吃紧时才用 —— 这是"保险丝"而不是"常用内存"。
sysctl -w vm.swappiness=10
grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
```

写进 `/etc/fstab` 是为了重启后还在。**只 `swapon` 不写 fstab 的话，下次重启就白做了。**

---

## 三、公网暴露意味着什么

这是本次部署真正需要认真对待的部分。

AgentDesk 的接口里有两类东西：

| 接口 | 被滥用的后果 |
|---|---|
| `POST /agent/ask`、`/chat`、`/rag/ask` | 刷你的模型余额 |
| `POST /approvals/{id}/execute` | **在服务器上执行写操作** |
| `GET /traces`、`/audit`、`/metrics/summary` | 泄露全部执行记录与成本 |

写代码的时候（Day 0-9）这些都是"本地测试用"，`main.py` 里没有任何鉴权。
一旦挂到公网、地址出现在简历上，**任何人扫到这个端口就能用你的钱、批你的命令**。

所以上线前补了 `app/security.py`，三层：

### 3.1 白名单，不是黑名单

```python
PUBLIC_EXACT = {"/", "/health", "/openapi.json", "/favicon.ico"}
PUBLIC_PREFIX = ("/docs", "/redoc")
```

黑名单（只保护已知的敏感接口）有个致命缺陷：**以后新加的接口默认是公开的**。
哪天加了个 `/admin/reset` 忘了登记，就是一次静默越权。

白名单反过来 —— 默认全拒，只放行明确要公开的。**新接口天然安全**。

### 3.2 限流三层，因为单层挡不住

只看 IP 限流是**可以绕过的**：`X-Forwarded-For` 就是个普通 HTTP 头，客户端随便写。
每个请求换个假 IP，单 IP 限流立刻失效。

| 层 | 限的是什么 | 能挡住什么 |
|---|---|---|
| 每 IP 每分钟 20 次 | 单个来源 | 手滑狂点、单机脚本 |
| 全站每分钟 60 次 | 与身份无关的总速率 | 伪造 IP 的分布式刷 |
| 每日 300 次额度 | 与时间窗口无关的总量 | 慢慢刷、账单上限 |

第三层是关键：前两层都是"速率"限制，只有它是"总量"限制。
**速率限制能拖慢攻击，总量限制才能保证账单有上限。**

### 3.3 几个容易写错的细节

**token 比较必须用 `secrets.compare_digest`。**
普通的 `==` 在第一个不同字符处就短路返回，耗时随"匹配了多长前缀"变化。
攻击者测响应时间就能逐字节猜出 token。这是真实的侧信道攻击手法。

**取 XFF 要取最后一个值，不是第一个。**
Nginx 用 `$proxy_add_x_forwarded_for` 追加，格式是「客户端自带的, 真实IP」。
客户端伪造的部分留在**左边**。取第一个 = 把限流 key 交给攻击者控制。

**开了鉴权却没配 token 必须拒绝启动。**
只打警告然后继续跑，结果是「以为有保护、其实裸奔」—— 比明知道没保护更危险。

```python
if AUTH_ENABLED and not AGENT_TOKEN:
    raise RuntimeError("AUTH_ENABLED=1 但 AGENT_TOKEN 为空……")
```

### 3.4 自检

安全层的失效是**静默的**：不带 token 也能调通，日志上看一切正常，接口全返回 200。
所以专门写了"故意攻击自己"的脚本：

```bash
python scripts/security_check.py        # 两种模式都跑
```

23 项检查，包括「每请求换一个假 IP 仍被拦」这种专门针对绕过手法的用例。

---

## 四、网络拓扑

```
互联网
  │  https://agent.simosheng.fun
  ▼
┌─────────────────────────────────────────┐
│ Nginx Proxy Manager（已有，占 80/443/81）│
│  · 自动管理 Let's Encrypt 证书           │
│  · simosheng.fun      → wp-app:80        │
│  · agent.simosheng.fun → agentdesk:8000  │
└──────────────┬──────────────────────────┘
               │ docker 网络 wordpress-blog_wpnet
               ▼
       ┌────────────────┐
       │   agentdesk    │  127.0.0.1:8088 → 8000
       │  mem_limit 400m│  （公网扫不到这个端口）
       └────────────────┘
```

**关键决策一：复用 NPM，不自建 Nginx。**
80/443 已经被占用，而且 NPM 里已经有一张 `simosheng.fun` 的 Let's Encrypt 证书。
自己再起一套 Nginx 既冲突，又要重新处理证书续期。

**关键决策二：加入 NPM 所在的 docker 网络，按容器名互访。**

```yaml
networks:
  wpnet:
    external: true
    name: wordpress-blog_wpnet
```

这样 NPM 里填的后端地址就是 `agentdesk`，**不需要在任何地方写死 IP**。
容器重建后 IP 会变，但容器名不变。

**关键决策三：端口绑 `127.0.0.1` 而不是 `0.0.0.0`。**

```yaml
ports:
  - "127.0.0.1:8088:8000"    # 不是 "8088:8000"
```

这一行的区别是整个部署里最重要的安全边界。
绑回环意味着这个端口**只在服务器本机可达**，公网扫不到。
流量只能走 NPM 那条路（有 HTTPS、有日志、有 WAF 能力）。

如果绑 `0.0.0.0`，就等于开了一条绕过 NPM 的旁路：
别人可以 `http://IP:8088` 直接打进来，前端加的所有防护形同虚设。

留着它只是为了能在服务器上 `curl localhost:8088` 排查问题。

**关键决策四：给容器设内存硬上限。**

```yaml
mem_limit: 400m
cpus: 1.0
```

不设的话，一旦 AgentDesk 内存失控，OOM killer 会从**整机**里挑进程杀。
设了上限，超了只杀它自己，**爆炸范围被限制在一个容器里**。

---

## 五、那个必须解释的取舍：沙箱用 mock

`SANDBOX_BACKEND` 有四个选项，部署到公网时选了最保守的 `mock`：

| 模式 | 怎么工作 | 为什么不选 |
|---|---|---|
| `docker` | 真容器隔离 | 要挂 `/var/run/docker.sock`。拿到它就能创建挂载宿主机根目录的特权容器 —— **token 泄露 = 整机沦陷** |
| `subprocess` | 在容器内真执行 | 命令能读到容器内的 `.env`，也就是 **API Key** |
| `auto` | 有 Docker 就用 Docker | 在服务器上等价于 `docker`，同样的问题 |
| **`mock`** | 仿真执行，不真跑 | **选它** |

理由很直接：**这个服务是要给陌生人访问的**（简历上的链接，面试官会点，可能有人乱试）。
而我的 token 鉴权是刚写的、没经过安全审计的自研代码 —— 拿它去守 root 级权限，风险收益不对等。

代价是演示效果打折。但"真 Docker 隔离"在本机已经实测验证过
（非 root / 根只读 / 无网络 / 内存封顶），面试时照样能讲、能放录屏。

**想开真隔离时**，改 `/opt/agentdesk/.env` 一行：

```bash
SANDBOX_BACKEND=subprocess   # 或 docker（需在 compose 里挂 docker.sock）
docker compose up -d
```

---

## 六、部署步骤

### 6.1 服务器侧

```bash
mkdir -p /opt/agentdesk
tar -xzf agentdesk.tar.gz -C /opt/agentdesk
```

生产 `.env` 权限设 600，且**不进任何仓库**：

```bash
chmod 600 /opt/agentdesk/.env
```

### 6.2 权限：容器内是 uid 10001

镜像里用非 root 用户运行。挂载出来的目录必须归属它，否则容器内写不了日志：

```bash
mkdir -p /opt/agentdesk/logs
chown -R 10001:10001 /opt/agentdesk/data /opt/agentdesk/logs
```

### 6.3 构建与启动

```bash
cd /opt/agentdesk
docker compose build      # 首次约 20 分钟
docker compose up -d
```

`pyproject` 的 pip 源换成阿里云镜像，构建快很多。

### 6.4 一个容易踩的坑：索引不在 git 里

`.gitignore` 里有 `data/index/` —— 索引被设计成"可随时重建"，所以没进仓库。
这意味着**如果从 GitHub clone 代码部署，启动会报索引不存在**。

打包上传时索引是带上的（走 tar，不走 git）。如果是全新环境，先重建：

```bash
docker exec agentdesk python -c "
from app.rag.pipeline import build_index; build_index()"
```

---

## 七、验证结果（实测）

### 7.1 资源占用

| 项目 | 数值 |
|---|---|
| 镜像大小 | 347 MB |
| 容器内存 | **144.3 MB** / 400 MB 上限（36%） |
| 容器 CPU | 0.15% |
| swap 使用 | 163 MB（在分担压力） |

对比部署前后：

| | 部署前 | 部署后 |
|---|---|---|
| 物理内存已用 | 1511 MB | 1544 MB |
| 可用 | 359 MB | 326 MB |
| swap 已用 | 0 | 163 MB |

物理内存只涨了 33MB，多出来的 114MB 被换到了 swap —— **这正是垫 swap 的意义**。

### 7.2 邻居没受影响

```
agentdesk       Up 2 minutes (healthy)     ← 新来的
wp-app          Up 3 weeks                 ← 运行时长未变
wp-npm          Up 3 weeks
zabbix-web      Up 3 weeks (healthy)
zabbix-agent    Up 3 weeks
zabbix-server   Up 3 weeks
zabbix-mysql    Up 25 hours
wp-db           Up 3 weeks (healthy)

WordPress   HTTP 301（正常跳转 HTTPS）
Zabbix      HTTP 200
```

**7 个原有容器运行时长一个都没变**，说明没有发生 OOM 重启。

### 7.3 鉴权边界

```
无 token 访问 /audit            → HTTP 401  ✓
错误 token 访问 /audit          → HTTP 401  ✓
正确 token 访问 /audit          → HTTP 200  ✓
NPM 容器内按名访问 agentdesk    → HTTP 200  ✓（反代链路通）
公网直连 101.200.219.134:8088   → 连接被拒  ✓（端口只绑回环）
```

### 7.4 真实功能

```
POST /rag/ask     HTTP 200   3.9s    带 [1][2][4][5] 引用溯源
POST /agent/ask   HTTP 200   8.8s    2 轮 / 6 次工具调用 / 7856 tokens
```

RAG 的回答里有一段值得注意：

> 参考资料中关于 `/etc/docker/daemon.json` 的具体配置内容被截断，未给出完整配置示例……
> 如需这两部分细节，知识库中暂无完整内容。

**没有编。** 库外内容明确说"知识库里没有"，这正是 Day 9 实测到的
「库外拒答 100%」在真实环境下的表现。

---

## 八、运维手册

```bash
cd /opt/agentdesk

docker compose ps                  # 状态
docker compose logs -f --tail=50   # 看日志
docker compose restart             # 重启
docker compose down                # 停止
docker compose up -d --build       # 改完代码重新构建

# 健康检查（含安全层状态：鉴权开没开、额度用了多少、拦了多少请求）
curl -s http://127.0.0.1:8088/health

# 带 token 调用
curl -s -H "X-API-Key: <你的 AGENT_TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"question":"web-01 磁盘满了怎么处理"}' \
     http://127.0.0.1:8088/rag/ask

# 内存是否吃紧
free -m

# 谁在内存里占最大
docker stats --no-stream
```

### 排查思路

| 症状 | 先看哪里 |
|---|---|
| 接口返回 401 | `/health` 里的 `security.token_configured` |
| 接口返回 429 | `/health` 里的 `daily_used`、`rejected` 计数 |
| 服务起不来 | `docker compose logs`，多半是索引与 embedder 后端不匹配 |
| 博客突然 502 | `free -m` + `dmesg \| tail -30` 看有没有 OOM killer |

---

## 九、待完成

1. **DNS**：给 `agent.simosheng.fun` 加一条 A 记录指向 `101.200.219.134`
   （目前只有 `simosheng.fun` 有解析，子域名不存在）。
2. **NPM 反代**：在 NPM 面板添加 Proxy Host，指向 `agentdesk:8000`，并申请证书。
   现有证书 `npm-3` 只覆盖 `simosheng.fun` / `www.simosheng.fun`，子域名需要新签一张。
3. **语义检索**：配 `DASHSCOPE_API_KEY` 后重建索引。
   注意 emebdding 从 512 维（哈希）变 1024 维（百炼），
   `VectorStore.load()` 会因为维度不匹配**主动拒绝加载旧索引** —— 这是设计如此，
   必须重建。重建后 Day 9 那份评测报告需要重跑。
