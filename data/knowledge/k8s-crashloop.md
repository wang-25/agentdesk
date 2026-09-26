# Kubernetes Pod CrashLoopBackOff 排查

## 先理解 CrashLoopBackOff 是什么

它不是一种错误，而是 **kubelet 的一种退避策略**：
容器启动后很快退出，kubelet 反复重启它，并且**每次失败后等待时间翻倍**（10s → 20s → 40s……最多 5 分钟）。

所以看到 CrashLoopBackOff，第一件事是：**去查容器为什么退出，而不是盯着这个状态。**

## 第一步：把退出原因看清楚

```bash
# 看 Pod 状态和重启次数（RESTARTS 列）
kubectl get pods -o wide

# 看 Pod 的事件和上一次退出的原因
kubectl describe pod <pod-name>
# 重点看三处：Last State（上次退出的退出码/原因）、Exit Code、Events

# 看当前容器的日志
kubectl logs <pod-name>

# ★ 看上一次崩溃前的日志 —— 容器在反复重启时，当前日志往往是空的
kubectl logs <pod-name> --previous

# 多容器 Pod 要指定容器
kubectl logs <pod-name> -c <container> --previous
```

**`--previous` 是这一步的关键。** 很多人只跑 `kubectl logs` 看到空白就卡住了。

## 第二步：按退出码分类

| 退出码 | 含义 | 排查方向 |
|---|---|---|
| `1` | 应用自身报错 | 看应用日志：配置错、依赖连不上、端口被占 |
| `137` | 被 SIGKILL（OOM） | `describe` 里看 `Reason: OOMKilled`；调大 memory limit |
| `143` | SIGTERM | 被正常停止；看探针是不是配置过严 |
| `126` / `127` | 命令无法执行 / 不存在 | 查 entrypoint、镜像里有没有这个二进制 |

```bash
# 确认是不是被 OOM 杀的
kubectl describe pod <pod-name> | grep -A3 -i "last state"
# 看到 Reason: OOMKilled 就是内存不够
```

## 第三步：四类最常见根因

### ① 探针配置过严（最隐蔽）

liveness 探针检查失败 → kubelet 杀掉容器 → 重启 → 又失败 → CrashLoopBackOff。

**特征**：应用日志看起来一切正常，但容器就是反复重启。

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 30    # ★ 应用启动慢就一定要给足
  periodSeconds: 10
  failureThreshold: 3
  timeoutSeconds: 5
```

检查方法：
```bash
kubectl describe pod <pod-name> | grep -A5 -i probe
# 看到 "Liveness probe failed" 就是这个问题
```

**注意**：`initialDelaySeconds` 小于应用的实际启动时间，就会出现
"还没启动完就被判定为不健康" 的死循环。

### ② 资源配置不足

```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "250m"
  limits:
    memory: "512Mi"     # 超了就被 OOMKill
    cpu: "500m"
```

**CPU 超限是限流（变慢），内存超限是直接杀掉。** 两者的表现完全不同。

### ③ 依赖没就绪

应用启动时要连数据库 / Redis，但对方还没起来。
容器一启动就报错退出 → 重启 → 还是连不上。

**处理**：加 `initContainer` 等待依赖，或在应用里做重试 + 指数退避。
用 `depends_on` 在 K8s 里是无效的（那是 Docker Compose 的语法）。

### ④ 配置缺失

ConfigMap / Secret 没挂上、环境变量没定义、挂载路径覆盖了应用目录。
这种通常退出码是 1，日志里能看到明确的 "no such file" 或 "connection refused"。

```bash
# 进容器看看实际环境（如果容器还能起来一会儿）
kubectl exec -it <pod-name> -- env
kubectl exec -it <pod-name> -- ls -l /etc/config
```

## 一个高频踩坑：挂载覆盖了应用目录

```yaml
volumeMounts:
  - name: config
    mountPath: /app        # ❌ 这会把镜像里 /app 的内容全部遮住
```

挂载点是**覆盖**而不是合并。正确做法是把配置挂到子目录：

```yaml
    mountPath: /app/config
```

## 排查顺序速查

```
1. kubectl describe pod        → 看 Exit Code、Reason、Events
2. kubectl logs --previous     → 看崩溃前的真实日志（不要漏 --previous）
3. 按退出码分类：
     1   → 应用/配置问题
     137 → 内存不够，调 limit
     143 → 探针或调度问题
4. 检查探针参数（initialDelaySeconds 是否给够）
5. 检查 resources.limits（内存尤其）
6. 检查依赖服务是否可达、配置是否正确挂载
```
