# Ragent 项目学习（六）：ReAct Agent 执行框架详解

> 系列：ragent项目学习6
> 本篇目标：搞懂 ReAct Agent 的完整执行框架——Agent 构建、工具体系、记忆管线、确认机制、MCP 桥接
> 对应源码：`d:\javaCODE\Practical trainning two\ragent\agent`

---

## 一、Agent 模式 vs Workflow 模式

Ragent 通过 `ragent.engine.type` 配置切换两种引擎：

| 对比 | Workflow (v1) | Agent (v2) |
|------|---------------|------------|
| 架构 | 固定管道：改写→意图→检索→融合→重排→生成 | ReAct 循环：思考→调工具→观察→再思考 |
| RAG 定位 | 就是整个应用 | **降级为 Agent 的一个 Tool** |
| 灵活性 | 流程确定，可控可测 | 模型自主决定调用哪些工具、调用几次 |
| 入口 | RAGChatServiceImpl | AgentChatServiceImpl |
| 成本 | 低（一次 LLM 调用为主） | 高（多轮 LLM + 工具调用） |
| 框架 | 自实现 StreamChatPipeline | AgentScope 2.0.2 |

> **面试要点**："Agentic RAG 的本质不是替代 RAG，而是把 RAG 从『流程本身』变成『Agent 的工具之一』，由模型决定何时检索、检索什么、是否需要再检索。"

---

## 二、ReActAgentProvider —— Agent 构建器

### 2.1 单例复用 + 懒重建

```java
public ActiveAgent getAgent() {
    String persona = resolvePersona();          // 人设
    ResolvedCatalog catalog = toolCatalog.resolve();  // 工具目录
    if (matches(cached, persona, catalog)) {
        return new ActiveAgent(cached.agent(), catalog);  // 复用
    }
    synchronized (this) {
        // 双检锁：构建新 Agent
        ReActAgent agent = buildAgent(persona, catalog);
        cached = new CachedAgent(persona, catalog, agent);
        return new ActiveAgent(agent, catalog);
    }
}
```

**重建条件**：人设内容变化 或 工具目录指纹变化（控制台修改后无需重启，下一次会话生效）。

**为什么不主动 close 旧实例？** 在途会话仍在旧实例上流式输出，交给 GC 回收。

### 2.2 Agent 构建参数

```java
ReActAgent.builder()
    .name("ragent")
    .sysPrompt(persona)                    // 人设（来自 AGENT_MAIN 提示词槽位）
    .model(agentChatModel)                  // 单模型，无 fallback
    .toolkit(toolCatalog.buildToolkit(catalog))
    .maxIters(10)                           // ReAct 循环上限
    .maxRetries(2)                          // 单次模型调用失败重试
    .stateStore(agentStateStore)            // PostgreSQL 持久化会话状态
    .middleware(userMemoryMiddleware)        // 记忆注入
    .middleware(contextCompactionMiddleware) // 上下文压缩
    .middleware(confirmDenialMiddleware)     // 确认拒绝口径修正
    .middleware(skillMaskingMiddleware)      // 技能遮蔽
    .build();
```

**关键配置**：
- `maxIters=10`：ReAct 循环最多 10 轮，防止模型死循环
- `sseTimeoutMs=900000`（15 分钟）：一次 Agent 运行最多 10 轮，每轮量级接近 RAG 单问全程

> **面试要点**："Agent 用单例复用，人设或工具目录变化时懒重建。构建时配置 maxIters=10 防止死循环，stateStore 用 PostgreSQL 持久化会话状态，重建不丢历史。"

---

## 三、工具体系

### 3.1 工具目录（AgentToolCatalog）

Agent 的工具箱由四类工具组成：

| 工具 | 类 | 来源 | 说明 |
|------|-----|------|------|
| search_knowledge | KnowledgeSearchTool | 固定注册 | RAG 检索入口，只读 |
| flush_memory | MemoryFlushTool | 可选（长期记忆开启时） | 触发记忆抽取 |
| load_skill | SkillLoadTool | 可选（有技能时） | 加载技能手册 |
| MCP 工具 | McpToolBridge | 按意图树配置挂载 | 远程工具调用 |

### 3.2 KnowledgeSearchTool —— RAG 作为工具

```java
public class KnowledgeSearchTool implements AgentTool {
    public static final String TOOL_NAME = "search_knowledge";
    
    // 参数 Schema：只有一个 query 字段
    public Map<String, Object> getParameters() {
        return Map.of("type", "object",
            "properties", Map.of("query", Map.of("type", "string")),
            "required", List.of("query"));
    }
    
    public boolean isReadOnly() { return true; }  // 只读，不需要确认
    
    public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
        String query = param.getInput().get("query");
        String result = knowledgeSearchFacade.search(query, recentTurns);
        return buildResult(toolCallId, result, false);
    }
}
```

**设计要点**：
- **RAG 降级为工具**：Agent 自主决定是否检索，而不是每次都检索
- **只读属性**：`isReadOnly=true`，不需要用户确认
- **近期轮次改写**：取最近 2 轮对话做查询改写，消解指代（"它""那个"）

> **面试要点**："在 Agent 模式下，RAG 管线降级为一个 search_knowledge 工具。模型自主决定何时检索、用什么 query 检索。工具调用时会带最近 2 轮对话做查询改写，消解指代。"

### 3.3 McpToolBridge —— MCP 工具桥接

将 MCP 远程工具适配为 AgentScope 工具：

```
MCP Server 定义工具 → McpToolRegistry 注册 → McpToolBridge 适配 → Agent Toolkit
```

**核心能力**：

1. **权限检查（checkPermissions）**：
   - 意图树配置了 `requireConfirm` → 返回 `ask`（需要用户确认）
   - MCP 声明 `readOnlyHint=false` → 返回 `ask`
   - 否则返回 `allow`

2. **技能遮蔽（maskedBySkill）**：
   - 属于某个技能但手册未加载 → 拒绝调用，提示先 `load_skill`
   - 防止模型照着历史里的旧调用硬闯

3. **Schema 转换**：把 MCP 的 JsonSchema 转成 AgentScope 的参数定义

> **面试要点**："MCP 工具通过 McpToolBridge 适配成 Agent 工具。权限检查看两个条件——意图树的 requireConfirm 和 MCP 的 readOnlyHint。技能遮蔽确保工具必须先加载手册才能调用。"

---

## 四、ReAct 循环执行流程

```
用户提问
    │
    ▼
AgentChatServiceImpl.streamChat()
    │
    ├─ ① 检查是否有待确认的工具调用（有则拒绝新问题）
    ├─ ② guardedStart：获取并发锁
    └─ ③ startRun：启动 ReAct 循环
         │
         ▼
    ┌──────────────────────────────────┐
    │  ReAct 循环（maxIters=10）        │
    │                                  │
    │  Thought：模型思考下一步做什么    │
    │      │                           │
    │      ▼                           │
    │  Action：调用工具                 │
    │      │                           │
    │      ├─ 只读工具 → 直接执行       │
    │      └─ 危险工具 → ask 用户确认   │
    │           │                      │
    │           ▼                      │
    │      Observation：获取工具结果    │
    │           │                      │
    │           └─ 回到 Thought         │
    │                                  │
    │  直到模型给出 Final Answer        │
    └──────────────────────────────────┘
         │
         ▼
    SSE 推送结果给前端
```

### 4.1 并发锁（AgentRunGate）

```java
Runnable releaseGate = runGate.acquire(userId, taskId, conversationId);
```

- 同一用户同一时间只能有一个 Agent 运行
- 防止用户同时发多个问题导致状态混乱
- 启动失败立即释放锁，防止用户被锁半小时

### 4.2 待确认检查

```java
if (conversationService.hasPendingConfirm(conversationId, userId)) {
    throw new ClientException("上一步操作还在等你确认，请先确认或取消");
}
```

有待确认的工具调用时不接受新问题，否则框架会报英文技术异常。

---

## 五、记忆体系（三层记忆）

### 5.1 记忆分层

```
┌─────────────────────────────────────────┐
│ 短期记忆：当前会话上下文窗口              │
│   ├─ 最近 N 轮原文                       │
│   └─ 早期对话摘要（压缩门触发）           │
├─────────────────────────────────────────┤
│ 长期记忆：跨会话的用户偏好/事实            │
│   ├─ Judge 仲裁：哪些值得记               │
│   ├─ Consolidation 合并：去重/归纳       │
│   └─ Commit 提交：持久化到 DB             │
├─────────────────────────────────────────┤
│ 工具记忆：最近工具调用结果（可裁剪）       │
└─────────────────────────────────────────┘
```

### 5.2 上下文窗口两道门

基于 `contextWindowChars=1,200,000` 派生：

| 门 | 触发比例 | 动作 |
|----|----------|------|
| 裁剪门 | 50% | 把老工具结果换成占位（保留 search_knowledge 的可裁剪） |
| 压缩门 | 80% | 早期对话做摘要，保留最近 20% + 2 个已完成工具循环 |

**为什么两道门？**
- 裁剪门轻量（不叫模型），先顶住
- 压缩门重量（叫模型做摘要），裁剪顶不住才动手
- 留 20% 给压缩期间的模型调用

### 5.3 长期记忆抽取管线（AgentMemoryPipeline）

```
触发（后台≥3条 / flush工具）
    │
    ▼
① 读快照：loadPending（待处理消息）
    │
    ▼
② 仲裁（AgentMemoryJudge，事务外）：
    对每条消息判断：新增 / 更新 / 删除 / 忽略
    │
    ▼
③ 合并计划（Consolidation）：
    容量顶到上限 → 合并相似条目
    │
    ▼
④ 提交（Commit，短事务双校验）：
    写记忆表 + 更新水位线 + revision 校验
```

**关键设计**：
- **仲裁在事务外**：慢调用（LLM）不占着控制面行锁
- **短事务双校验**：提交时再校验 revision，防并发冲突
- **容量上限**：`0.005 × 预算`，夹在 [1500, 6000] 字符

### 5.4 记忆中间件链（顺序很重要）

```java
.middleware(userMemoryMiddleware)         // 外层：注入长期记忆
.middleware(contextCompactionMiddleware)  // 中间：上下文压缩
.middleware(confirmDenialMiddleware)      // 内层：确认拒绝口径修正
.middleware(skillMaskingMiddleware)       // 最内层：技能遮蔽
```

**为什么这个顺序？** 注释解释得很清楚：
- 记忆块插在人设与会话之间，压缩中间件的 offset 比对自然吸收这一条
- 确认拒绝排在压缩之后：被压进摘要的拒绝结果已经不在列表里，改写自然跳过
- 技能遮蔽最内层：手册被压缩带走后遮蔽跟着复位，工具与手册同进同出

> **面试要点**："记忆分三层——短期上下文、长期用户记忆、工具结果。上下文窗口有两道门：50% 触发轻量裁剪（老工具结果换占位），80% 触发重量压缩（LLM 摘要）。长期记忆用 Judge→Consolidation→Commit 管线，仲裁在事务外不占锁，提交用短事务双校验防并发。"

---

## 六、确认机制

### 6.1 危险工具确认流程

```
模型调用危险工具（requireConfirm=true 或 readOnlyHint=false）
    │
    ▼
McpToolBridge.checkPermissions() → 返回 ask
    │
    ▼
框架暂停 Agent 循环，SSE 推送确认卡片给前端
    │
    ▼
用户点击 确认 / 取消
    │
    ├─ 确认 → confirmPendingTool(conversationId, messageId, approved=true)
    │           → Agent 继续执行工具
    │
    └─ 取消 → confirmPendingTool(conversationId, messageId, approved=false)
                → 框架返回 "Permission denied by user"
                → AgentConfirmDenialMiddleware 改写口径
```

### 6.2 AgentConfirmDenialMiddleware —— 拒绝口径修正

框架默认返回 `"Permission denied by user"`，模型可能误解为权限不足。中间件改写成：

```
用户在确认卡片上点了取消，这次操作没有执行。这不是权限不足，也不是系统故障。
请如实告诉用户操作已取消；等用户确认后再重新发起。
```

**只改写用户取消的拒绝文案，规则拒绝不动。**

> **面试要点**："危险工具调用需要用户确认。用户取消时框架默认返回 Permission denied，模型可能误解。我们用中间件改写成中文说明，告诉模型这是用户主动取消，不是权限问题。"

---

## 七、状态持久化

### 7.1 PgAgentStateStore

会话状态存在 PostgreSQL，不是内存：
- 重建 Agent 不丢历史
- 多节点部署时可共享状态
- 崩溃恢复后可继续会话

### 7.2 evictStateCache

```java
public void evictStateCache(String userId, String sessionId) {
    CachedAgent current = cached;
    if (current != null) {
        current.agent().clearStateCache(userId, sessionId);
    }
}
```

- 只作用于已构建的实例
- 清理动作不该顺手把 Agent 建起来
- 多节点各持一份缓存，需要时经 Redis 广播补齐

---

## 八、设计模式总结

| 设计模式 | 应用位置 | 作用 |
|----------|----------|------|
| 单例 + 懒重建 | ReActAgentProvider | Agent 复用，人设/工具变化时重建 |
| 适配器 | McpToolBridge | MCP 工具 → AgentScope 工具 |
| 策略 | KnowledgeSearchTool 等 | AgentTool 接口，多种工具可互换 |
| 中间件/责任链 | 4 个 Middleware | 记忆注入→压缩→确认→遮蔽 |
| 管道 | AgentMemoryPipeline | Judge→Consolidation→Commit |
| 双检锁 | getAgent() | 并发安全的懒重建 |

---

## 九、面试高频问题（Agent 篇）

### Q1：Agent 模式和传统 RAG 有什么区别？
> 传统 RAG 是固定管道，每次都检索→生成。Agent 模式把 RAG 降级为一个工具，由模型自主决定何时检索、检索什么、调用几次。灵活性更高，但成本也更高（多轮 LLM + 工具调用）。Ragent 通过 engine.type 一键切换。

### Q2：ReAct 循环是什么？怎么防止死循环？
> ReAct = Reasoning + Acting，循环是"思考→调工具→观察→再思考"直到给出最终答案。防止死循环靠 maxIters=10，超出后框架熔断收尾。

### Q3：你们的工具有哪些？RAG 怎么变成工具的？
> 四类工具：① search_knowledge（RAG 检索，固定注册，只读）；② flush_memory（记忆抽取）；③ load_skill（技能手册）；④ MCP 工具（按意图树挂载）。RAG 变成工具就是把检索逻辑封装成 AgentTool 接口，模型通过 function call 调用。

### Q4：危险工具怎么防止误操作？
> 两层防护：① 权限检查——McpToolBridge.checkPermissions 返回 ask 触发确认；② 用户确认卡片——框架暂停循环，等用户点确认/取消。取消时中间件改写口径，避免模型误解为权限不足。

### Q5：上下文窗口爆了怎么办？
> 两道门：① 50% 触发裁剪——老工具结果换占位；② 80% 触发压缩——LLM 摘要早期对话，保留最近 20% + 2 个已完成循环。裁剪轻量不叫模型，压缩重量但留余量。

### Q6：长期记忆怎么抽取的？
> 管线四步：① 读待处理消息（后台≥3条或flush触发）；② Judge 仲裁（新增/更新/删除/忽略，事务外不占锁）；③ Consolidation 合并（容量上限时）；④ Commit 提交（短事务双校验 revision）。容量上限是 0.005×上下文预算。

---

## 十、本篇小结

ReAct Agent 框架的核心价值是**自主决策 + 安全可控**：

1. **ReAct 循环**：思考→调工具→观察，maxIters=10 防死循环
2. **RAG 降级为工具**：模型自主决定何时检索，而不是固定流程
3. **工具体系**：search_knowledge + MCP 工具 + 记忆工具 + 技能工具
4. **确认机制**：危险工具需用户确认，取消时口径修正
5. **三层记忆**：短期上下文（两道门）+ 长期记忆（Judge→Commit 管线）+ 工具结果
6. **状态持久化**：PgAgentStateStore，重建不丢历史

下一篇预告：**学习7 —— 流量治理与可观测详解**（Redis 公平排队、分布式信号量、幂等控制、全链路 Trace、效果评估）。
