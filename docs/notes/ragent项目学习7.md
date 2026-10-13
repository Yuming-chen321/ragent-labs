# Ragent 项目学习（七）：流量治理与可观测详解

> 系列：ragent项目学习7
> 本篇目标：搞懂 Ragent 的流量治理（Redis 公平排队、信号量、幂等）和可观测体系（全链路 Trace）
> 对应源码：`rag/src/main/java/.../ratelimit` + `framework/trace` + `rag/aop`

---

## 一、流量治理全景

Ragent 的流量治理解决三个核心问题：

| 问题 | 解决方案 | 组件 |
|------|----------|------|
| 大模型 API 并发限制 | Redis 公平排队 + 信号量 | FairDistributedRateLimiter + ChatQueueLimiter |
| 重复请求/重复消费 | 幂等注解 + AOP | @IdempotentSubmit / @IdempotentConsume |
| Worker 宕机资源泄漏 | permit 自动过期 + entry TTL | RPermitExpirableSemaphore + entry marker |

```
用户请求
    │
    ▼
ChatQueueLimiter.enqueue()
    │
    ├─ globalEnabled=false → 直通（chatEntryExecutor 执行）
    │
    └─ globalEnabled=true → FairDistributedRateLimiter.acquire()
         │
         ├─ 有 permit → onAcquired（执行业务）
         ├─ 排队超时 → onTimeout（REJECT + DONE）
         └─ 取消 → cleanup（移队 + 释放）
```

---

## 二、FairDistributedRateLimiter —— 分布式公平限流器（⭐ 核心）

### 2.1 核心数据结构（Redis）

| Key | 类型 | 作用 |
|-----|------|------|
| `{name}:semaphore` | RPermitExpirableSemaphore | 信号量，permit 有 lease 时间 |
| `{name}:queue` | RScoredSortedSet (ZSET) | 公平排队队列，score=入队序号 |
| `{name}:queue:seq` | RAtomicLong | 入队序号生成器 |
| `{name}:queue:notify` | RTopic | permit 释放时广播通知 |
| `{name}:entry:{requestId}` | RBucket | 存活标记，TTL=等待预算+5s |

### 2.2 公平排队流程

```
请求到达
    │
    ▼
① setEntryMarker(requestId, TTL)  ← 先写存活标记
② ZADD queue (seq, requestId)     ← 入队
    │
    ▼
③ tryAcquireIfReady(ticket)
    │
    ├─ availablePermits <= 0 → scheduleQueuePoll（定时轮询）
    │
    ├─ claimIfReady (Lua 原子)  ← 取队头 + 清理僵尸
    │   ├─ 不是队头 → return false（继续等）
    │   └─ 是队头 → 返回 score
    │
    ├─ tryAcquirePermit (信号量)
    │   ├─ 失败 → 按原 score 重入队（保留位次）
    │   └─ 成功 → ticket.grant(permitId)
    │
    └─ grant → 执行业务 → finally 释放 permit
```

### 2.3 Ticket 状态机

```
        ┌──────────┐
        │ PENDING  │ ← 初始状态
        └────┬─────┘
             │
   ┌─────────┼─────────┐
   ▼         ▼         ▼
GRANTED  TIMED_OUT  CANCELLED
 (终态)    (终态)     (终态)
```

**CAS 协调**：状态一旦从 PENDING 转走就不再变更，业务回调最多触发一次。

**grant 与 cancel 的竞争处理**：
```java
boolean grant(String permitId) {
    permitRef.set(permitId);                    // 先 set permit
    if (!state.compareAndSet(PENDING, GRANTED)) {
        // 已被 cancel/timeout 抢占
        if (permitRef.compareAndSet(permitId, null)) {
            releasePermitQuietly(permitId);    // 释放 permit 防泄漏
        }
        return false;
    }
    // 正常授权，业务执行完在 finally 释放
}
```

**为什么先 set permit 再 CAS？** 让并发 cancel/timeout 路径在 CAS 失败时能看到 permit 并正确释放，避免 permit 泄漏。

### 2.4 Lua 原子 claim（claimIfReady）

`lua/queue_claim_atomic.lua` 做三件事（原子）：
1. 取队头（ZRANGE 0,0）
2. 检查队头是否是当前 requestId
3. 清理僵尸条目（entry marker 不存在的 ZREM）

**为什么需要僵尸清理？** JVM 崩溃后 entry marker 自然过期，但 ZSET 条目还在，会永久占据队头。Lua 内检查 entry 是否存在，不存在就 ZREM。

### 2.5 RTopic 通知机制

```
permit 释放 → publishQueueNotify() → RTopic 广播
    │
    ▼
所有节点的 PollNotifier.fire()
    │
    ├─ permit <= 0 → 不扫描（等下一次 release）
    └─ permit > 0 → 遍历所有 poller，尝试抢占
```

**通知合并**：`firing` CAS + `pendingNotifications` 计数，连续多次通知只触发一次扫描，避免风暴。

### 2.6 为什么不用简单分布式锁？

| 对比 | 简单分布式锁 | 公平排队限流器 |
|------|-------------|---------------|
| 公平性 | 无（随机抢占） | 有（FIFO） |
| 超时处理 | 锁超时释放 | 排队超时 + 自动拒绝 |
| 取消支持 | 无 | emitter 取消即移队 |
| 僵尸处理 | 锁 TTL | entry TTL + Lua 清理 |
| 通知 | 无（轮询等待） | RTopic 广播唤醒 |

> **面试要点**："公平限流器用 Redis ZSET 做 FIFO 队列，RPermitExpirableSemaphore 做信号量。permit 有 lease 时间防泄漏，entry marker 有 TTL 处理 JVM 崩溃的僵尸条目。Lua 脚本原子完成取队头+清理僵尸。permit 释放时通过 RTopic 广播唤醒所有等待者。"

---

## 三、ChatQueueLimiter —— SSE 限流入口

### 3.1 核心流程

```java
public void enqueue(question, conversationId, emitter, onAcquire) {
    if (!globalEnabled) {
        chatEntryExecutor.execute(onAcquire);  // 直通
        return;
    }
    chatRateLimiter.acquire(AcquireRequest.builder()
        .maxWaitMillis(globalMaxWaitSeconds)
        .onAcquired(TtlRunnable.get(onAcquire))           // 拿到 permit
        .onTimeout(TtlRunnable.get(() -> handleReject()))  // 排队超时
        .onAcquiredExecutor(chatEntryExecutor)
        .cancelBinder(cancel -> {                          // emitter 生命周期绑定
            emitter.onCompletion(cancel);
            emitter.onTimeout(cancel);
            emitter.onError(e -> cancel.run());
        })
        .build());
}
```

**TtlRunnable**：包装 Runnable，保证 TransmittableThreadLocal（UserContext、RagTraceContext）在线程池切换时不丢失。

### 3.2 拒绝流程（handleReject）

排队超时不直接抛异常，而是：
1. **记录拒绝会话**：把用户问题和"系统繁忙"回复存入会话历史
2. **发 SSE 事件**：META → REJECT → FINISH → DONE
3. **前端展示**：用户看到"系统繁忙，请稍后再试"，但对话记录保留

> **面试要点**："限流入口绑定 SSE emitter 生命周期——emitter 完成/超时/出错时自动取消排队。排队超时不抛异常，而是记录拒绝会话并发 REJECT 事件，用户体验友好。"

---

## 四、幂等控制（两套）

### 4.1 @IdempotentSubmit —— 接口幂等

基于 Redisson 分布式锁：
- SpEL 表达式生成 key（如 `#userId + ':' + #conversationId`）
- 拿到锁 → 执行业务；拿不到 → 抛"重复请求"
- 锁自动过期防死锁

### 4.2 @IdempotentConsume —— 消费幂等

基于 Redis + Lua 状态机：
```
INIT → PROCESSING → SUCCESS
              │
              └→ FAILED
```
- INIT：首次消费，标记 PROCESSING
- PROCESSING：正在处理，拒绝重复
- SUCCESS/FAILED：终态，直接返回结果

---

## 五、可观测体系：全链路 Trace

### 5.1 Trace 两级结构

```
t_rag_trace_run（运行级）
    ├─ trace_id（雪花 ID）
    ├─ user_id, conversation_id
    ├─ question, status, duration_ms
    └─ error_message
         │
         ▼
    t_rag_trace_node（节点级，树形）
         ├─ node_id, parent_node_id, depth
         ├─ node_type（RETRIEVAL/LLM/EMBEDDING...）
         ├─ node_name, class_name, method_name
         ├─ status（RUNNING/SUCCESS/ERROR）
         └─ duration_ms, error_message
```

### 5.2 RagTraceContext —— 上下文透传

```java
// TransmittableThreadLocal 保证异步线程池透传
TRACE_ID      → traceId
TASK_ID       → taskId
NODE_STACK    → 节点栈（Deque），深拷贝防止父子线程串挂
```

**NODE_STACK 深拷贝的坑**：
```java
// 默认 TTL copy() 返回父值引用，并发子任务共用同一个 Deque
// 互相 push/pop 时父子节点 ID 串挂，trace 层级紊乱
private static final TransmittableThreadLocal<Deque<String>> NODE_STACK = 
    new TransmittableThreadLocal<>() {
        @Override
        public Deque<String> copy(Deque<String> parentValue) {
            return parentValue == null ? null : new ArrayDeque<>(parentValue);  // 深拷贝
        }
    };
```

### 5.3 @RagTraceNode + AOP 采集

```java
@Around("@annotation(traceNode)")
public Object aroundNode(joinPoint, traceNode) {
    String nodeId = snowflakeId();
    String parentNodeId = RagTraceContext.currentNodeId();
    int depth = RagTraceContext.depth();
    
    // startNode：插入 RUNNING 状态
    traceRecordService.startNode(builder()
        .traceId(traceId).nodeId(nodeId)
        .parentNodeId(parentNodeId).depth(depth)
        .nodeType(traceNode.type()).nodeName(traceNode.name())
        .className(method.getDeclaringClass().getName())
        .methodName(method.getName())
        .status("RUNNING").startTime(new Date())
        .build());
    
    RagTraceContext.pushNode(nodeId);
    try {
        Object result = joinPoint.proceed();
        traceRecordService.finishNode(traceId, nodeId, "SUCCESS", null, ...);
        return result;
    } catch (Throwable ex) {
        traceRecordService.finishNode(traceId, nodeId, "ERROR", truncateError(ex), ...);
        throw ex;
    } finally {
        RagTraceContext.popNode();
    }
}
```

**采集内容**：traceId、nodeId、parentNodeId、depth、nodeType、nodeName、className、methodName、status、startTime、endTime、durationMs、errorMessage。

**业务零侵入**：只需在方法上加 `@RagTraceNode(name="xxx", type="RETRIEVAL")`。

### 5.4 Trace 生命周期

```
请求入口
    │
    ├─ RagTraceContext.setTraceId(traceId)
    ├─ traceRecordService.startRun(...)        ← 插入 run 记录
    │
    ├─ 业务执行（@RagTraceNode 自动采集节点）
    │
    └─ traceRecordService.finishRun(...)       ← 更新 run 状态
```

管理后台 `admin/traces` 页面可视化展示：
- run 列表（按用户/会话/状态筛选）
- 节点树形展开（每个节点的耗时、状态、错误）
- 慢节点高亮（超过阈值标红）

> **面试要点**："Trace 分两级——run（一次完整问答）和 node（每个方法调用）。用 TransmittableThreadLocal 透传 traceId 和节点栈，节点栈必须深拷贝防止并发串挂。@RagTraceNode 注解 + AOP 零侵入采集，记录父子关系、深度、类型、耗时、错误。"

---

## 六、配置速查

```yaml
# 限流
rag:
  rate-limit:
    global-enabled: true
    global-max-wait-seconds: 10    # 排队最长等 10 秒
  semaphore:
    document-upload:
      name: rag:document:upload
      max-concurrent: 10
      max-wait-seconds: 5
      lease-seconds: 300

# Trace
rag:
  trace:
    enabled: true
    max-error-length: 500
```

---

## 七、设计模式总结

| 设计模式 | 应用位置 | 作用 |
|----------|----------|------|
| 状态机 | Ticket（PENDING/GRANTED/TIMED_OUT/CANCELLED） | 状态互斥，CAS 协调 |
| 观察者 | RTopic + PollNotifier | permit 释放广播唤醒 |
| 模板方法 | RagTraceAspect | startNode → proceed → finishNode 固定骨架 |
| AOP | @RagTraceNode / @IdempotentSubmit | 横切逻辑零侵入 |
| 装饰器 | TtlRunnable | 透传 TTL 上下文 |
| CAS | Ticket.grant/cancel/timeout | 并发安全的状态转换 |

---

## 八、面试高频问题（流量治理与可观测篇）

### Q1：你们怎么做并发限流的？
> 用 Redis 实现的分布式公平限流器。ZSET 做 FIFO 排队队列，RPermitExpirableSemaphore 做信号量。请求先入队，Lua 脚本原子取队头+清理僵尸，拿到 permit 才执行业务。permit 释放时通过 RTopic 广播唤醒等待者。排队超时发 REJECT 事件，不抛异常。

### Q2：为什么用公平排队而不是简单限流？
> 大模型 API 有并发限制，简单限流（如令牌桶）会让后来的请求随机抢到 permit，不公平。公平排队保证先到先服务，用户体验更好。而且支持取消——SSE emitter 关闭时自动移队，不占资源。

### Q3：Worker 宕机了 permit 会泄漏吗？
> 不会。两层防护：① RPermitExpirableSemaphore 的 permit 有 lease 时间（300s），到期自动释放；② entry marker 有 TTL（等待预算+5s），JVM 崩溃后 Lua 脚本会清理对应的 ZSET 僵尸条目。

### Q4：幂等怎么实现的？
> 两套：① 接口幂等 @IdempotentSubmit，基于 Redisson 分布式锁，SpEL 生成 key；② 消费幂等 @IdempotentConsume，基于 Redis+Lua 状态机（INIT→PROCESSING→SUCCESS/FAILED）。

### Q5：全链路 Trace 怎么设计的？
> 两级结构——run（一次问答）和 node（方法调用，树形）。用 TransmittableThreadLocal 透传 traceId 和节点栈。@RagTraceNode 注解 + AOP 自动采集节点的父子关系、深度、类型、耗时、错误。节点栈必须深拷贝，防止并发子任务共用同一个 Deque 导致层级串挂。

### Q6：SSE 请求取消了怎么处理？
> ChatQueueLimiter 把 cancel 绑定到 emitter 的 onCompletion/onTimeout/onError。emitter 关闭时自动调用 ticket.cancel()，移队+释放 permit。如果已经 GRANTED，permit 由业务的 try/finally 释放，cancel 不会跨界释放（否则会导致并发请求拿到正在使用的 permit）。

---

## 九、本篇小结

流量治理与可观测的核心价值是**稳定性 + 可排查**：

1. **公平排队**：Redis ZSET FIFO + 信号量，先到先服务，支持取消
2. **防泄漏**：permit lease + entry TTL + Lua 僵尸清理
3. **幂等**：接口幂等（分布式锁）+ 消费幂等（状态机）
4. **全链路 Trace**：run + node 两级，TTL 透传，AOP 零侵入
5. **友好拒绝**：排队超时记录会话 + 发 REJECT 事件，不抛异常

下一篇预告：**学习8 —— 面试终极整理**（高频问题汇总 + 项目串讲 + 简历包装建议）。
