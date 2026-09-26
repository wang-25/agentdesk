# Docker 容器退出码含义与排查

## 退出码速查

| 退出码 | 含义 | 典型原因 |
|---|---|---|
| `0` | 正常退出 | 容器主进程干完活正常结束 |
| `1` | 应用错误 | 程序自己抛异常退出，看应用日志 |
| `2` | shell 用法错误 | 启动脚本参数写错 |
| `126` | 命令无法执行 | 文件没有执行权限 |
| `127` | 找不到命令 | 入口命令不存在，或 PATH 不对 |
| `128` | 无效退出码 | 退出码本身不合法 |
| `137` | **被 SIGKILL 杀掉（128+9）** | **被 OOM Killer 杀，或 `docker stop` 超时后被强杀** |
| `139` | 段错误（128+11） | 程序内存访问越界 |
| `143` | 收到 SIGTERM（128+15） | 正常的 `docker stop`，进程优雅退出 |

**137 和 143 是最需要区分的两个**：143 是正常停止流程，137 说明进程是被强杀的。

## 排查步骤

```bash
# 1. 看退出码
docker ps -a
# STATUS 列会显示 "Exited (137) 3 minutes ago"

# 2. 看容器日志
docker logs --tail 200 <container>

# 3. 看容器的详细状态（重点看 OOMKilled 字段）
docker inspect <container> --format '{{json .State}}' | python -m json.tool
# 关注 "OOMKilled": true/false, "ExitCode", "Error"

# 4. 看宿主机内核日志里有没有 OOM 记录
dmesg -T | grep -i -E "oom|killed process"

# 5. 看资源限制
docker inspect <container> --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}'
```

## 137 的三种成因

**① 内存超限被 OOM Killer 杀（最常见）**

`docker inspect` 里 `OOMKilled: true` 就是这个。
容器用的内存超过 `--memory` 限制，内核把容器里的主进程杀掉。

```bash
docker run --memory=512m --memory-swap=512m ...
```

注意 `--memory-swap` 如果等于 `--memory`，等于禁用 swap，容器更容易被 OOM。
不设 `--memory-swap` 时，swap 默认是 memory 的两倍。

**② `docker stop` 超时**

`docker stop` 默认先发 SIGTERM，等 10 秒；如果进程没退出，再发 SIGKILL ——
这时退出码就是 137。这说明**应用没有正确处理 SIGTERM**。

处理方式：在应用里捕获 SIGTERM 做优雅退出；或调整超时
`docker stop -t 30 <container>`。

**③ 宿主机整体内存不足**

容器自己没有超限，但宿主机内存耗尽，内核仍然会杀进程。
这种情况要看宿主机：`free -m`、`dmesg -T | grep oom`。

## 127 与 126

- `127`：入口命令不存在。常见于 `ENTRYPOINT` 路径写错、或者镜像里没有这个二进制。
- `126`：命令存在但不可执行。常见于文件没有 `+x` 权限，或文件系统挂载了 `noexec`。

```bash
# 进镜像看看命令到底在不在、有没有权限
docker run --rm --entrypoint sh <image> -c "ls -l /path/to/cmd; which cmd"
```

## 0 但服务不可用

容器正常退出，但你想让它一直跑着。原因是 **容器主进程必须在前台运行** ——
容器生命周期跟着 PID 1 走。如果主进程 fork 到后台就退出了，容器也跟着退。

```dockerfile
# ❌ 主进程会立刻退出
CMD service nginx start

# ✅ 主进程保持前台
CMD ["nginx", "-g", "daemon off;"]
```

## 稳定运行的几条经验

- 给容器设置 `--memory` 限制，并**留出足够余量**（实际用量的 1.5~2 倍）
- 配置 `--restart unless-stopped`，进程崩了能自动拉起
- 开启健康检查 `HEALTHCHECK`，让编排系统能发现"进程活着但服务不可用"
- 容器日志一定要限制大小（`max-size` / `max-file`），否则日志会占满磁盘
- 应用要正确处理 SIGTERM，避免每次都靠 SIGKILL 强杀
