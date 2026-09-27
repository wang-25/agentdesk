# 手写 ReAct vs LangGraph 对比报告

- 生成时间：2026-09-26 17:24:42
- 用例数：3
- 控制变量：同一份 Prompt、同一套工具执行逻辑、同一模型、temperature=0
- 唯一变量：编排方式（手写 while 循环 vs LangGraph 状态图）

## 一、总体对比

| 用例 | 工具调用(手写/LG) | token(手写/LG) | 耗时(手写/LG) | 工具集合 | 调用序列 | 完整性 |
|------|-------------------|----------------|---------------|----------|----------|--------|
| Q1   | 10 / 7            | 9717 / 6214    | 9.3s / 6.4s   | 一致     | 不同     | —      |
| Q2   | 6 / 6             | 7124 / 7176    | 6.2s / 7.2s   | 一致     | 不同     | —      |
| Q3   | 5 / 5             | 4880 / 4894    | 4.0s / 3.9s   | 一致     | 相同     | —      |

## 二、合计

> 全部用例执行成功，合计包含所有用例。

| 指标 | 手写 ReAct | LangGraph | 差异 |
|---|---|---|---|
| 工具调用总数 | 21 | 18 | -3 |
| token 总量 | 21721 | 18284 | -3437 |
| 总耗时 | 19.5s | 17.5s | -2.0s |
| 有效用例数 | 3 / 3 | 3 / 3 | |

## 三、结论

1. **两者的工具选择高度一致** —— 说明「该查什么」由 Prompt 和工具 schema 决定，与用什么框架无关。这是三条结论里最重要的一条：**Agent 的行为质量取决于你的 Prompt 和工具设计，不取决于框架。**
2. **调用序列不完全相同** —— 证明 Agent 行为不是严格可复现的。同一份 Prompt、同一个模型、temperature=0，两个实现仍会走出不同的路径。所以评测要看统计分布，不能只看单次结果；上线后也要有评测集兜底，而不是靠「我试过一次没问题」。
3. **token 与耗时的差异，主要来自模型的随机性，不是框架开销** —— 两个引擎都成功的用例上：手写 21721 token / 18284 token，总耗时 19.5s / 17.5s。因为两个版本共用了同一份 Prompt 和同一套工具执行代码，真正的变量只剩「模型这一步输出了什么」—— 它走了不同的路径、调了不同数量的工具，后续轮次就把差异放大了。

   ⚠️ **样本只有 3 个用例，这个差异在统计上不构成结论**，不能拿它给框架下判断。真要做选型对比，需要几十个用例 + 多轮重复。本报告站得住脚的结论只有前两条。
4. **框架的价值不在省 token，在于它多给的东西** —— 检查点（能中断续跑，这是人工确认功能的技术前提）、结构可视化（一张 mermaid 就是架构图）、按节点流式输出进度（前端能显示「正在查磁盘…」）。这三样手写版都能做，但要自己造。

## 四、原始数据

### Q1｜有明确故障

**问题**：web-01 上的网站访问很慢，有时报 502，帮我看下原因

> 磁盘满 + 上游超时，知识库里有对应案例

**手写 ReAct**：轮次 3 · 工具调用 10 次 · token 9717 · 9.3s · 停止原因 answered

工具序列：

1. `check_load(host=web-01)`
2. `check_disk(host=web-01)`
3. `check_service(host=web-01,service=nginx)`
4. `list_containers(host=web-01)`
5. `tail_log(host=web-01,service=nginx,lines=50)`
6. `tail_log(host=web-01,service=mariadb,lines=50)`
7. `check_load(host=db-01)`
8. `check_load(host=cache-01)`
9. `search_knowledge(query=nginx 502 upstream timed out no space left on device 磁盘满,top_k=3)`
10. `tail_log(host=web-01,service=wp-app,lines=50)`

回答：

```
**现象**：web-01 根分区磁盘写满（96%），叠加 2 核 CPU 上负载 8.42 的严重过载，导致 nginx 转发到本机 WordPress 容器时上游超时，对外表现为访问慢和 502。

**依据**

| 项目 | 数值 | 判断 |
|---|---|---|
| 根分区 `/dev/vda1` | 38.4G / 40G，**96%** | critical |
| `/var` 分区 | 21.0G / 60G，35% | 正常 |
| 负载 load1 / 核数 | 8.42 / 2 核 = **4.21 per core** | critical |
| 内存 | 总 3548MB，已用 3210MB，可用仅 **338MB** | 紧张 |
| 占用最高进程 | `mariadbd` CPU 62.4%、内存 41.2% | 本机跑着数据库 |
| nginx 服务 | active，3 天未重启，PID 1183 | 进程本身正常 |
| 容器 | wp-app、wp-db 均 Up 3 天，重启 0 次 | 容器没崩 |
| nginx 日志 | `upstream timed out (110) while reading response header from upstream`，上游 `127.0.0.1:8080` | 上游响应超时 |
| nginx 日志 | `no space left on device while writing to /var/log/nginx/access.log`、`could not open error log file` | 磁盘已写满 |
| db-01 | load1 0.42 / 4 核，内存可用 4780MB | 正常 |
| cache-01 | load1 1.1 / 2 核，内存可用 1164MB | 正常 |

**根因**

根分区写满 + 本机资源过载，是本次故障的直接原因，置信度**确定**：

1. 根分区 96% 已满，nginx 连 access.log / error.log 都写不进去（日志里明确报 `no space left on device`）。磁盘满会同时拖慢所有需要写盘的进程。
2. 本机 2 核却跑着 `mariadbd`（CPU 62.4%、内存 41.2%），负载冲到 8.42，可用内存只剩 338MB。WordPress 容器（wp-app → 127.0.0.1:8080）拿不到 CPU 和内存，响应超过 nginx 的 upstream 超时阈值，于是返回 502。
3. 注意：**db-01 和 cache-01 都是空闲的**，说明 web-01 上这个 `mariadbd` 是本地自建的数据库，没有走 db-01 —— 这是架构上的错配，也是负载来源。

需要补充确认的一点（置信度：可能）：`mariadbd` 与容器 `wp-db`（mariadb:10.11）是否为同一实例、以及根分区被什么占满，工具未返回该信息，无法确定。

**建议**（按优先级）

1. **立即释放根分区空间**（最高优先级，先止血）
   - 定位大文件：`du -xh --max-depth=1 / | sort -h`，重点看 `/var/log`、`/var/lib/docker`、`/tmp`。
   - 清理已轮转日志：`journalctl --vacuum-size=200M`；清理 `/var/log/ng
```

**LangGraph**：轮次 2 · 工具调用 7 次 · token 6214 · 6.4s · 停止原因 answered

工具序列：

1. `check_load(host=web-01)`
2. `check_disk(host=web-01)`
3. `check_service(host=web-01,service=nginx)`
4. `list_containers(host=web-01)`
5. `tail_log(host=web-01,service=nginx,lines=50)`
6. `tail_log(host=web-01,service=mariadb,lines=50)`
7. `search_knowledge(query=nginx 502 网站访问慢 根分区磁盘满,top_k=3)`

回答：

```
**现象**：web-01 根分区磁盘写满（96%），叠加负载严重过载，导致 WordPress 上游响应超时，nginx 返回 502。

**依据**：
- 磁盘：`/dev/vda1` 挂载 `/`，40G 中已用 38.4G，使用率 **96%**，级别 critical；`/var` 分区仅 35%，说明问题集中在根分区。
- 负载：2 核 CPU，load1 = **8.42**，每核负载 **4.21**（critical）；内存 3548MB 中已用 3210MB，仅剩 **338MB** 可用。
- 进程：`mariadbd` 占 CPU **62.4%**、内存 **41.2%**，是负载主要来源；nginx 仅 8.1%。
- nginx 日志（4 条）明确命中两个已知错误模式：
  - `no space left on device while writing to /var/log/nginx/access.log`
  - `could not open error log file`
  - `upstream timed out (110: Connection timed out) while reading response header from upstream`，上游为 `http://127.0.0.1:8080`（即 wp-app 容器）
- 服务状态：nginx active（运行 3 天，0 次重启）；容器 wp-app、wp-db 均 Up 3 天，无重启。**说明不是进程崩溃，而是资源耗尽导致的响应超时。**
- 注：web-01 上无 mariadb 系统服务日志（数据库跑在容器 wp-db 里），该路径无法获取日志。

**根因**：
根分区 `/` 写满（96%）是直接触发因素——nginx 无法写 access/error 日志，同时磁盘满会拖慢容器与数据库的 I/O，使 wp-app 上游响应超时，nginx 报 502。负载过载（每核 4.21）与内存紧张（仅剩 338MB）是并发的加重因素，主要消耗者是 mariadbd（CPU 62.4%、内存 41.2%）。
置信度：**磁盘写满为确定**（日志直接命中 + 分区数据一致）；**mariadbd 高负载与 502 的因果关系为可能**，需进一步确认数据库慢查询/连接数。

**建议**（按优先级）：
1. **立即释放根分区空间**（最高优先级）：定位 `/` 上的大文件/大目录，重点排查 `/var/log`（nginx 日志未轮转）、容器日志（`/var/lib/docker/containers/*/*-json.log`）、以及 `/tmp`。清理或截断后可立即恢复 nginx 写日志能力。
2. **配置日志轮转**：为 nginx 与容器日志设置 logrotate / `max-size`，避免再次写满。
3. **缓解负载与内存**：检查 wp-db（mariadb）容器的慢查询与连接数，必要时限制其内存/CPU；确认 338MB 可用内存是否已触发 swap 抖动。
4. **验证恢复**：清理空间后观察 nginx 错误日志是否停止出现 `no space left`，并确认 502 是否消失；若 502 仍在，再深入排查 wp-app 容器与数据库连接。
5. **长期**：为 `/` 分区扩容或迁移日志/数据目录到 `/var`（当前仅 35%，有充足余量）。

以上均为只读排查结论，我未执行任何清理
```

### Q2｜容器故障

**问题**：cache-01 上的 Redis 容器一直在重启，帮我查下原因

> AOF 权限问题，需要看容器状态和日志

**手写 ReAct**：轮次 3 · 工具调用 6 次 · token 7124 · 6.2s · 停止原因 answered

工具序列：

1. `list_containers(host=cache-01)`
2. `check_load(host=cache-01)`
3. `check_disk(host=cache-01)`
4. `tail_log(host=cache-01,service=redis,lines=50)`
5. `check_service(host=cache-01,service=redis)`
6. `search_knowledge(query=Redis 容器重启 append-only file Permission denied AOF 权限,top_k=3)`

回答：

```
知识库没有直接命中该场景的处置手册，但日志本身已给出明确原因。

**现象**：cache-01 上的 `redis-cache` 容器处于反复重启状态（Restarting），累计重启 47 次。

**依据**：
- 容器状态：`redis-cache`（redis:7-alpine），状态 `Restarting (1) 8 seconds ago`，重启次数 **47**，`restarting: true`
- Redis 日志（尾部）：`Fatal error, can't open the append-only file: Permission denied`，命中已知错误模式「权限不足」
- 主机负载正常：load1=1.1 / 2 核（每核 0.55，level=ok），内存 610MB/1774MB 已用，可用 1164MB
- 磁盘正常：`/` 分区 12.1G/40G，使用率 **32%**（level=ok）
- 主机上不存在名为 `redis` 的 systemd 服务（`not-found`），说明 Redis 以容器方式运行，非 systemd 托管

**根因**：Redis 进程无法打开 AOF（append-only file）持久化文件，报 `Permission denied`，导致启动即致命退出，容器被反复拉起又崩溃。**置信度：确定**（日志明确指向 AOF 文件权限问题，且已排除资源类原因——负载、内存、磁盘均正常）。

**建议**（按优先级）：
1. **确认 AOF 文件与目录的属主/权限**：进入宿主机查看 Redis 数据卷挂载点（如 `/var/lib/redis` 或 compose 中映射的目录），检查 `appendonly.aof` 及所在目录的 owner/group 与权限。Redis 容器默认以 `redis` 用户（UID 999）运行，若宿主机目录属主为 root 且权限过严，即会触发此错误。
2. **修正权限**：将数据目录属主调整为容器内运行用户，例如 `chown -R 999:999 <数据目录>`，并确保目录可写（`chmod 750` 或按需）。注意不要用 `chmod 777` 这类过宽权限。
3. **检查挂载配置**：核对 docker-compose / `docker run` 的 volume 映射，确认挂载路径与 Redis 配置中 `dir`、`appendfilename` 指向一致，避免挂载点覆盖导致权限错乱。
4. **临时恢复**：权限修正后重启容器（`docker restart redis-cache`）观察是否稳定；若 AOF 文件已损坏，需按 Redis 官方方式用 `redis-check-aof --fix` 修复后再启动。
5. **加固**：为容器配置日志轮转与重启策略监控，避免此类崩溃循环长期未被发现（本次已重启 47 次）。

注：以上均为只读排查结论与建议，我未执行任何变更操作。
```

**LangGraph**：轮次 3 · 工具调用 6 次 · token 7176 · 7.2s · 停止原因 answered

工具序列：

1. `list_containers(host=cache-01)`
2. `check_load(host=cache-01)`
3. `check_disk(host=cache-01)`
4. `tail_log(host=cache-01,service=redis,lines=50)`
5. `check_service(host=cache-01,service=redis)`
6. `search_knowledge(query=Redis 容器反复重启 append-only file Permission denied AOF 权限,top_k=3)`

回答：

```
知识库没有直接命中该场景的处置条目，但日志本身已给出明确原因。

**现象**：cache-01 上的 Redis 容器 `redis-cache` 处于反复重启状态，已重启 47 次。

**依据**：
- 容器状态：`redis-cache`（镜像 `redis:7-alpine`）状态为 `Restarting (1) 8 seconds ago`，累计重启 **47 次**，`restarting: true`。
- 容器日志（唯一一条）：`Fatal error, can't open the append-only file: Permission denied`，命中已知错误模式「权限不足」。
- 主机资源正常，可排除资源耗尽：负载 1.1 / 2 核（每核 0.55，level=ok）；内存 610MB / 1774MB 已用，可用 1164MB；根分区 `/` 使用率 32%（level=ok）。
- 主机上不存在名为 `redis` 的 systemd 服务（`not-found`），说明 Redis 以容器方式运行，与现象一致。

**根因**：Redis 启动时无法打开 AOF（append-only file）文件，报 `Permission denied`，进程随即以退出码 1 退出，被容器重启策略反复拉起，形成重启循环。**置信度：确定**（日志直接给出致命错误，且资源类原因已排除）。

具体权限问题的来源（如挂载卷属主/权限被改、容器以非 root 用户运行而 AOF 目录属主为 root、SELinux/AppArmor 拦截等）从现有信息无法进一步区分，需要登录主机确认。

**建议**（按优先级）：
1. **确认 AOF 文件与目录的属主/权限**：在 cache-01 上查看 Redis 数据卷挂载点（如 `docker inspect redis-cache` 找到 `appendonlydir`/`appendonly.aof` 所在宿主机路径），执行 `ls -ln` 检查属主与权限。Redis 容器默认以 `redis` 用户（uid 999）运行，AOF 目录需对该 uid 可写。
2. **修正属主/权限**：将 AOF 目录及文件属主改为容器内运行用户，例如 `chown -R 999:999 <aof_dir>`，权限设为 `0750`/`0644` 级别，然后重启容器。
3. **检查是否被安全模块拦截**：若属主权限正常仍报错，查看 `dmesg` / audit 日志确认是否 SELinux 或 AppArmor 拒绝访问，必要时调整挂载标签（如 `:z`/`:Z`）。
4. **临时恢复服务**：若急需恢复且 AOF 数据可接受丢失，可临时以 `--appendonly no` 启动验证是否为 AOF 路径问题（仅用于定位，不建议长期关闭持久化）。
5. **验证**：重启后确认容器状态为 `Up` 且重启计数不再增长，并检查日志无新的 `Permission denied`。

注：以上均为只读排查与建议，我未在主机上执行任何变更操作。
```

### Q3｜负例（不该查出问题）

**问题**：db-01 最近状态怎么样，有没有需要注意的地方

> ★ 关键用例：一切正常时，Agent 应该如实说'没发现问题'，而不是硬找一个问题出来。这是最容易被模型搞砸的一类

**手写 ReAct**：轮次 2 · 工具调用 5 次 · token 4880 · 4.0s · 停止原因 answered

工具序列：

1. `check_load(host=db-01)`
2. `check_disk(host=db-01)`
3. `list_containers(host=db-01)`
4. `check_service(host=db-01,service=mysql)`
5. `tail_log(host=db-01,service=mysql,lines=50)`

回答：

```
**现象**：db-01 整体状态健康，各项指标均在正常范围内，没有发现需要立即处理的异常。

**依据**：
- 负载：load1/5/15 = 0.42 / 0.51 / 0.48，4 核，每核负载 0.1，判定 ok
- 内存：总 7900 MB，已用 3120 MB，可用 4780 MB（占用约 39%）
- 磁盘：`/` 使用 41%（31.2G/80G），`/var/lib/mysql` 使用 44%（88.0G/200G），最高 44%，判定 ok
- 服务：mysql 服务 active，已运行 15 天，重启次数 0，PID 2210
- 容器：zabbix-mysql（mysql:8.0）状态 Up 15 days，重启次数 0，无反复重启
- 日志：仅 1 条 InnoDB buffer pool 加载完成记录，未命中任何已知错误模式
- 资源占用最高进程：mysqld，CPU 12.0%、内存 28.5%

**根因**：无故障。所有指标正常，置信度：确定（基于当前采集到的数据）。

**建议**：
1. 无需处置，保持现状即可。
2. 唯一值得留意的是 `/var/lib/mysql` 已用 88G/200G，按当前增速可关注增长趋势，建议纳入日常容量监控，暂不需要扩容。
3. 日志样本量偏少（仅返回 1 行），若后续要排查慢查询或连接问题，建议单独拉取更长时间窗口的 MySQL 错误日志与慢查询日志。

补充说明：以上数据来自工具返回，其中 backend 标记为 mock，如需生产级结论建议在真实环境复核。
```

**LangGraph**：轮次 2 · 工具调用 5 次 · token 4894 · 3.9s · 停止原因 answered

工具序列：

1. `check_load(host=db-01)`
2. `check_disk(host=db-01)`
3. `list_containers(host=db-01)`
4. `check_service(host=db-01,service=mysql)`
5. `tail_log(host=db-01,service=mysql,lines=50)`

回答：

```
**现象**：db-01 整体状态健康，各项指标均在正常范围内，没有发现需要立即处理的异常。

**依据**：
- 负载：load1 = 0.42 / load5 = 0.51 / load15 = 0.48，4 核 CPU，每核负载 0.1，判定 ok
- 内存：总 7900 MB，已用 3120 MB，可用 4780 MB（占用约 39%）
- 磁盘：`/` 使用 41%（31.2G/80G），`/var/lib/mysql` 使用 44%（88.0G/200G），最高 44%，判定 ok
- 服务：mysql 服务 active，已运行 15 天，重启次数 0，PID 2210
- 容器：zabbix-mysql（mysql:8.0）状态 Up 15 days，重启次数 0，无反复重启
- 日志：仅 1 条 InnoDB buffer pool 加载完成记录，未命中任何已知错误模式
- 资源占用最高进程：mysqld，CPU 12.0%、内存 28.5%

**根因**：无故障。所有检查项（负载、内存、磁盘、服务、容器、日志）均正常，置信度：确定。

**建议**：
1. 无需处置，保持现状即可。
2. 唯一值得留意的是 `/var/lib/mysql` 已用 88G/200G，按当前增速建议纳入容量趋势监控，避免后续写满（当前 44%，尚有余量，非紧急）。
3. 若后续出现性能问题，可优先关注 mysqld 的慢查询与连接数，本次日志中未包含相关信息，需要时可进一步抓取。

补充说明：以上数据来自 mock 后端，日志仅返回 1 行，样本较少，无法据此评估历史错误趋势。
```
