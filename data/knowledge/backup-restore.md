# MariaDB / MySQL 数据备份与恢复

## 现象

需要迁移机器、升级数据库、担心误操作，或者已经发生了误删 ——
这时候才想起来没备份，就已经晚了。

**这篇讲的是"怎么做"，不是"要不要做"。** 备份这件事的价值在出事那一刻才显现。

## 先理解：备份要做在宿主机上，不是容器里

数据库跑在容器里，但**备份命令应该在宿主机上执行**：

```bash
# 在宿主机上，通过 docker exec 让容器里的 mysqldump 输出，重定向到宿主机文件
docker exec <db容器> mysqldump -uroot -p'<密码>' <库名> > /root/backup_$(date +%F).sql
```

这样做的好处：

- 备份文件落在**宿主机的文件系统**，不随容器删除而消失
- 不需要进容器交互，脚本化方便
- 不用把数据库端口暴露出来

> 反例：进容器里 `mysqldump > /tmp/x.sql` —— 文件在容器里，
> 容器一删就没了，等于没备份。

## 第一步：备份

```bash
# 单库备份（最常用）
docker exec <db容器> mysqldump -uroot -p'<密码>' wordpress > /root/wp_$(date +%F).sql

# 带存储过程/触发器/事件，并锁表保证一致性
docker exec <db容器> mysqldump -uroot -p'<密码>' \
    --routines --triggers --events --single-transaction \
    wordpress > /root/wp_$(date +%F).sql

# 压缩（文本文件压缩率很高，通常能到 1/5）
gzip /root/wp_$(date +%F).sql

# 全库备份
docker exec <db容器> mysqldump -uroot -p'<密码>' --all-databases > /root/all_$(date +%F).sql
```

`--single-transaction` 对 InnoDB 很重要：**不加它可能锁表**，
备份期间业务写入会被阻塞。

## 第二步：验证备份能用

**没验证过的备份不算备份。** 至少看两件事：

```bash
# 1. 大小是否合理（一个空文件说明命令其实失败了）
ls -lh /root/wp_*.sql.gz

# 2. 内容是不是真的 SQL（看头尾）
zcat /root/wp_2026-09-29.sql.gz | head -20
zcat /root/wp_2026-09-29.sql.gz | tail -5
```

文件尾部应该能看到类似 `/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;` 的收尾语句 ——
**没有的话说明导出中途断了**，这个备份是不可用的。

## 第三步：恢复到一台新机器

```bash
# 1. 把备份文件传到目标机器（宿主机上）
scp /root/wp_2026-09-29.sql.gz root@目标机器:/root/

# 2. 解压
gunzip /root/wp_2026-09-29.sql.gz

# 3. 导进容器里的数据库（用重定向，不要交互式粘贴）
docker exec -i <db容器> mysql -uroot -p'<密码>' wordpress < /root/wp_2026-09-29.sql
```

注意是 `docker exec -i`（**带 -i**，保持标准输入打开），
不加 `-i` 的话重定向进去的内容进不去容器。

```bash
# 4. 验证：看表和行数
docker exec <db容器> mysql -uroot -p'<密码>' -e "USE wordpress; SHOW TABLES;"
docker exec <db容器> mysql -uroot -p'<密码>' -e "SELECT COUNT(*) FROM wordpress.wp_posts;"
```

## 第四步：自动化（别靠记得）

```bash
# /etc/cron.daily/db-backup（每天跑一次）
#!/bin/sh
set -e
DIR=/root/backups
mkdir -p "$DIR"
docker exec wp-db mysqldump -uroot -p'<密码>' --single-transaction wordpress \
    | gzip > "$DIR/wp_$(date +%F).sql.gz"
# 只保留最近 14 天
find "$DIR" -name 'wp_*.sql.gz' -mtime +14 -delete
```

关键的两点：

- **保留多份**（只留最新一份的话，误删后同步覆盖，等于没备份）
- **留在本机是不够的** —— 机器没了备份也没了，至少再同步一份到别处

## 常见根因速查

| 问题 | 原因 / 处理 |
|---|---|
| 备份文件是空的 | 密码错了或库名错了；`mysqldump` 失败但被管道吞掉了 → 去掉管道先看报错 |
| 备份期间业务卡住 | 缺 `--single-transaction`（MyISAM 表不适用） |
| 恢复后中文乱码 | 导出与导入的字符集不一致，两边都加 `--default-character-set=utf8mb4` |
| `Access denied` | 用的账号没有 `LOCK TABLES` 或 `SELECT` 权限 |
| 备份在容器里找不到 | 你是进容器里导出的，文件在容器文件系统里 |

## 几个容易踩的坑

- **只在容器里导出，不落到宿主机**。容器一重建，备份跟着没了。
- **从不验证备份**。等真要恢复时才发现文件是空的 —— 这是最贵的一种疏忽。
- **只保留一份且覆盖写**。误删后的下一次自动备份会把好的那份覆盖掉。
- **备份文件放在同一块盘上**。盘坏了两边一起没，等于没备份。
- **恢复前不确认目标库是空的**。重复导入会造成主键冲突，
  先确认再导，或者先 `DROP DATABASE` 再重建。
