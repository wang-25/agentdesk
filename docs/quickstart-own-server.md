# Quickstart：部署到你自己的机器

> AgentDesk 默认连的是**仿真数据**（不碰任何真实机器），clone 下来就能跑通全链路。
> 要让它查**你自己的服务器**，按下面三步配 —— 全程只读命令，写操作走审批 + 沙箱。
>
> ⚠️ 只连**你自己有权限的机器**，私钥用**专用的**那一把 —— 本项目不提供、
> 也不应该提供"代管别人服务器凭据"的能力（那是另一类产品，安全模型完全不同）。

## 前置条件

- Python 3.10+（Linux / macOS / Windows 均可）
- 一台你能 SSH 登录的 Linux 服务器（查别的机器时才需要；只跑仿真不用）
- 可选：Docker（用于写操作的沙箱隔离执行）

## 第 0 步：跑通仿真模式（1 分钟，验证环境）

```bash
git clone https://github.com/wang-25/agentdesk.git && cd agentdesk
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                 # Windows: copy .env.example .env
# 编辑 .env：填 DEEPSEEK_API_KEY（其余先不动，OPS_BACKEND 默认 mock）

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000/try —— 填个问题就能跑，此时查的是内置仿真主机。

## 第 1 步：让它查你自己的服务器

在**能 SSH 到那台服务器的机器上**（比如你自己的笔记本）配：

```env
# .env 里三行
OPS_BACKEND=ssh
OPS_SSH_TARGETS=web-01=root@你的服务器IP        # 逻辑名=用户@地址，Agent 只认逻辑名
OPS_SSH_KEY=~/.ssh/agentdesk_ops               # ★ 专用密钥，别复用平时登录那把
```

专用密钥生成与授权（一次性）：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/agentdesk_ops -N ""
ssh-copy-id -i ~/.ssh/agentdesk_ops.pub 用户名@你的服务器IP
```

重启服务，问「web-01 磁盘还剩多少」—— 返回的就是你服务器的真实数据，
每条结论都带它实际执行的命令。

## 第 2 步（可选）：Docker 部署

```bash
docker compose up -d --build         # OPS_* 与密钥路径都在 .env 里配
```

- 端口只绑 `127.0.0.1:8088`，要对外就自己套一层 Nginx + HTTPS + 令牌鉴权
  （参考 [docs/deployment.md](deployment.md)，那台 ECS 的完整部署实录）
- 沙箱要真容器隔离需挂 Docker（`SANDBOX_BACKEND=docker`），
  探测不到 Docker 时自动落到仿真——**fail-closed，绝不降级成无隔离执行**

## 安全边界（务必读完再用）

1. **只读为主**：6 个只读工具（磁盘/负载/服务/容器/日志/知识检索）；
   唯一的执行工具 `run_command` 走「白名单 → 审批单 → 一次性容器」，
   ssh 后端下**执行通道直接关闭**（fail-closed，宁可不能做不可做错）
2. **私钥权限**：目标机器上给这把公钥加限制
   （`from="你的IP"`, `no-pty,no-agent-forwarding,no-port-forwarding`），
   即使泄露也只能从你部署 AgentDesk 的那台机器使用
3. **令牌**：对外开放时设 `AUTH_ENABLED=1` + `AGENT_TOKEN`（≥32 位随机串），
   面试演示场景把令牌私下发给对方即可 —— 不提供自助注册是有意的
