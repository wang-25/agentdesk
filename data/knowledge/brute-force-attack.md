# 暴力破解与恶意扫描：识别与处置

## 现象

站点莫名变慢、访问日志里某个 IP 出现成千上万次、
后台登录页刷不动、或者收到服务商的异常流量告警。

这类事情**不会自己停止** —— 脚本一旦开始就会一直打下去，
所以"重启一下就好了"是错觉，它转头就回来。

## 第一步：从访问日志里把它找出来

```bash
# 容器日志：按来源 IP 统计 Top 10
docker logs --tail 20000 <容器名> | awk '{print $1}' | sort | uniq -c | sort -rn | head -10

# 看请求都打在哪些路径
docker logs --tail 20000 <容器名> | awk '{print $7}' | sort | uniq -c | sort -rn | head -10

# 看某个具体 IP 在干什么
docker logs --tail 20000 <容器名> | grep '45.134.79.120' | tail -20
```

异常特征（任意一条都值得警惕）：

- 单个 IP 的请求数远超其他人（几百比一）
- 请求密集打在同一个路径上
- **间隔非常规律**（每 10 秒左右一次 = 脚本）
- User-Agent 伪装成正常客户端（`Jetpack`、`Mozilla`、`curl`）但行为不像人

## 第二步：判断它在打什么

| 目标 | 意图 | 危害 |
|---|---|---|
| `/xmlrpc.php` | 利用 `system.multicall`，**一条请求里试几百组密码** | 爆破后台密码的加速器 |
| `/wp-login.php` | 直接撞库 | 拿到后台权限 |
| `/phpmyadmin`、`/admin`、`.env` | 探测常见入口与配置文件 | 信息泄露 |
| `/.git/config` | 探测源码泄露 | 源码与凭据泄露 |

关键判断：**看响应码**。

```bash
# 看这些请求被返回了什么状态码
docker logs --tail 20000 <容器名> | grep xmlrpc | awk '{print $9}' | sort | uniq -c
```

- 全是 `200` → **请求被正常处理了**，入口没挡住，它在继续试
- 是 `403` / `404` → 已经被挡住，危害有限，但流量还在消耗资源

## 第三步：处置（按性价比排序）

### 1. 反向代理层直接拒绝（最有效，一步到位）

在 Nginx / Nginx Proxy Manager 的对应站点配置里：

```nginx
location = /xmlrpc.php {
    return 403;
}
```

如果不用远程发布（桌面客户端、手机 App 发博），**这个入口完全可以关掉**，
没有任何副作用。

```bash
# 改完先校验，再重载 —— 顺序不能反
docker exec <npm容器> nginx -t
docker exec <npm容器> nginx -s reload
```

`nginx -t` 不通过就**绝对不要 reload**。

### 2. 限速（治标，但能挡住慢速扫描）

```nginx
limit_req_zone $binary_remote_addr zone=login:10m rate=10r/m;

location = /wp-login.php {
    limit_req zone=login burst=5 nodelay;
    # 转发给上游的配置照常
}
```

### 3. fail2ban（自动封 IP）

看日志、匹配失败次数、自动加防火墙规则。适合长期运行，
但**配置成本比前两项高**，单机小站先做第 1 项往往就够。

### 4. 改端口 / 关闭入口

如数据库端口、phpMyAdmin 这类管理入口 —— **能从公网访问的，都应该收回到内网**。

## 第四步：验证是否真的挡住了

**这一步不能省。** 改完必须自己验证，不能假设生效。

```bash
# 从本机验
curl -s -o /dev/null -w "%{http_code}\n" https://你的域名/xmlrpc.php
# 期望：403

# 从公网验（换一台网络环境，或用手机流量）
curl -s -o /dev/null -w "%{http_code}\n" https://你的域名/xmlrpc.php

# 确认站点本身没被误伤
curl -s -o /dev/null -w "%{http_code}\n" https://你的域名/
# 期望：200
```

还要确认**应用自己还收不收得到这些请求** —— 反向代理挡住了，
但如果有别的路径能直达应用，那就没挡全：

```bash
docker logs --since 5m <应用容器> | grep -c xmlrpc
# 期望：0
```

## 几个容易踩的坑

- **只在应用层改，不在代理层拦**。请求已经打到 PHP 上了，
  资源早就消耗掉了 —— 越靠前拦越省。
- **改完不验证**。配置语法没问题 ≠ 生效了；
  一定要从**公网**再请求一次看状态码。
- **把整个 IP 段都封了**。攻击者 IP 和正常用户共用出口段的情况很常见，
  先封单个 IP，观察后再决定是否扩大。
- **重启服务当作处置**。它会回来，而且重启清掉了你判断趋势所需的日志。
- **在公网机器上新开管理端口**。这类事情每多暴露一个端口就多一个入口 ——
  管理后台能走内网就别走公网。
