# Ragent 项目学习（二）：framework 基座详解

> 系列：ragent项目学习2
> 本篇目标：搞懂 framework 模块的 6 大核心能力——SSE 流式推送、RocketMQ 事务消息、幂等控制、分布式 ID、Trace 链路追踪、上下文传递
> 对应源码：`d:\javaCODE\Practical trainning two\ragent\framework`

---

## 一、framework 模块定位

framework 是整个项目的**通用技术底座**，不依赖任何业务模块，被 rag/agent/system 共享。它解决的是 AI 应用开发中**反复出现的工程问题**：

| 问题 | framework 的解法 |
|------|------------------|
| 大模型输出如何流式推给前端？ | SseEmitterSender + StreamTaskManager |
| 入库任务如何异步解耦且保证最终一致？ | RocketMQ 事务消息封装 |
| 用户重复点击上传/取消怎么办？ | @IdempotentSubmit 分布式锁 |
| MQ 消息重复投递怎么办？ | @IdempotentConsume Redis 状态机 |
| 全链路如何追踪定位问题？ | RagTraceContext + @RagTraceNode |
| 异步线程如何拿到登录用户和 Trace？ | TTL（TransmittableThreadLocal） |

---

## 二、SSE 流式推送机制（⭐ 面试重点）

### 2.1 为什么用 SSE 而不是 WebSocket？

| 维度 | SSE | WebSocket |
|------|-----|-----------|
| 协议 | HTTP 长连接，单向（服务端→客户端） | 全双工 |
| 浏览器原生支持 | ✅ EventSource API | ✅ 但需额外库 |
| 自动重连 | ✅ 内置 | ❌ 需自己实现 |
| 适用场景 | **大模型流式输出**（单向推送） | 双向实时通信（聊天、游戏） |
| 代理/CDN 兼容 | ✅ 走标准 HTTP | ❌ 部分代理不支持 |

> **面试话术**："Ragent 用 SSE 做流式输出，因为大模型推理是单向数据流，SSE 基于 HTTP 兼容性好、自动重连、实现简单，比 WebSocket 更贴合场景。"

### 2.2 SseEmitterSender —— 线程安全的 SSE 封装

核心设计：**AtomicBoolean + CAS 保证连接只关闭一次**

```
SseEmitterSender 内部状态机：
  ┌─────────┐  sendEvent()   ┌──────────┐
  │  活跃    │ ──────────────→ │ 发送数据  │
  └────┬────┘                └──────────┘
       │ onCompletion/onTimeout/onError
       ▼
  ┌─────────┐  CAS  compareAndSet(false→true)
  │  已关闭  │  ←──── complete() / fail()
  └─────────┘
```

关键代码要点：
- `emitter.onCompletion/onTimeout/onError` 三个回调统一置 `closed=true`
- `sendEvent()` 发送前先检查 `closed.get()`，已关闭直接 return
- `complete()` 和 `fail()` 都用 `compareAndSet(false, true)`，**幂等关闭**

**为什么要 CAS？** 流式响应场景下，业务线程发完数据调 `complete()`，同时 HTTP 容器线程可能因为超时调 `onTimeout`，两个线程并发关闭会抛异常。CAS 保证只有一个线程真正执行 `emitter.complete()`。

### 2.3 StreamTaskManager —— 跨节点取消管理器（亮点设计）⭐

**问题**：用户在前端点击"停止生成"，这个请求可能落到**另一台服务器节点**上（因为流式连接是有状态的，请求是负载均衡的）。如何让跑在 A 节点的流式任务被 B 节点收到的取消请求终止？

**方案：Redis 标记 + Topic 广播 + 属主校验**

```
用户点"停止" (落到节点B)
    │
    ▼
节点B: cancelByUser(taskId)
    │  ① RBucket.set(cancelKey, requester)  ← Redis 标记取消
    │  ② RTopic.publish(taskId|requester)    ← 广播所有节点
    ▼
所有节点（包括A）收到广播
    │
    ▼
节点A: cancelLocal(taskId, requester)
    │  ① 校验属主：requester == ownerUserId？
    │  ② CAS 置 cancelled=true（只执行一次）
    │  ③ 执行 cancelAction（中断上游流）
    │  ④ 执行 finalizer（补发终止事件 + 保存已生成内容）
    ▼
SSE 连接正常关闭
```

**三个关键设计点**：

1. **属主校验防越权**：taskId 是雪花 ID，时间有序可预测，不能作为访问凭证。必须用 Redis 存 `owner:taskId → userId`，取消时比对发起方是否是属主。系统侧回收（超时/断连）用 `__system__` 占位，无条件放行。

2. **注册时复核**：取消标记可能**先于任务注册到达**（竞态），所以 `register()` 时会检查 Redis 是否已被标记取消，如果是则立即执行 finalizer。

3. **本地缓存 + Redis 双层**：Guava Cache 存本地任务信息（owner、finalizer、cancelAction），Redis 存取消标记和属主。两者配合保证跨节点取消的正确性。

> **面试话术**："流式任务的取消是个典型的分布式问题。我们用 Redis 做跨节点的取消标记和属主存储，用 Redisson Topic 做广播通知。本地用 Guava Cache 存任务的收尾回调。核心是属主校验 + 注册时复核，解决竞态和越权两个问题。"

---

## 三、RocketMQ 事务消息封装

### 3.1 事务消息 HALF/COMMIT 流程回顾

```
Producer                          Broker                        Consumer
  │                                │                              │
  │ ① sendMessageInTransaction    │                              │
  │ ─────────────────────────────→│  (HALF 消息，Consumer 不可见)  │
  │                                │                              │
  │ ② executeLocalTransaction    │                              │
  │   (更新文档状态为 running)      │                              │
  │                                │                              │
  │ ③ COMMIT / ROLLBACK           │                              │
  │ ─────────────────────────────→│                              │
  │                                │ ④ 投递消息给 Consumer          │
  │                                │ ────────────────────────────→│
  │                                │                              │
  │ ⑤ checkLocalTransaction       │ (回查，可能发给任意实例)        │
  │ ←─────────────────────────────│                              │
```

### 3.2 DelegatingTransactionListener —— 通用事务监听器

传统用法是**每个 topic 写一个 @RocketMQTransactionListener**，Ragent 做了统一封装：

| 组件 | 作用 |
|------|------|
| `localTransactionMap<txId, Consumer>` | per-message 的本地事务逻辑，当前实例有效 |
| `checkerMap<topic, TransactionChecker>` | per-topic 的回查逻辑，所有实例共享 |
| `executeLocalTransaction()` | 用 TransactionTemplate 执行本地事务，成功 COMMIT，失败 ROLLBACK |
| `checkLocalTransaction()` | 按 topic 找 checker，查 DB 判断本地事务是否提交 |

**关键约束**（TransactionChecker 接口注释）：
> 回查时 Broker 可能将请求发送到任意实例，因此实现类必须基于消息内容（而非内存状态）查询 DB 判断本地事务是否已提交。

### 3.3 RocketMQProducerAdapter —— 生产者适配器

| 方法 | 说明 |
|------|------|
| `send()` | 普通同步消息，封装 MessageWrapper（带 keys） |
| `sendInTransaction()` | 事务消息：注册 txId → 发 HALF → 本地事务 → COMMIT/ROLLBACK |

**注意**：事务消息发送失败或状态非 SEND_OK 时，必须 `unregisterLocalTransaction(txId)` 清理 Map，避免内存泄漏。

### 3.4 ⚠️ 与你踩过的坑的关联

你之前遇到的"事务消息消费端不触发"问题，根因就在这里：

- `@RocketMQTransactionListener` 注解在 Spring Boot 4.1.0 + starter 2.3.6 下**无法正确注册到 Producer 的回调链**
- HALF 消息发了，本地事务执行了（文档状态变 running），但 COMMIT 没执行
- Broker 看不到 COMMIT，消息对 Consumer 不可见
- 你最终用 `CompletableFuture.runAsync()` 绕过了这个机制

> **面试可以主动提**："我们在 Spring Boot 4 升级时遇到了 RocketMQ starter 兼容性问题，事务监听器注册失败。短期绕过方案是 CompletableFuture 异步执行，长期需要升级 starter 版本。这个经验让我深刻理解了框架版本兼容性的重要性。"

---

## 四、幂等控制（两套方案）

### 4.1 @IdempotentSubmit —— 防重复提交（接口侧）

**场景**：用户快速双击"上传"按钮、网络重试导致同一个请求发两次。

**实现：Redisson 分布式锁**

```
请求进来
  │
  ▼
buildLockKey()
  ├─ 有 key 属性 → SpEL 解析业务 key
  └─ 无 key 属性 → path:userId:argsMD5
  │
  ▼
lock.tryLock()
  ├─ 成功 → 执行方法 → finally unlock
  └─ 失败 → 抛 ClientException("您操作太快")
```

**Key 生成策略**：
- 默认：`idempotent-submit:path:{servletPath}:userId:{userId}:md5:{argsMD5}`
- 自定义：`@IdempotentSubmit(key = "#request.docId")` → 用 SpEL 取业务字段

**特殊处理**：MultipartFile 参数用自定义 Gson 序列化器（只序列化 name/size/contentType，不序列化文件内容），避免 MD5 计算不稳定。

> **面试要点**："防重复提交用的是 Redisson 分布式锁，tryLock 非阻塞获取，获取失败说明有相同请求在执行，直接拒绝。Key 支持 SpEL 自定义，默认用 path+userId+参数MD5 兜底。"

### 4.2 @IdempotentConsume —— 防重复消费（MQ 侧）

**场景**：RocketMQ 至少一次投递（At-Least-Once），消息可能重复消费。

**实现：Redis + Lua 脚本状态机**

状态枚举：
| 状态 | 说明 |
|------|------|
| 空 / 不存在 | 未消费过 |
| `CONSUMING` | 消费中 |
| `CONSUMED` | 已消费完成 |

Lua 脚本（原子操作）：
```lua
-- SET key value NX GET PX expire_time_ms
-- NX: 只有 key 不存在才设置
-- GET: 返回旧值
-- PX: 毫秒过期
return redis.call('SET', key, value, 'NX', 'GET', 'PX', expire_time_ms)
```

状态流转：
```
不存在 ──SET NX成功→ CONSUMING ──方法执行成功→ CONSUMED
                       │
                       └──方法抛异常→ 删除 key（下次可重试）
                       
SET NX失败（已存在）:
  ├─ 旧值=CONSUMING → 抛异常（让 MQ 延迟重试）
  └─ 旧值=CONSUMED  → 直接 return null（跳过）
```

**面试要点**：
- 为什么用 Lua？保证 `SET NX GET PX` 原子性，避免并发竞态
- 为什么消费中要抛异常而不是跳过？因为可能是之前消费失败的重试，抛异常让 MQ 延迟重试
- 为什么已消费直接跳过？保证幂等，同一条消息只处理一次

---

## 五、分布式 ID（雪花算法）

### 5.1 CustomIdentifierGenerator

```java
@Override
public Number nextId(Object entity) {
    return IdUtil.getSnowflakeNextId();  // Hutool 封装
}
```

- 实现 MyBatis-Plus 的 `IdentifierGenerator` 接口，替换默认策略
- 基于 Hutool 的 `IdUtil.getSnowflakeNextId()`，本质是雪花算法

### 5.2 雪花算法结构（64 位）

```
0 | 41位时间戳 | 10位机器ID | 12位序列号
```

| 部分 | 位数 | 说明 |
|------|------|------|
| 符号位 | 1 | 固定 0 |
| 时间戳 | 41 | 毫秒级，可用约 69 年 |
| 机器 ID | 10 | 最多 1024 个节点 |
| 序列号 | 12 | 同一毫秒内最多 4096 个 ID |

> **面试要点**："雪花 ID 的优点是趋势递增、不依赖数据库、性能高。Ragent 用 Hutool 封装，配置简单。要注意的是机器 ID 不能重复，否则会冲突。"

---

## 六、Trace 链路追踪

### 6.1 RagTraceContext —— Trace 上下文

```
RagTraceContext (基于 TTL 透传)
  ├─ TRACE_ID: String          全链路唯一 ID
  ├─ TASK_ID: String           当前流式任务 ID
  └─ NODE_STACK: Deque<String> 调用节点栈（维护父子关系）
```

**关键设计：NODE_STACK 的深拷贝**

```java
private static final TransmittableThreadLocal<Deque<String>> NODE_STACK = 
    new TransmittableThreadLocal<>() {
        @Override
        public Deque<String> copy(Deque<String> parentValue) {
            // 必须深拷贝！否则父子线程共用同一个 Deque，
            // 并发 push/pop 会导致 trace 层级串挂
            return parentValue == null ? null : new ArrayDeque<>(parentValue);
        }
    };
```

**为什么 TTL？** 普通 ThreadLocal 在线程池场景下会丢失上下文。TTL（TransmittableThreadLocal）是阿里开源的，能在 `Runnable`/`Callable` 提交到线程池时自动捕获并传递父线程的值。

### 6.2 @RagTraceNode —— 节点注解

```java
@RagTraceNode(name = "multi-channel-retrieval", type = "RETRIEVE_CHANNEL")
public KnowledgeRetrievalResult retrieveKnowledgeChannels(...) { ... }
```

标注在方法上，由 AOP 拦截记录节点的开始/结束时间、输入输出、状态，最终存入 `t_rag_trace_run` + `t_rag_trace_node` 两张表。

> **面试要点**："Trace 用 TTL 在异步线程间传递 traceId 和节点栈。节点栈维护调用层级，注意 TTL 默认是引用拷贝，节点栈必须重写 copy() 做深拷贝，否则并发下父子节点 ID 会串挂。"

---

## 七、上下文传递（UserContext）

### 7.1 UserContext —— 用户上下文

```java
private static final TransmittableThreadLocal<LoginUser> CONTEXT = new TransmittableThreadLocal<>();
```

提供 `get/set/clear/requireUser/getUserId` 等方法。**同样基于 TTL**，保证异步线程（CompletableFuture、MQ 消费者）能拿到登录用户。

**使用场景**（你改过的代码）：
```java
CompletableFuture.runAsync(() -> {
    UserContext.set(LoginUser.builder().username(username).build());
    executeChunk(docId);
});
```

> ⚠️ **面试踩坑点**：异步线程里必须手动 `UserContext.set()`，因为 TTL 只在通过 TtlRunnable/TtlCallable 包装时才自动传递。如果直接 `CompletableFuture.runAsync()` 不包装，子线程拿不到父线程的 UserContext。

---

## 八、异常体系

### 8.1 异常分层

```
AbstractException (抽象基类，含 errorCode + errorMessage)
  ├─ ClientException    客户端错误（参数不对、越权等）
  ├─ ServiceException   业务异常（幂等冲突、状态不对等）
  └─ RemoteException    远程调用异常（模型 API 失败等）
```

### 8.2 GlobalExceptionHandler —— 全局异常处理

| 拦截异常 | 处理方式 |
|----------|----------|
| MethodArgumentNotValidException | 参数校验失败，返回第一个字段错误 |
| HandlerMethodValidationException | 方法参数校验失败 |
| AbstractException | 业务异常，返回 errorCode + message |
| NotLoginException / NotRoleException | Sa-Token 认证失败 |
| MaxUploadSizeExceededException | 文件上传超限，提示大小限制 |
| Throwable | 兜底，返回"系统执行出错" |

> **面试要点**："异常分三层——ClientException（4xx）、ServiceException（业务）、RemoteException（远程调用）。全局异常处理器统一包装成 Result 返回，前端不用解析 HTTP 状态码，看 code 字段就行。"

---

## 九、设计模式总结

| 设计模式 | 应用位置 | 作用 |
|----------|----------|------|
| 适配器模式 | RocketMQProducerAdapter | 统一 MQ 接口，便于切换实现 |
| 策略模式 | TransactionChecker | 按 topic 注册不同回查逻辑 |
| 模板方法 | DelegatingTransactionListener | 固定事务流程，回查逻辑可扩展 |
| AOP 切面 | IdempotentSubmitAspect / IdempotentConsumeAspect | 幂等逻辑零侵入 |
| CAS 原子操作 | SseEmitterSender / StreamTaskManager | 保证关闭只执行一次 |
| 状态机 | IdempotentConsume | CONSUMING → CONSUMED 流转 |

---

## 十、面试高频问题（framework 篇）

### Q1：SSE 和 WebSocket 怎么选？
> 大模型流式输出是单向数据流，SSE 基于 HTTP 兼容性好、自动重连、实现简单。WebSocket 适合双向通信场景。

### Q2：流式任务跨节点取消怎么实现？
> Redis 存取消标记和属主，Redisson Topic 广播通知所有节点。本地 Guava Cache 存收尾回调。核心是属主校验（防越权）+ 注册时复核（防竞态）+ CAS 只执行一次。

### Q3：RocketMQ 事务消息怎么保证最终一致？
> HALF 消息 → 本地事务 → COMMIT/ROLLBACK。如果本地事务状态不确定，Broker 会回查，回查逻辑必须查 DB（不能看内存）。

### Q4：接口幂等怎么实现？
> 防重复提交用 Redisson 分布式锁，tryLock 非阻塞。防重复消费用 Redis + Lua 状态机（CONSUMING/CONSUMED）。Key 支持 SpEL 自定义。

### Q5：异步线程怎么传递上下文？
> 用 TransmittableThreadLocal（TTL），它在 Runnable 提交到线程池时自动捕获父线程值。但要注意：直接 CompletableFuture.runAsync() 不会自动传递，需要手动 set 或用 TtlRunnable 包装。

### Q6：雪花算法的机器 ID 怎么保证不重复？
> 通常用机器 IP 哈希、配置文件指定、或注册中心分配。Ragent 用 Hutool 默认实现，简单场景够用。大规模部署需要专门的机器 ID 分配服务。

---

## 十一、本篇小结

framework 模块是 Ragent 的**工程根基**，核心价值在于：
1. **SSE 流式 + 跨节点取消**：解决大模型输出实时推送和用户主动停止的分布式问题
2. **事务消息封装**：统一 RocketMQ 事务消息，支持多 topic 回查
3. **双幂等**：接口侧（分布式锁）+ MQ 侧（Redis 状态机），保证不重复提交/消费
4. **TTL 上下文**：Trace 和用户信息在异步线程间正确传递
5. **雪花 ID + 全局异常**：基础设施完备

下一篇预告：**学习3 —— infra-ai 模型路由详解**（ModelSelector 选择策略、ModelRoutingExecutor 执行、ModelHealthStore 熔断、档位路由、首包探测）。
