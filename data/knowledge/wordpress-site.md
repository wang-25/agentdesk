# WordPress 站点访问慢 / 白屏 / 后台异常排查

## 现象

站点打开很慢、间歇性白屏、后台登录转圈、或者前台正常但后台进不去。
错误页上可能显示 `502`、`504`，也可能什么都不显示直接空白。

## 先分清是哪一层挂的

WordPress 站点至少有四层，**症状相似但处置完全不同**。先分层，再动手：

| 层 | 典型症状 | 一句话判断 |
|---|---|---|
| DNS / 网络 | 解析不到、超时 | `curl -I` 连都连不上 |
| 反向代理（NPM 等） | 502 / 504、证书错误 | 直连容器端口却正常 |
| WordPress / PHP 容器 | 白屏、500、慢 | 直连容器端口也很慢 |
| MariaDB | 后台能开但前台报错、极慢 | 报 `Error establishing a database connection` |

**判断的关键动作是"绕过反向代理直连容器"** —— 这一步能立刻把问题砍掉一半。

## 第一步：确认站点在哪一层

```bash
# 1. 从外面看：拿到状态码和耗时
curl -o /dev/null -s -w "HTTP %{http_code}  总耗时 %{time_total}s\n" https://你的域名/

# 2. 在宿主机上绕过反向代理，直连 WordPress 容器的端口
#    先确认容器映射到宿主机的哪个端口
docker ps --format '{{.Names}}\t{{.Ports}}'

# 直连容器（把 8080 换成实际映射端口）
curl -o /dev/null -s -w "HTTP %{http_code}  总耗时 %{time_total}s\n" http://127.0.0.1:8080/
```

- 外面慢、直连快 → 问题在**反向代理**那一层
- 直连也慢 → 问题在 **WordPress 或数据库**

## 第二步：看访问日志，识别流量特征

**这一步经常被跳过，但它经常就是答案。** 站点莫名变慢，未必是配置问题，
可能是正在被刷。

```bash
# 看容器访问日志的最近 500 行
docker logs --tail 500 wp-app

# 按来源 IP 统计请求数，看有没有某个 IP 特别突出
docker logs --tail 20000 wp-app | awk '{print $1}' | sort | uniq -c | sort -rn | head -10

# 看请求都打在哪些路径上
docker logs --tail 20000 wp-app | awk '{print $7}' | sort | uniq -c | sort -rn | head -10
```

典型的异常特征：

- 某个 IP 占了绝大多数请求
- 请求密集打在同一个路径上（`/xmlrpc.php`、`/wp-login.php`）
- 请求间隔非常规律（脚本行为，几秒一次）
- User-Agent 伪装成 `Jetpack`、`WordPress.com`、`Mozilla` 但行为不像人

## 第三步：数据库方向

```bash
# 进数据库容器看当前连接与慢查询
docker exec wp-db mysql -uroot -p -e "SHOW STATUS LIKE 'Threads_connected';"
docker exec wp-db mysql -uroot -p -e "SHOW PROCESSLIST;"

# 看有没有锁等待
docker exec wp-db mysql -uroot -p -e "SELECT * FROM information_schema.INNODB_TRX\G"
```

- `Threads_connected` 接近 `max_connections` → 连接数方向的问题
- `PROCESSLIST` 里有大量 `Sleep` → 应用没正确关闭连接，或者连接池配置过大
- 有长时间运行的 `SELECT` → 缺索引或全表扫描

## 第四步：WordPress 自身

```bash
# 看 PHP 错误日志（路径随镜像不同，常见这两个）
docker exec wp-app tail -50 /var/log/apache2/error.log
docker exec wp-app tail -50 /var/log/php/error.log

# 看容器资源占用
docker stats --no-stream wp-app wp-db
```

- 内存接近上限 → PHP-FPM 子进程被杀，表现为间歇性 502
- 日志里出现 `Allowed memory size exhausted` → 调 `WP_MEMORY_LIMIT` 或 PHP 的 `memory_limit`

## 常见根因与处理

| 根因 | 判断依据 | 处理 |
|---|---|---|
| 被刷 / 慢速攻击 | 日志里单个 IP 占比极高 | 在反向代理层拦路径 + 限速（另见「暴力破解处置」） |
| 插件冲突 | 停用插件后恢复 | 后台逐个停用；进不去后台就改名插件目录 |
| 数据库慢 | `PROCESSLIST` 有长查询 | 加索引、清理 `wp_options` 里的过期 transient |
| 内存不足 | `docker stats` 接近上限 | 加内存或调小 PHP-FPM 子进程数 |
| 磁盘满 | `df -h` 使用率 100% | 另见「磁盘写满排查」 |

## 临时恢复与根治

**临时恢复**（先让站点能用）：

```bash
docker restart wp-app                      # 重启 WordPress 容器
docker exec wp-db mysql -uroot -p -e "KILL <阻塞的查询ID>;"
```

**根治**要看具体根因：

- 被刷 → 反向代理层拦截 + fail2ban，而不是靠重启（重启完它接着来）
- 插件 → 找出那个插件并替换掉
- 数据库 → 加索引、定期清理 transient、配置慢查询日志
- 内存 → 这是容量问题，重启只是把计时器归零

## 几个容易踩的坑

- **一慢就重启容器**。重启会清空现场证据（日志、连接状态、进程），
  真正的原因往往就在那里面。先看日志再重启。
- **只调 PHP 的 `memory_limit` 掩盖内存不足**。容器本身只有那么多内存，
  把单个进程的限制调大，只会让它更容易被 OOM 杀掉。
- **改了配置没生效**。WordPress 常把配置写进数据库（`wp_options`），
  在 `wp-config.php` 里改了不一定生效。
- **忘了对象缓存**。装了 Redis 类插件但 Redis 容器没起来，
  表现是"站点时快时慢"，很容易查错方向。
