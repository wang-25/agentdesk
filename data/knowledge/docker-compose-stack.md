# Docker Compose 栈排障：容器起不来、端口冲突、卷权限、网络不通

## 现象

`docker compose up -d` 之后有的容器没起来、`Exited (1)` 反复重启、
端口被占用、容器之间互相连不上、或者配置改了但行为没变。

## 先理解：Compose 折腾的多半不是应用本身

单个容器跑不起来，通常是应用自己的问题。但**一个栈跑不起来**，
绝大多数是这四件事之一：端口、卷、网络、配置没重新加载。
先查这四项，比去翻应用日志快得多。

## 第一步：看清每个容器的状态

```bash
# 看这个栈里所有容器的状态
docker compose ps

# 状态是 Exited 的，看它退出前的输出（这是第一手证据）
docker compose logs <服务名>

# 只看最后 50 行，并持续跟随
docker compose logs --tail 50 -f <服务名>
```

`State` 列的含义：

- `Up` —— 正常
- `Up (unhealthy)` —— 进程在但健康检查不过，**应用可能实际不可用**
- `Exited (1)` —— 启动即失败，看日志
- `Restarting` —— 反复重启，看退出码（另见「容器退出码」）

## 第二步：端口冲突（最常见）

```bash
# 看宿主机上哪些端口已经被占了
ss -lntp | grep -E ':80|:443|:3306|:8080'

# 看某个端口到底被谁占着
ss -lntp | grep ':3306'
```

典型报错：`Bind for 0.0.0.0:3306 failed: port is already allocated`

两种处理，别混着用：

```yaml
# 方案 A：改宿主机这一侧的端口（容器内部端口不变）
ports:
  - "3307:3306"

# 方案 B：根本不映射到宿主机 —— 只有同网络的容器需要访问它时这么做
# （数据库就该这样，不暴露给宿主机更安全）
# 不写 ports，容器间用服务名互访
```

> **判断标准**：这个服务需不需要从**宿主机外面**访问？不需要就别映射端口。
> 数据库、Redis 这类只被其它容器访问的服务，映射出去只是多一个攻击面。

## 第三步：卷与权限

```bash
# 看这个栈用了哪些卷，以及它们现在多大
docker compose config | grep -A5 volumes
docker system df -v | grep -A20 "VOLUME NAME"

# 看容器里的用户是谁（权限问题的根子往往在这）
docker compose exec <服务名> id
```

典型报错与根因：

- `Permission denied` 写文件 → 容器里跑的是非 root 用户（如 `uid=65534`），
  而挂载出来的宿主目录属主是 root。**要么改宿主目录属主，要么显式指定 user。**
- 挂载后目录被清空 → 用**命名卷**挂载到了应用目录，卷是空的就会覆盖镜像里的内容。
  第一次挂载前先把镜像里的原有文件复制出来。
- 改了挂载的配置文件没生效 → 应用只在启动时读一次，需要重启而不是 reload。

```bash
# 把宿主目录属主改成容器里跑的那个 uid（以 65534 为例）
chown -R 65534:65534 /path/to/data
```

## 第四步：容器之间网络不通

```bash
# 看这个栈创建了哪些网络，以及谁在上面
docker network ls
docker network inspect <栈名>_default | grep -A20 Containers

# 进到一个容器里，直接测另一个服务能不能连上（用服务名，不是 IP）
docker compose exec app ping db
docker compose exec app nc -zv db 3306
```

要点：

- Compose 里**用服务名互访**（`db`、`redis`），不要写容器 IP —— IP 会变
- 容器**重启后 IP 可能变**，所以硬编码 IP 的栈迟早出问题
- 用 `127.0.0.1` 连另一个容器是**错的** —— 那是这个容器自己
- 外部要访问，才需要 `ports` 映射；容器间访问不需要

## 第五步：配置改了没生效

这是最容易白忙一阵的一类：

```bash
# 看 Compose 实际生效的配置（环境变量替换之后的样子）
docker compose config

# 改了 compose 文件后，只重建受影响的服务
docker compose up -d --force-recreate <服务名>

# 只改了镜像？重新拉
docker compose pull <服务名> && docker compose up -d <服务名>
```

- 改了 `environment` / `command` → 必须**重建容器**（`up -d` 通常会自动做）
- 改了镜像里的 `daemon.json` 一类全局配置 → **只对之后新建的容器生效**，
  已有容器要 `--force-recreate` 才吃得到
- 改了 `.env` → 要重新 `up`，已经跑着的容器不会自动读到

## 常见根因速查

| 症状 | 先查 |
|---|---|
| 端口被占 | `ss -lntp \| grep :端口` |
| 写文件 Permission denied | 容器里 `id` + 宿主目录属主 |
| 挂载后目录空了 | 是否用空命名卷覆盖了应用目录 |
| 容器间连不上 | 用服务名而非 IP / 127.0.0.1 |
| 改配置不生效 | 是否重建了容器 |
| 磁盘悄悄被吃光 | `docker system df -v`（多半是日志或 build cache） |

## 几个容易踩的坑

- **`docker compose down` 和 `stop` 不是一回事**。`down` 会删掉容器和网络，
  加 `-v` 还会删掉卷（**数据会没**）；`stop` 只是停，数据都还在。
- **把数据库端口映射到公网**。多数被拖库的站都是这么没的 ——
  数据库只该被同网络的容器访问。
- **用 `latest` 标签**。某天 `up` 一下服务就坏了，因为镜像悄悄升了级。
  固定具体版本号。
- **不看 `docker system df`**。跑久了磁盘被 build cache 和停止的容器吃满，
  根因却当成"应用写太多日志"去查。
