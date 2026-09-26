# Linux 负载（load average）异常升高排查

## 先理解 load average 是什么

`uptime` 输出的三个数字，是**过去 1 分钟、5 分钟、15 分钟的平均值**。

关键点：**load 统计的是"处于可运行状态 + 不可中断等待状态"的进程数**。

- 可运行状态（R）：在等 CPU
- 不可中断等待（D）：在等 I/O（磁盘、网络文件系统）

**所以 load 高不等于 CPU 忙。** 这是最常被误解的一点 ——
磁盘 I/O 卡住时，进程处于 D 状态，load 会飙到几十，但 CPU 很闲。

## 第一步：区分是 CPU 还是 I/O

```bash
uptime            # 看 load 三个数字
nproc             # 看 CPU 核数

top               # 看 %Cpu(s) 那一行
# 关注：us（用户态）、sy（内核态）、wa（I/O 等待）、id（空闲）

# 直接看有没有 D 状态的进程 —— 有的话基本可以确定是 I/O 问题
ps -eo state,pid,ppid,comm | awk '$1=="D"'
```

**判断规则：**

| 现象 | 结论 |
|---|---|
| load 高 + `%wa` 高 + 有 D 状态进程 | **I/O 瓶颈**，不是 CPU |
| load 高 + `us`/`sy` 高 + `wa` 低 | CPU 瓶颈，找吃 CPU 的进程 |
| load 高但 CPU 和 I/O 都不高 | 看是不是进程数/线程数过多，或内核态锁竞争 |

**经验值**：load 除以核数，大于 1 说明有排队。4 核机器 load 到 8 就是严重过载。

## 第二步：定位具体进程

```bash
# 按 CPU 排序
top -o %CPU

# 一次看清各进程的 CPU 和状态
ps -eo pid,ppid,state,pcpu,pmem,comm --sort=-pcpu | head -20

# 看线程级的 CPU 占用（找出是哪个线程在吃）
top -H -p <pid>
```

## 第三步：I/O 方向继续深挖

```bash
# 看磁盘利用率：%util 接近 100% 说明盘打满了
iostat -x 1 3
# 关注：%util、await（平均等待毫秒）、r/s w/s

# 看哪个进程在读写在最凶
iotop -o

# 看文件系统是否满或只读挂载
mount | column -t
dmesg -T | tail -50      # 硬盘坏道会在这里留下 I/O error
```

## 第四步：CPU 方向继续深挖

```bash
# 看上下文切换和运行队列
vmstat 1 5
# 关注：r（运行队列长度）、cs（上下文切换）、sy

# cs 异常高 → 可能是锁竞争或线程数过多
```

## 常见根因与处理

| 根因 | 特征 | 处理 |
|---|---|---|
| 死循环 / 代码 bug | 单进程 CPU 100% | 看该进程栈：`cat /proc/<pid>/stack`；重启并修代码 |
| 大量并发请求 | 多进程均摊高 CPU | 加机器、加限流、查上游流量来源 |
| 磁盘 I/O 打满 | `%util` ~100%，D 状态进程多 | 换 SSD、拆盘、查是不是在跑备份/大查询 |
| 内存不足换页 | `si`/`so` 非零，swap 在动 | 加内存；查内存泄漏 |
| NFS 挂载卡住 | 大量 D 状态，`df -h` 也卡住 | 检查挂载点；加 `soft,timeo` 挂载参数 |
| 线程数爆炸 | `cs` 极高 | 限制线程池；查是不是空跑创建线程 |

## 应急处理

```bash
# 找出 CPU 占用最高的前 5 个进程，确认后终止
ps -eo pid,pcpu,pmem,comm --sort=-pcpu | head -6

# 优雅停止（给进程处理机会）
kill -15 <pid>
# 15 秒后还没停，再强杀
kill -9 <pid>
```

**注意：kill -9 之前先确认这个进程是什么。**
在负载已经很高的时候杀掉关键进程（比如数据库），故障会直接升级。

## 一条容易忽略的检查

```bash
# 是不是被 CPU 配额限制了（容器 / cgroup 环境）
cat /sys/fs/cgroup/cpu.max 2>/dev/null
docker stats
```

容器里 load 高但宿主机看起来正常，很可能是 `--cpus` 配额给太小了。
