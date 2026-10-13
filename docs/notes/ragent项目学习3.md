# Ragent 项目学习（三）：infra-ai 模型路由详解

> 系列：ragent项目学习3
> 本篇目标：搞懂 infra-ai 模块的模型路由体系——选择策略、故障转移、熔断机制、档位路由、首包探测
> 对应源码：`d:\javaCODE\Practical trainning two\ragent\infra-ai`

---

## 一、infra-ai 模块定位

infra-ai 是 Ragent 的**模型接入层**，对上（rag/agent）提供统一的模型服务接口，对下封装多家供应商的 API 调用。核心价值是**供应商无关性**和**高可用性**：

```
rag/agent 业务层
    │  调用 LLMService / EmbeddingService / RerankService / VlmService 接口
    ▼
infra-ai 模型接入层
    ├─ ModelSelector        选择候选模型列表
    ├─ ModelRoutingExecutor  执行 + 故障转移
    ├─ ModelHealthStore      熔断状态管理
    └─ ChatClient/EmbeddingClient/RerankClient  具体供应商实现
    │  (BaiLian / Ollama / AIHubMix / SiliconFlow)
    ▼
外部模型 API（百炼 / Ollama / AIHubMix / SiliconFlow）
```

**关键设计**：业务层只依赖 `LLMService`、`EmbeddingService` 等接口，完全不知道底层用的是哪家供应商。换供应商只改 yaml，不改代码。

---

## 二、整体架构：三件套

模型路由的核心是**三个组件的协作**：

```
┌─────────────┐     候选列表      ┌──────────────────┐
│ ModelSelector│ ──────────────→ │ ModelRoutingExecutor│
└─────────────┘                  └────────┬─────────┘
                                          │ 调用/降级
                                          ▼
                                  ┌──────────────┐
                                  │ModelHealthStore│
                                  │ (熔断器)      │
                                  └──────────────┘
```

| 组件 | 职责 | 类比 |
|------|------|------|
| ModelSelector | 根据配置选出有序的候选模型列表 | 选菜单（按档位/优先级排序） |
| ModelRoutingExecutor | 遍历候选，失败自动切下一个 | 服务员上菜，这道菜没了换一道 |
| ModelHealthStore | 记录每个模型的健康状态，熔断不健康的 | 黑名单，某家店出问题先不选 |

---

## 三、ModelSelector —— 模型选择器

### 3.1 两种选择策略

Ragent 对**对话模型**和**嵌入/重排/视觉模型**采用不同的选择策略：

| 模型组 | 选择策略 | 配置依据 |
|--------|----------|----------|
| chat | **档位机制**（tier） | fast/standard/deep 三档，每档有有序候选列表 |
| embedding | defaultModel + priority | 首选模型置顶，其余按 priority 排序 |
| rerank | defaultModel + priority | 同上 |
| vlm | defaultModel + priority | 同上 |

### 3.2 Chat 档位机制（重点）

```yaml
ai:
  chat:
    default-tier: standard          # 默认档位
    deep-thinking-tier: deep        # 深度思考档位
    tiers:
      fast:                         # 快档：简单问题
        candidates: [qwen-flash, qwen-plus, qwen3-local]
        timeout-ms: 5000
      standard:                     # 标准档：常规问答
        candidates: [qwen3-max, qwen-plus, qwen3-local, gpt-5.4]
        timeout-ms: 120000
      deep:                         # 深档：复杂推理
        candidates: [qwen3-max, deepseekv4pro]
        timeout-ms: 180000
```

**档位选择逻辑**：
```
用户请求
  ├─ 开启深度思考(thinking=true) → deep-thinking-tier (deep)
  ├─ 显式指定档位(override)       → 指定档位
  └─ 默认                          → default-tier (standard)
```

**候选排序逻辑**：
1. 如果有 `preferredModelId`，该模型置顶（必须支持思考）
2. 拼接档位内的候选列表（去重）
3. 过滤：未启用的 → 不支持思考的（如果 requireThinking）→ 不健康的 → 未登记 provider 的

> **面试要点**："Chat 模型用档位机制而不是简单 priority，是因为对话场景对延迟和能力有不同需求——简单问题用 fast 档省钱省时，复杂推理用 deep 档保证质量。每个档位有独立的候选列表和超时预算。"

### 3.3 Embedding 的 defaultModel + priority

```yaml
ai:
  embedding:
    default-model: qwen-emb-8b
    candidates:
      - id: qwen-emb-8b        priority: 1   # 百炼
      - id: qwen-emb-local     priority: 2   # Ollama 本地
      - id: text-embedding-3-large  priority: 3  # AIHubMix
```

排序规则：
1. `defaultModel` 置顶
2. 其余按 `priority` 升序（数值越小越优先）
3. 同 priority 按 id 字母序

### 3.4 过滤条件（通用）

构建最终候选列表时，每个候选必须通过：
- `enabled != false`（未禁用）
- `healthStore.isUnavailable(id) == false`（未熔断）
- provider 配置存在（或为 noop）

---

## 四、ModelHealthStore —— 熔断器（⭐ 面试重点）

### 4.1 三态状态机

经典的熔断器模式，三个状态：

```
        连续失败 >= failureThreshold
CLOSED ──────────────────────────────→ OPEN
  ↑                                      │
  │                                      │ 等待 openDurationMs
  │                                      ▼
  │                                    HALF_OPEN
  │                                      │
  │         探测成功                      │ 探测失败
  └──────────────────────────────────────┘
```

| 状态 | 含义 | 行为 |
|------|------|------|
| CLOSED | 正常 | 允许所有请求通过 |
| OPEN | 熔断 | 拒绝所有请求，直接跳过 |
| HALF_OPEN | 半开 | 只允许**一个**探测请求通过 |

### 4.2 关键方法

| 方法 | 作用 |
|------|------|
| `isUnavailable(id)` | 模型是否不可用（OPEN 且未过期，或 HALF_OPEN 且有探测在飞） |
| `allowCall(id)` | 申请调用许可，返回 CallPermit（null=拒绝） |
| `markSuccess(id)` | 标记成功 → CLOSED，清零失败计数 |
| `markFailure(id)` | 标记失败 → 连续失败+1，达阈值 OPEN；HALF_OPEN 失败直接 OPEN |
| `releaseHalfOpenPermit(permit)` | 释放半开探测名额 |

### 4.3 HALF_OPEN 的单探测设计

HALF_OPEN 状态下，`halfOpenInFlight` 标志保证**同时只有一个探测请求**：

```
HALF_OPEN 状态
  │
  ├─ 第一个请求 → allowCall() 成功，halfOpenInFlight=true，返回 permit
  ├─ 第二个请求 → halfOpenInFlight=true → allowCall() 返回 null（拒绝）
  │
  └─ 探测结果：
       成功 → markSuccess → CLOSED，halfOpenInFlight=false
       失败 → markFailure → OPEN，halfOpenInFlight=false
```

**为什么只允许一个探测？** 避免在模型刚恢复时大量请求涌入再次把它打挂。

### 4.4 配置参数

```yaml
ai:
  selection:
    failure-threshold: 2        # 连续失败 2 次触发熔断
    open-duration-ms: 30000     # 熔断持续 30 秒后进入半开
```

> **面试要点**："熔断器用三态状态机：CLOSED（正常）→ OPEN（熔断）→ HALF_OPEN（半开探测）。连续失败 2 次熔断，30 秒后半开只放一个探测请求，成功则恢复，失败则继续熔断。HALF_OPEN 只允许一个探测是为了避免恢复初期被流量打挂。"

---

## 五、ModelRoutingExecutor —— 路由执行器

### 5.1 故障转移核心逻辑

```java
public <C, T> T executeWithFallback(
        ModelCapability capability,
        List<ModelTarget> targets,           // 有序候选列表
        Function<ModelTarget, C> clientResolver,  // 根据 target 找 client
        ModelCaller<C, T> caller) {          // 实际调用逻辑

    for (ModelTarget target : targets) {
        C client = clientResolver.apply(target);
        if (client == null) continue;                    // client 缺失，跳过
        if (healthStore.allowCall(target.id()) == null)  // 熔断中，跳过
            continue;

        try {
            T response = caller.call(client, target);   // 调用
            healthStore.markSuccess(target.id());        // 成功，标记健康
            return response;                              // 返回结果
        } catch (Exception e) {
            healthStore.markFailure(target.id());        // 失败，标记不健康
            // 继续循环，尝试下一个候选
        }
    }
    throw new RemoteException("All candidates failed");  // 全部失败
}
```

### 5.2 执行流程

```
候选列表 [A, B, C]
  │
  ▼
A: allowCall? → 是 → 调用 → 成功 → markSuccess → 返回 ✓
                    → 失败 → markFailure → 继续
  │
  ▼
B: allowCall? → 是 → 调用 → 成功 → markSuccess → 返回 ✓
                    → 失败 → markFailure → 继续
  │
  ▼
C: allowCall? → 是 → 调用 → 成功 → markSuccess → 返回 ✓
                    → 失败 → markFailure → 抛异常 ✗
```

**关键点**：
- `allowCall` 在执行前检查，熔断中的模型直接跳过
- `markSuccess`/`markFailure` 更新健康状态，影响后续选择
- 全部失败抛 `RemoteException`，由上层处理

---

## 六、RoutingLLMService —— 对话路由服务

### 6.1 同步调用

```java
public String chat(ChatRequest request) {
    return executor.executeWithFallback(
        ModelCapability.CHAT,
        selector.selectChatCandidates(thinking),    // 选候选
        target -> clientsByProvider.get(target.candidate().getProvider()),  // 找 client
        (client, target) -> client.chat(request, target)  // 调用
    );
}
```

直接复用 `executeWithFallback`，简单清晰。

### 6.2 流式调用（⭐ 复杂）

流式调用不能直接复用 `executeWithFallback`，因为流式是**异步回调**模式，需要特殊处理：

```
遍历候选 [A, B, C]
  │
  ▼
A:
  ├─ allowCall? → 否 → 跳过
  ├─ 启动流式请求 client.streamChat()
  ├─ 等待首包（首包探测，超时预算 = 档位 timeoutMs）
  │   ├─ 首包到达 → markSuccess → 返回 handle ✓
  │   ├─ 超时 → markFailure → cancel → 继续下一个
  │   ├─ 错误 → markFailure → cancel → 继续下一个
  │   └─ 无内容 → markFailure → cancel → 继续下一个
  ▼
B: 同上
  ...
  ▼
全部失败 → callback.onError()
```

**首包探测的意义**：流式请求启动后，模型可能长时间不返回第一个 token。用首包超时判断模型是否真的可用，比等整个流结束再判断要快得多。

### 6.3 首包探测 LlmFirstPacketProbe

```java
@Component
public class LlmFirstPacketProbe {
    @RagTraceNode(name = "llm-first-packet", type = "LLM_TTFT")
    public ProbeStreamBridge.ProbeResult awaitFirstPacket(
            ProbeStreamBridge bridge, long timeout, TimeUnit unit) {
        return bridge.awaitFirstPacket(timeout, unit);
    }
}
```

**为什么拆成独立 Bean？** 注释写得很清楚：
> Spring AOP 不拦截类内 self-call，因此 RoutingLLMService 必须依赖外部 bean 调用此方法，@RagTraceNode 才会生效。

这是 Spring AOP 的经典坑——**同类内部方法调用不走代理**。把探测逻辑拆到独立 Bean，通过依赖注入调用，AOP 才能拦截并记录 TTFT（Time To First Token）trace。

> **面试要点**："流式调用的故障转移比同步复杂——需要首包探测。因为流式是异步的，不能等整个流结束才判断成败。我们用档位的 timeoutMs 作为首包超时预算，首包到了就认为模型可用，超时/错误/无内容就降级到下一个候选。另外，首包探测拆成独立 Bean 是因为 Spring AOP 不拦截同类内部调用，独立 Bean 才能让 @RagTraceNode 生效。"

---

## 七、RoutingEmbeddingService —— 嵌入路由服务

### 7.1 两种调用模式

```java
// 模式1：不指定模型，走候选列表降级
public List<Float> embed(String text) {
    return executor.executeWithFallback(
        ModelCapability.EMBEDDING,
        selector.selectEmbeddingCandidates(),   // 全候选
        this::resolveClient,
        (client, target) -> client.embed(text, target)
    );
}

// 模式2：指定模型 ID，只用这一个，不降级
public List<Float> embed(String text, String modelId) {
    return executor.executeWithFallback(
        ModelCapability.EMBEDDING,
        List.of(resolveTarget(modelId)),        // 单元素列表
        this::resolveClient,
        (client, target) -> client.embed(text, target)
    );
}
```

### 7.2 ⚠️ 与你踩过的坑的关联

你之前遇到的"分块时 Ollama 连接失败"问题，根因就在这里：

- **分块流程**：`embed(text, modelId)` 指定了知识库配置的 `qwen-emb-local`（Ollama），**只用这一个模型，不降级**，所以 Ollama 没装就直接报错
- **问答流程**：`embed(text)` 不指定模型，走候选列表，百炼（priority 1）成功

**为什么指定模型不降级？** 因为知识库级配置就是要强制用某个模型，降级到其他模型可能导致向量维度不一致（1024 vs 1536），检索时出问题。

> **面试可以主动提**："Embedding 调用有两种模式——指定 modelId 的不降级（保证维度一致），不指定的走候选列表降级。我们踩过一个坑：知识库配置了 Ollama 但没装，分块时指定 modelId 导致直接失败。修复方案是把知识库的 embedding_model 改为百炼。这个经验让我理解了为什么指定模型不能降级——向量维度必须一致。"

---

## 八、多供应商客户端体系

### 8.1 接口抽象

```
ChatClient (接口)
  ├─ BaiLianChatClient        阿里云百炼
  ├─ OllamaChatClient         本地 Ollama
  ├─ AIHubMixChatClient       AIHubMix 中转
  └─ SiliconFlowChatClient    SiliconFlow

EmbeddingClient (接口)
  ├─ BaiLianEmbeddingClient
  ├─ OllamaEmbeddingClient
  ├─ AIHubMixEmbeddingClient
  └─ SiliconFlowEmbeddingClient

RerankClient (接口)
  ├─ BaiLianRerankClient
  └─ NoopRerankClient         兜底（不重排）

VlmService (接口)
  └─ RoutingVlmService
```

### 8.2 AbstractOpenAIStyleChatClient —— OpenAI 兼容抽象

所有 ChatClient 都继承 `AbstractOpenAIStyleChatClient`，因为百炼、Ollama、AIHubMix、SiliconFlow 都支持 **OpenAI 兼容协议**（`/v1/chat/completions`）。

抽象层统一处理：
- 请求体构建（messages、temperature、stream 等）
- SSE 流式响应解析
- HTTP 客户端（OkHttp）
- 按档位超时派生客户端（缓存 `syncClientByTimeout`）

子类只需实现：
- `provider()` 返回提供商标识
- 特殊的请求头处理（如百炼的 Authorization）

> **面试要点**："所有供应商都用 OpenAI 兼容协议，所以我们有一个 AbstractOpenAIStyleChatClient 抽象基类，统一处理请求构建和 SSE 解析。新增供应商只需继承它，实现 provider() 和特殊头部。这就是面向接口编程的好处——换供应商不改业务代码。"

---

## 九、配置体系（AIModelProperties）

配置结构清晰，四层嵌套：

```
ai
├─ providers                    供应商连接信息
│   ├─ ollama      {url, apiKey, endpoints}
│   ├─ bailian     {url, apiKey, endpoints}
│   ├─ aihubmix    {url, apiKey, endpoints}
│   └─ siliconflow {url, apiKey, endpoints}
│
├─ chat                        对话模型（档位机制）
│   ├─ default-tier
│   ├─ deep-thinking-tier
│   ├─ tiers: {fast, standard, deep}
│   └─ candidates              物理模型注册表
│
├─ embedding                   嵌入模型（priority 机制）
│   ├─ default-model
│   └─ candidates
│
├─ rerank / vlm               重排/视觉（同 embedding）
│
├─ selection                   熔断配置
│   ├─ failure-threshold: 2
│   └─ open-duration-ms: 30000
│
└─ stream                      流式配置
    └─ message-chunk-size: 1
```

---

## 十、设计模式总结

| 设计模式 | 应用位置 | 作用 |
|----------|----------|------|
| 策略模式 | ModelSelector | chat 走档位，embedding 走 priority，两种选择策略 |
| 责任链/循环 | ModelRoutingExecutor | 遍历候选，失败转移 |
| 熔断器 | ModelHealthStore | CLOSED/OPEN/HALF_OPEN 三态 |
| 模板方法 | AbstractOpenAIStyleChatClient | 统一 OpenAI 协议处理，子类填钩子 |
| 函数式接口 | ModelCaller | 调用逻辑以 lambda 传入执行器 |
| 适配器 | ChatClient 各实现 | 适配不同供应商 API |

---

## 十一、面试高频问题（infra-ai 篇）

### Q1：你们的模型熔断降级怎么做的？
> 三层机制：① ModelSelector 过滤掉熔断中的模型；② ModelHealthStore 实现三态熔断器（CLOSED→OPEN→HALF_OPEN），连续失败 2 次熔断 30 秒，半开只放一个探测；③ ModelRoutingExecutor 遍历候选，失败自动切下一个。全部失败抛 RemoteException。

### Q2：Chat 模型为什么用档位而不是 priority？
> 对话场景对延迟和能力有不同需求。fast 档（qwen-flash）响应快但能力弱，适合简单问题；standard 档（qwen3-max）均衡；deep 档（qwen3-max+deepseek）能力强但慢，适合复杂推理。每个档位有独立的候选列表和超时预算。

### Q3：流式调用的故障转移和同步有什么不同？
> 同步直接用 executeWithFallback，失败就切。流式需要首包探测——因为流式是异步的，不能等流结束才判断。用档位 timeoutMs 作为首包超时，首包到了就认为可用，超时/错误就降级。另外首包探测拆成独立 Bean，是因为 Spring AOP 不拦截同类内部调用。

### Q4：为什么指定 modelId 的 Embedding 调用不降级？
> 因为不同 Embedding 模型输出维度可能不同（如百炼 1024 维 vs text-embedding-3-large 1536 维）。降级到其他模型会导致同一知识库向量维度不一致，检索时出问题。所以指定模型就是强制用，失败就报错。

### Q5：HALF_OPEN 为什么只允许一个探测请求？
> 避免模型刚恢复时大量请求涌入再次把它打挂。半开状态只放一个探测，成功就恢复（CLOSED），失败就继续熔断（OPEN）。这是熔断器模式的标准做法。

### Q6：新增一个模型供应商需要改哪些代码？
> 1. yaml 加 provider 配置和 candidate；2. 实现 ChatClient/EmbeddingClient 接口（继承 AbstractOpenAIStyleChatClient）；3. 注册为 Spring Bean。业务代码不用改——这就是面向接口编程的好处。

---

## 十二、本篇小结

infra-ai 模块的核心价值是**模型高可用**：

1. **ModelSelector**：两种选择策略——chat 用档位（按能力/延迟分级），embedding/rerank/vlm 用 priority
2. **ModelHealthStore**：三态熔断器，连续失败 2 次熔断 30s，半开单探测
3. **ModelRoutingExecutor**：遍历候选 + 故障转移，成功标记健康，失败标记不健康
4. **RoutingLLMService**：同步直接用执行器，流式加首包探测
5. **RoutingEmbeddingService**：指定模型不降级（维度一致），不指定走降级
6. **多供应商抽象**：OpenAI 兼容协议统一，新增供应商只改 yaml + 实现接口

下一篇预告：**学习4 —— 知识库入库链路详解**（上传→存储→解析→切分→嵌入→落库，信号量/调度/MQ）。
