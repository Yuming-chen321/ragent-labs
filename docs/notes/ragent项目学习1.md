# Ragent 项目学习（一）：项目全景与架构总览

> 系列：ragent项目学习1 / 学习2 / 学习3 ...
> 本篇目标：搞懂项目定位、模块分层、双引擎架构、两条核心链路的全貌
> 对应源码：`d:\javaCODE\Practical trainning two\ragent`

---

## 一、项目定位

Ragent 是一个**面向 Agentic RAG 演进的生产级 Java AI 应用平台**，覆盖"文档入库 → 知识管理 → 混合检索 → 智能问答 → 溯源反馈"的完整闭环。

一句话理解：**它把 RAG 管线做成了可编排、可降级、可观测的工程系统，并且能从"传统 RAG 管道"平滑升级为"ReAct Agent 架构"**。

核心能力清单（来自 README + 配置验证）：

| 能力 | 说明 | 配置证据 |
|------|------|----------|
| 混合检索 | 向量、关键词、知识图谱、联网搜索四路并行召回 | `rag.search.channels.*` |
| 融合重排 | RRF 融合 + Rerank 精排 + 证据闸门 | `rag.search.fusion.*` / `rag.search.evidence.*` |
| 问题理解 | 查询词映射、问题重写/拆分、树形意图识别、多知识库路由 | `rag.query-rewrite` / IntentNode |
| 模型路由 | 多供应商、多候选、档位（fast/standard/deep）、熔断降级 | `ai.chat.tiers.*` / `ai.selection.*` |
| 双引擎 | workflow（v1 管线）/ agent（v2 ReAct）一键切换 | `ragent.engine.type` |
| 会话记忆 | 最近 N 轮 + 持久化摘要 + Agent 长期记忆 | `rag.memory.*` / `agent.memory.*` |
| 流量保护 | Redis 公平排队、分布式并发信号量、幂等 | `rag.rate-limit.*` / `rag.semaphore.*` |
| 可观测 | 全链路 Trace（run + node 两级）、管理后台可视化 | `rag.trace.enabled` |
| MCP | 工具发现、Schema 校验、远程工具调用 | `rag.mcp.servers` / mcp-server 模块 |

---

## 二、技术栈全景

| 分类 | 技术 | 版本/说明 |
|------|------|-----------|
| 语言 | Java | 17（pom 定义）/ 运行环境 21 |
| 框架 | Spring Boot | 4.1.0（注意：大版本，自动装配机制有变） |
| ORM | MyBatis-Plus | 3.5.17（spring-boot4-starter） |
| 数据库 | PostgreSQL + pgvector | 向量存储，`vector` 类型 + HNSW |
| 向量库备选 | Milvus | 2.6.6 SDK，`rag.vector.type` 可切换 pg/milvus |
| 缓存 | Redis + Redisson | 4.6.1，限流/分布式锁/信号量 |
| 消息 | RocketMQ | 5.x + starter 2.3.6（有兼容性坑） |
| 对象存储 | S3 协议（rustfs/MinIO）/ 阿里云 OSS | 文档原文、多模态资产 |
| 文档解析 | MinerU（PDF/Word/PPT）+ Apache Tika 3.2.3（txt/md） | SaaS API 轮询 |
| 大模型 | 阿里云百炼 DashScope | qwen3-max / text-embedding-v4 / qwen3-rerank / qwen-vl-max |
| 备选供应商 | Ollama、AIHubMix、SiliconFlow | OpenAI 兼容端点 |
| Agent 框架 | AgentScope | 2.0.2（agentscope-core） |
| MCP | MCP Java SDK | 1.1.2 |
| 认证 | Sa-Token | 1.45.0 |
| 前端 | React + TypeScript + Vite + Tailwind + shadcn/ui | 独立 frontend 目录 |
| 部署 | Docker Compose | PostgreSQL/Redis/RocketMQ/MinIO |

---

## 三、整体架构分层

```
┌─────────────────────────────────────────────────────────┐
│              frontend (React + TS)                       │
│   聊天页 / Agent页 / 管理后台(知识库/检索Trace/仪表盘)      │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP / SSE
┌────────────────────────▼────────────────────────────────┐
│         bootstrap (启动模块 :9090 /api/ragent)            │
├─────────────────────────────────────────────────────────┤
│  agent 模块                    │  rag 模块                │
│  ReAct循环/工具/记忆/确认       │  knowledge(入库管理)      │
│  KnowledgeSearchTool          │  core/retrieval(多路检索) │
│  McpToolBridge                │  core/vector(pg/milvus)  │
├───────────────────────────────┼──────────────────────────┤
│  system 模块 (用户/权限)        │  rag.service(会话/流式管道)│
├───────────────────────────────┴──────────────────────────┤
│  infra-ai 模块：模型接入层                                 │
│  ChatClient / EmbeddingClient / RerankClient / Vlm       │
│  ModelSelector + ModelRoutingExecutor + ModelHealthStore │
├──────────────────────────────────────────────────────────┤
│  framework 模块：通用底座                                  │
│  web(SSE/异常) / MQ(事务消息) / 幂等 / Trace / 雪花ID /上下文│
├──────────────────────────────────────────────────────────┤
│  中间件：PostgreSQL(pgvector) / Redis / RocketMQ / MinIO   │
│  外部：百炼 / MinerU / LightRAG / MCP Server(:9099)        │
└──────────────────────────────────────────────────────────┘
```

**依赖方向**：bootstrap → agent/rag/system → infra-ai → framework（严格单向，经典"洋葱架构"）。

---

## 四、七大 Maven 模块职责

### 1. framework —— 通用技术底座

| 包 | 关键类 | 作用 |
|----|--------|------|
| web | SseEmitterSender、StreamTaskManager、GlobalExceptionHandler | SSE 推送、流式任务生命周期 |
| mq | RocketMQProducerAdapter、DelegatingTransactionListener | 事务消息 HALF/COMMIT 封装 |
| idempotent | @IdempotentSubmit / @IdempotentConsume + AOP | 接口幂等、消费幂等（SpEL key） |
| trace | RagTraceContext、RagTraceNode | Trace 上下文与节点注解 |
| distributedid | CustomIdentifierGenerator（雪花+Lua 初始化） | 分布式 ID |
| convention | Result、ChatRequest、RetrievedChunk、GroundingChunk | 全局约定模型 |
| context | UserContext、LoginUser | 用户上下文（TTL 传递） |

### 2. infra-ai —— 模型接入与路由（面试重点）

| 包 | 关键类 | 作用 |
|----|--------|------|
| model | **ModelSelector / ModelRoutingExecutor / ModelHealthStore** | 候选选择、路由执行、健康状态（熔断计数） |
| chat | RoutingLLMService + BaiLian/Ollama/AIHubMix/SiliconFlow Client | 对话补全，OpenAI 兼容协议抽象（AbstractOpenAIStyleChatClient） |
| embedding | RoutingEmbeddingService + 4 个 Client | 向量化，priority 优先级 fallback |
| rerank | BaiLianRerankClient + NoopRerankClient | 精排（noop 兜底） |
| vlm | RoutingVlmService | 图片理解（PDF 抽图转写） |
| chat 探测 | LlmFirstPacketProbe | 首包探测（TTFT 优化） |

### 3. rag —— RAG 核心域（最大的模块）

| 包 | 关键类 | 作用 |
|----|--------|------|
| knowledge | KnowledgeBase/Document/ChunkService、MQ 消费者、schedule | 知识库管理 + 入库链路 |
| core.retrieval | **MultiChannelRetrievalEngine**、SearchChannel、RetrievalScopeResolver | 多通道并行检索 + 作用域路由 |
| core.retrieval.postprocessor | Fusion（RRF）、Deduplication、Rerank、EvidenceGate、ChannelAttribution | 后处理器责任链 |
| core.vector | PgVectorStoreService / MilvusVectorStoreService + 装饰器 | 向量读写，双实现可切换 |
| core.rewrite | QueryRewriteService、MultiQuestionRewriteService | 查询改写/拆分 |
| core.intent | IntentNode 树 | 树形意图识别 → 知识库路由 |
| core.source | CitationMarkup、SourcesAssembler | 行内引用 [N] 与来源面板 |
| service.pipeline | StreamChatPipeline、StreamChatEventHandler | v1 流式问答编排 |
| ratelimit | FairDistributedRateLimiter、ChatQueueLimiter | Redis 公平排队限流 |
| trace/eval | RagTraceRecordService、EvalController | 全链路 Trace、效果评估 |

### 4. agent —— ReAct Agent 域（v2 引擎）

| 包 | 关键类 | 作用 |
|----|--------|------|
| config | ReActAgentProvider、AgentEngineConfiguration | 基于 AgentScope 构建 ReAct 循环 |
| tool | KnowledgeSearchTool、McpToolBridge、MemoryFlushTool、AgentToolCatalog | RAG 降级为 Tool、MCP 桥接 |
| memory | AgentMemoryPipeline（Judge/Extract/Merge/Consolidate/Compaction） | 长期记忆提炼与上下文压缩 |
| confirm | AgentConfirmDenialMiddleware | 危险工具调用人工确认 |
| state | PgAgentStateStore | Agent 状态持久化 |

### 5. system —— 用户/权限等系统能力
### 6. bootstrap —— 唯一启动入口 + application.yaml 全部配置
### 7. mcp-server —— 独立 MCP 服务端（:9099），提供远程工具

---

## 五、双引擎架构（本项目最大亮点）⭐

```yaml
ragent:
  engine:
    type: agent  # workflow（v1 编排管线）/ agent（v2 ReAct 架构）
```

| 对比 | workflow (v1) | agent (v2) |
|------|---------------|------------|
| 架构 | 固定管道：改写→意图→检索→融合→重排→生成 | ReAct 循环：思考→调工具→观察→再思考 |
| RAG 定位 | 就是整个应用 | **降级为 Agent 的一个 Tool**（KnowledgeSearchTool） |
| 灵活性 | 流程确定，可控可测 | 模型自主决策调用次数与顺序 |
| 入口 | RAGChatServiceImpl → StreamChatPipeline | AgentChatServiceImpl → ReActAgentProvider |
| 成本 | 低（一次 LLM 调用为主） | 高（多轮 LLM + 工具调用，max-iters=10 兜底） |

**面试话术**："Agentic RAG 的本质不是替代 RAG，而是把 RAG 从『流程本身』变成『Agent 的工具之一』，由模型决定何时检索、检索什么、是否需要再检索。Ragent 通过 engine.type 一键切换两种架构，且共享同一套 infra-ai 模型路由层。"

---

## 六、两条核心链路总览

### 链路 A：知识入库（Ingestion Pipeline）

```
管理台上传文档
  → S3/rustfs 存储原文（kb-bucket: ragent-sources，按 collectionName 隔离）
  → [事务消息/异步] KnowledgeDocumentChunkConsumer
  → 解析：MinerU(PDF/Word/PPT, 轮询SaaS) / Tika(txt/md) / VLM(图片转写)
  → 切分 Chunk（分块策略）
  → Embedding：RoutingEmbeddingService（百炼 text-embedding-v4 优先，Ollama 兜底）
  → VectorChunkSink → pgvector t_knowledge_vector（HNSW 索引）
  → 装饰器同步：KeywordSyncingVectorStoreService(ES) / GraphSyncingVectorStoreService(LightRAG)
  → 文档状态 pending→running→success，chunk_log 记录过程
```

并发保护：上传信号量（10 并发）、MinerU 解析信号量（5 并发）。

### 链路 B：智能问答（Chat Pipeline，v1 workflow 视角）

```
用户提问 (SSE)
  → ① 查询理解：QueryTermMapping 术语映射 → QueryRewriteService 改写/拆分子问题
  → ② 意图路由：IntentNode 树打分 → RetrievalScopeResolver 决定检索哪些知识库
      （confidence-threshold=0.6，低于则退化为全库检索）
  → ③ 多路检索：MultiChannelRetrievalEngine
      向量(pgvector) ∥ 关键词(ES) ∥ 图谱(LightRAG) ∥ 联网搜索
      每通道 recall-budget=20 条，超时 15s，CompletableFuture 并行
  → ④ 后处理链（SearchResultPostProcessor 责任链）：
      RRF融合(rrf-k=20) → 去重 → Rerank精排(候选池40→top10) → 证据闸门(min-rerank-score=0.2)
  → ⑤ 记忆组装：最近 8 轮 + 第 9 轮起持久化摘要(400字)
  → ⑥ Prompt 组装（.st 模板 + 引用规则注入）
  → ⑦ RoutingLLMService 流式生成（档位路由 + 首包探测 + 熔断）
  → ⑧ SSE 推送：delta/meta/sources/完成事件
  → ⑨ 异步收尾：会话标题生成、推荐问题、反馈入库(MQ)
```

**检索漏斗三段预算**（重点记忆）：

```
每通道召回 20 条 → RRF 融合 → 候选池截断 40 条 → Rerank → 最终 top-k 10 条进 LLM
(recall-budget)      (fusion)    (rerank-candidate-limit)         (default-top-k)
```

---

## 七、application.yaml 关键配置速查

```yaml
# 引擎档位
ragent.engine.type: agent | workflow

# 模型体系：4 provider（ollama/bailian/aihubmix/siliconflow）
ai.chat.tiers:            # 对话三档位
  fast:     [qwen-flash, qwen-plus, qwen3-local]        timeout 5s
  standard: [qwen3-max, qwen-plus, qwen3-local, gpt-5.4] timeout 120s
  deep:     [qwen3-max, deepseekv4pro]                  timeout 180s
ai.embedding.candidates:  # priority 1/2/3 → bailian → ollama → aihubmix
ai.selection:             # 熔断：failure-threshold=2, open-duration-ms=30000

# 检索
rag.search.default-top-k: 10 / recall-budget: 20 / rerank-candidate-limit: 40
rag.search.fusion.rrf-k: 20   # 注意：不是经典60，候选池小，60会过度平滑
rag.search.evidence.min-rerank-score: 0.2  # 整批最高分低于此值全弃→"知识库没答案"

# 基础设施
rag.vector.type: pg | milvus
rag.keyword.type: none | es
rag.graph.type: none | lightrag
rag.storage.type: s3 | oss
rag.default.dimension: 1536   # ⚠️ 注意：当前是1536，与之前排查文档中 text-embedding-v4=1024 的结论不一致，面试前务必确认实际库表维度！
```

> ⚠️ **重要发现（学习笔记）**：当前 `rag.default.dimension=1536`，而之前的排查文档结论是"百炼 text-embedding-v4 输出 1024 维"。两者矛盾——要么后来换了 embedding 模型，要么 yaml 与表结构仍存在漂移。**面试前必须亲自验证一次**（查表 `\d t_knowledge_vector` + 查配置），否则被追问会翻车。

---

## 八、模块设计思想（面试升华用）

1. **洋葱架构 + 依赖倒置**：framework 不依赖任何业务模块；rag/agent 依赖 infra-ai 抽象（EmbeddingService 接口），不依赖具体厂商 Client——换供应商只改 yaml。
2. **策略模式遍地**：SearchChannel（四路检索）、SearchResultPostProcessor（后处理链）、EmbeddingClient（多厂商）、VectorStoreService（pg/milvus）——全部面向接口 + Spring 注入 List。
3. **装饰器模式**：KeywordSyncing/GraphSyncingVectorStoreService 在向量写入时"顺手"同步 ES 和图谱，业务无感知。
4. **责任链 + 预算控制**：检索后处理器按序执行，三段预算单调收窄，启动时校验（RetrievalChannelConfigValidator fail-fast）。
5. **注解驱动横切**：@RagTraceNode（链路追踪）、@IdempotentSubmit/Consume（幂等）——业务零侵入。

---

## 九、系列学习路线图

| 篇 | 主题 | 核心内容 |
|----|------|----------|
| **1（本篇）** | 全景与架构 | 模块分层、双引擎、两条链路总览 |
| 2 | framework 基座 | SSE、事务消息封装、幂等 AOP、雪花 ID、Trace 上下文 |
| 3 | infra-ai 模型路由 | ModelSelector/Executor/HealthStore、档位、熔断、首包探测 |
| 4 | 知识库入库链路 | 上传→解析→切分→嵌入→落库，MQ/信号量/调度 |
| 5 | 多路检索引擎 | 四通道、作用域路由、RRF、Rerank、证据闸门 |
| 6 | ReAct Agent | 工具体系、记忆管线、确认机制、MCP 桥接 |
| 7 | 流量治理与可观测 | 公平排队、信号量、幂等、Trace、评估 |
| 8 | 面试终极整理 | 高频问题 + 标准答案 + 追问应对 |

---

## 十、本篇小结（面试开场白模板）

> "Ragent 是一个生产级 Agentic RAG 平台，采用洋葱分层的多模块架构：framework 提供技术底座，infra-ai 统一接入 4 家模型供应商并实现档位路由和熔断降级，rag 模块承载知识库管理和四路混合检索，agent 模块实现 ReAct 执行框架。它最大的特点是**双引擎设计**——通过 engine.type 配置可以在传统 RAG 管线和 ReAct Agent 架构间切换，Agent 模式下 RAG 管线降级为一个可复用工具。入库和问答两条核心链路都具备完善的流量保护（Redis 公平排队、信号量、幂等）和全链路 Trace 可观测能力。"

---

**下一篇预告**：**学习2 —— framework 基座详解**（SSE 推送机制、RocketMQ 事务消息封装、幂等注解 AOP 实现原理）。
