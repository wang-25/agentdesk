# MySQL / MariaDB 连接数打满（Too many connections）

## 现象

应用报错：
- `ERROR 1040 (HY000): Too many connections`
- `SQLSTATE[HY000] [1040] Too many connections`

新连接全部被拒，已有连接可能仍然正常 —— 这一点很有迷惑性。

## 第一步：先拿到一个能连进去的会话

连接数满了，普通账号也连不进去。用 **`extra_port`** 或者以 root 走 socket 连：

```bash
# 走 unix socket 连（不走 TCP，不受 max_connections 限制影响）
mysql -u root -p --socket=/var/lib/mysql/mysql.sock

# 或者临时用管理员账号（MySQL 8 保留 1 个给 CONNECTION_ADMIN 权限用户）
mysql -u root -p
```

如果连 socket 也进不去，就得改配置文件临时加大 `max_connections` 然后重启 —— 
但这是最后的办法，因为重启会丢掉现场。

## 第二步：看当前连接情况

```sql
-- 当前连接数 vs 上限
SHOW STATUS LIKE 'Threads_connected';
SHOW STATUS LIKE 'Max_used_connections';
SHOW VARIABLES LIKE 'max_connections';

-- 按来源 IP 分组，找出谁开得多（这一步最能定位问题）
SELECT SUBSTRING_INDEX(host, ':', 1) AS ip,
       COUNT(*) AS conns
FROM information_schema.processlist
GROUP BY ip
ORDER BY conns DESC
LIMIT 10;

-- 按状态分组，看这些连接在干什么
SELECT command, state, COUNT(*)
FROM information_schema.processlist
GROUP BY command, state
ORDER BY COUNT(*) DESC;

-- 看有没有长事务 / 长时间 Sleep 的连接
SELECT id, user, host, db, command, time, state, LEFT(info, 80) AS query
FROM information_schema.processlist
WHERE command = 'Sleep' AND time > 300
ORDER BY time DESC;
```

## 三类成因，处理方式完全不同

### ① 连接泄漏（最常见）

应用拿到了连接但没释放。表现是大量 `Sleep` 状态的连接，`time` 很大。

**根因通常在应用侧**：异常路径没走到 `close()`，或者连接池配置不对。

```sql
-- 确认是否有大量长期 Sleep 连接
SELECT COUNT(*) FROM information_schema.processlist
WHERE command = 'Sleep' AND time > 600;
```

**处理**：应用侧用 `try/finally` 或连接池（HikariCP、SQLAlchemy pool）保证释放。
数据库侧的兜底是设置 `wait_timeout` / `interactive_timeout`，让空闲连接自动断开：

```ini
[mysqld]
wait_timeout = 600
interactive_timeout = 600
```

### ② 连接池配置过大

每个应用实例开 50 个连接，10 个实例就是 500 个。
**实例数 × 池大小必须小于 `max_connections`，并留余量给运维和监控。**

### ③ 慢查询堆积

连接本身正常，但每个查询都很慢，连接来不及释放。
表现是大量 `Query` 状态且 `time` 很大。

```sql
-- 看当前正在跑的慢查询
SELECT id, time, LEFT(info, 100) FROM information_schema.processlist
WHERE command = 'Query' AND time > 10 ORDER BY time DESC;

-- 看是否开了慢查询日志
SHOW VARIABLES LIKE 'slow_query_log%';
SHOW VARIABLES LIKE 'long_query_time';
```

**处理**：先 kill 掉明显卡住的查询，再优化 SQL 或加索引。

```sql
KILL <id>;      -- 只 kill 一条
```

## 应急恢复顺序

```sql
-- 1. 先 kill 掉长期 Sleep 的连接（安全，应用会自己重连）
SELECT CONCAT('KILL ', id, ';')
FROM information_schema.processlist
WHERE command = 'Sleep' AND time > 600;

-- 把上一步输出的语句复制执行
```

2. 如果还是连不上，临时提高上限（**不需要重启**）：

```sql
SET GLOBAL max_connections = 500;
```

注意这个改动**重启后会失效**。确认有效后，写进配置文件持久化：

```ini
[mysqld]
max_connections = 500
```

## 监控与预防

- 监控 `Threads_connected / max_connections` 的比值，**超过 80% 就该告警**
- 监控 `Threads_running`，它比 `Threads_connected` 更能反映真实压力
- 应用侧连接池设置合理的 `maxLifetime`，避免持有过期连接
- **不要靠一直调大 `max_connections` 解决问题** —— 
  连接数上去之后内存和上下文切换开销也会上去，最后是整体雪崩
