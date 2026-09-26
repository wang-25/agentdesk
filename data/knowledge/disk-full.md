# 磁盘写满（No space left on device）排查与清理

## 现象

应用报 `No space left on device`、`disk full writing to ...`，
或 MySQL/MariaDB 报 `Disk full writing to './ib_logfile0'`。
`df -h` 显示某个分区使用率接近或达到 100%。

## 第一步：确认到底是哪个分区

```bash
df -h              # 看各分区使用率
df -i              # ★ 别忘看 inode！"空间够但 inode 用尽"是独立的故障
```

**注意一点**：有时 `df -h` 显示还有空间，但应用仍然报写满。这通常是两种情况：

1. **inode 用尽** —— 大量小文件把 inode 耗光了，`df -i` 的 IUse% 会是 100%
2. **已删除但被进程占用的文件** —— 文件删了但句柄没释放，空间不归还

第二种的查法：

```bash
lsof | grep deleted | head -20
# 或
lsof +L1
```

找到之后重启对应进程，空间立即释放。

## 第二步：找出谁占的空间

从根目录逐层往下定位：

```bash
du -sh /* 2>/dev/null | sort -rh | head -10
# 定位到具体目录后继续往下
du -sh /var/* 2>/dev/null | sort -rh | head -10
```

`sort -rh` 里的 `-h` 让 1.2G、900M 这种带单位的字符串能正确排序 ——
不加 `-h` 会按字典序排出错误结果，这是很常见的坑。

## 最常占满磁盘的几类目录

| 路径 | 什么在涨 | 怎么处理 |
|---|---|---|
| `/var/log` | 应用日志、journald | 配 logrotate；`journalctl --vacuum-size=500M` |
| `/var/lib/docker` | 容器层、镜像、日志 | `docker system prune -a`；限制容器日志大小 |
| `/var/lib/mysql` | 数据库数据、binlog | 清理过期 binlog；开 binlog 过期策略 |
| `/tmp` | 临时文件没清 | 加 tmpfiles 清理规则 |
| `/root` 或家目录 | 下载的文件、日志 | 人工确认后清理 |
| 被删除但未释放的文件 | 进程持有句柄 | 重启进程 |

## Docker 占满磁盘的专项处理

容器日志默认不自限，是常见元凶：

```bash
# 看 Docker 总共占了多少
docker system df -v

# 清理：停止的容器、无用镜像、无用卷、构建缓存
docker system prune -a

# 找出日志最大的容器
du -sh /var/lib/docker/containers/*/*-json.log | sort -rh | head -5
```

**根治**：给 Docker 配置日志轮转（`/etc/docker/daemon.json`）：

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" }
}
```

改完需要重启 Docker，且**只对新建容器生效**，老容器要重建。

## 第三步：安全清理的顺序

**先低风险、高收益的，再动高风险目录。**

```bash
# 1. journald 日志（安全，自动保留最近的）
journalctl --vacuum-size=500M

# 2. 过期日志文件（先看再删，不要直接 rm -rf）
find /var/log -name "*.gz" -mtime +30 -ls | head -20
# 确认无误后再删

# 3. 包管理缓存（安全）
yum clean all      # 或 dnf clean all

# 4. Docker 资源（相对安全，但会删掉停止的容器）
docker system prune -a
```

## 风险提示

- **不要对 `/`、`/var`、`/usr` 直接 `rm -rf`**。先 `ls` 或 `find` 看清文件，再删。
- **不要删正在使用的日志文件**。用 `truncate -s 0 file.log` 清空，而不是 `rm` ——
  直接 `rm` 会让 `tail -f` 的进程继续持有句柄，空间不会释放。
- **数据库目录不要手工删文件**。清理 binlog 用 SQL：`PURGE BINARY LOGS BEFORE ...`
- 清理前先确认有没有备份。**磁盘满的时候删错文件，是最糟糕的故障升级路径。**
