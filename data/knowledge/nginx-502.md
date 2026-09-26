# Nginx 502 Bad Gateway 排查

## 现象

浏览器或客户端收到 `502 Bad Gateway`，nginx 的 access log 里对应请求状态码为 502。

## 根本原因

502 的含义是 **nginx 作为反向代理，联系不上上游（upstream）**。所以问题几乎从不在 nginx 本身，而在它转发到的那一端。

常见原因按出现频率排序：

1. **上游服务没起来或已崩溃** —— 最常见。PHP-FPM、Gunicorn、Tomcat、Node 进程挂了，nginx 转发过去没人接。
2. **上游处理超时** —— 上游还活着但处理太慢，超过 `proxy_read_timeout`（默认 60 秒）。日志里会出现 `upstream timed out (110: Connection timed out) while reading response header`。
3. **上游连接数打满** —— 上游的 backlog 队列满了，新连接被拒。日志关键词：`connect() failed (111: Connection refused)` 或 `no live upstreams`。
4. **上游返回了非法响应** —— 上游吐出的 HTTP 头不合法，nginx 无法解析。

## 排查步骤

按这个顺序，每一步都能排除一类原因：

```bash
# 1. 先确认 nginx 自己活着，且配置没报错
systemctl status nginx
nginx -t

# 2. 看错误日志的最后 100 行 —— 这一步就能定位到具体是哪一类
tail -100 /var/log/nginx/error.log

# 3. 确认上游进程在不在
systemctl status php-fpm        # 或 gunicorn / tomcat
ps -ef | grep -v grep | grep gunicorn

# 4. 绕过 nginx，直接打上游端口，确认上游本身是否正常
curl -I http://127.0.0.1:8080/health

# 5. 看 nginx 转发到哪个地址（确认 upstream 配置没写错）
nginx -T | grep -A5 upstream

# 6. 检查连接数与文件描述符是否打满
ss -lntp | grep :8080
cat /proc/sys/net/core/somaxconn
ulimit -n
```

## 从 error.log 关键词直接判断

| 日志关键词 | 含义 | 处理方向 |
|---|---|---|
| `upstream timed out` | 上游处理超时 | 查上游慢在哪；必要时调大 `proxy_read_timeout` |
| `connect() failed (111: Connection refused)` | 上游端口没人监听 | 上游进程没起来或崩了 |
| `no live upstreams` | upstream 全部被标记为不可用 | 上游整体挂了，或健康检查配置过严 |
| `upstream sent invalid header` | 上游返回非法响应头 | 查上游代码/中间件 |
| `worker_connections are not enough` | nginx 自身连接数不够 | 调大 `worker_connections` |

## 临时恢复与根治

**临时恢复**（先恢复业务，再查根因）：

```bash
systemctl restart php-fpm      # 或对应的上游服务
systemctl reload nginx         # 配置改动后重载
```

**根治**要看是三类里的哪一类：

- 上游进程反复挂 → 查 OOM（`dmesg | grep -i oom`）、查代码异常
- 上游慢 → 加缓存、拆接口、加机器
- 连接数打满 → 调 `worker_processes`、`worker_connections`、上游的 backlog 与进程数

## 几个容易踩的坑

- **只重启 nginx 没用**。502 是上游的问题，重启 nginx 只是碰运气。
- **`proxy_read_timeout` 调大只是掩盖问题**。上游 60 秒还没处理完，说明它本身有性能问题。
- **注意 SELinux**。在 CentOS/Rocky 上，SELinux 会阻止 nginx 反向代理到非标准端口，日志里会出现 `Permission denied` 而不是连接错误，很容易看错方向。
