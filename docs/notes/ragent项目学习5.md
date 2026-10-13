# Ragent 项目学习（五）：多路检索引擎详解

> 系列：ragent项目学习5
> 本篇目标：搞懂多路检索引擎的完整链路——四通道并行、作用域路由、RRF 融合、Rerank 精排、证据闸门、三段预算
> 对应源码：`d:\javaCODE\Practical trainning two\ragent\rag\src\main\java\com\nageoffer\ai\ragent\rag\core\retrieval`

---

## 一、检索链路全景

```
用户问题（子问题）
    │
    ▼
┌─────────────────────────────────────────────┐
│ 1. 作用域解析 RetrievalScopeResolver          │
│    ├─ 意图最高分 >= 0.6 → 定向检索（命中库）  │
│    └─ 意图最高分 < 0.6  → 全局检索（所有库）  │
└─────────────────┬───────────────────────────┘
                  ▼
┌─────────────────────────────────────────────┐
│ 2. 多通道并行检索 MultiChannelRetrievalEngine │
│    向量(pgvector) ∥ 关键词(ES) ∥ 图谱 ∥ 联网  │
│    每通道 recall-budget=20，超时 15s          │
└─────────────────┬───────────────────────────┘
                  ▼
┌─────────────────────────────────────────────┐
│ 3. 后处理器链（责任链）                      │
│    ① Deduplication (order=1)   去重         │
│    ② Fusion (order=5)         RRF 融合+截断  │
│    ③ Rerank (order=10)        精排 top10    │
│    ④ EvidenceGate (order=15)  证据闸门      │
│    ⑤ MetadataEnrichment (20)  元数据回表     │
└─────────────────┬───────────────────────────┘
                  ▼
            最终 top-k=10 条进 LLM
```

---

## 二、检索漏斗三段预算（⭐ 重点）

```
每通道召回 20 条 → RRF 融合 → 候选池截断 40 条 → Rerank → 最终 top-10 条进 LLM
(recallBudget)      (Fusion)    (candidateLimit)         (contextTopK)
```

| 预算 | 配置项 | 默认值 | 作用 |
|------|--------|--------|------|
| recallBudget | `rag.search.recall-budget` | 20 | 每通道召回条数，**保召回** |
| candidateLimit | `rag.search.fusion.rerank-candidate-limit` | 40 | 融合后送 Rerank 的候选池上限，**控成本** |
| contextTopK | `rag.search.default-top-k` | 10 | 最终进 LLM 的条数，**小而精** |

**不变式**：`recallBudget ≥ contextTopK` 且 `candidateLimit ≥ contextTopK`，启动时校验 fail-fast。

> **面试要点**："检索漏斗分三段预算——召回 20、候选池 40、最终 10。以前一个 topK 复用三处，调一处误动三处。现在显式拆成三段，各阶段只读自己的那一段。"

---

## 三、四个检索通道

### 3.1 通道列表

| 通道 | 实现类 | 数据源 | 优势 |
|------|--------|--------|------|
| 向量 | VectorSearchChannel | pgvector（HNSW 索引） | 语义相似度、同义词、改写 |
| 关键词 | KeywordSearchChannel | Elasticsearch（BM25） | 精确匹配、专有名词、数字 |
| 图谱 | GraphSearchChannel | LightRAG | 实体关系、层级结构 |
| 联网 | WebSearchChannel | 外部搜索引擎 API | 时效性内容 |

### 3.2 并行执行

```java
List<CompletableFuture<SearchChannelResult>> futures = enabledChannels.stream()
    .map(channel -> withTimeout(
        CompletableFuture.supplyAsync(() -> channel.search(context), ragRetrievalExecutor),
        channel, channelTimeoutMs))
    .toList();

List<SearchChannelResult> results = futures.stream()
    .map(CompletableFuture::join)
    .filter(Objects::nonNull)
    .toList();
```

**关键点**：
- `CompletableFuture.supplyAsync` 并行执行，线程池 `ragRetrievalExecutor`
- `withTimeout` 通道级超时（默认 15s），超时返回空结果不影响其他通道
- 异常通道返回 `emptyResult`，不让一个通道失败拖垮整个检索

> **面试要点**："四个通道用 CompletableFuture 并行执行，总延迟 = 最慢通道的延迟。每个通道有 15s 超时，超时返回空结果降级，不让最慢的钳制其他通道。"

---

## 四、RetrievalScopeResolver —— 检索作用域路由

### 4.1 全局 vs 定向

```
意图识别 → KB 意图最高分
    │
    ├─ >= 0.6 (confidence-threshold) → 定向检索（只查命中的知识库）
    │                                    + 补充路（25% 额度给未命中库兜底）
    │
    └─ < 0.6 → 全局检索（查所有知识库）
```

### 4.2 补充路（Supplement）

定向检索时，从各通道的召回额度里划出 `supplement-ratio=0.25` 给**未命中的知识库**，兜住意图判错的情况。

### 4.3 意图去重

多个子问题可能命中同一个意图节点，`extractKbIntents` 按节点 ID 去重，保留最高分。不去重的后果：
- 同一库被查多次
- 同一 chunk 在通道列表里占多个名次
- RRF 按名次累加分数翻倍

> **面试要点**："检索作用域由意图置信度决定——最高分 >= 0.6 走定向，否则全局。定向时留 25% 额度做补充路兜底。意图要按节点去重，否则 RRF 分数会虚高。"

---

## 五、后处理器链（责任链模式）

### 5.1 执行顺序

```
原始结果（多通道 chunks 合并）
    │
    ▼ ① Deduplication (order=1)    去重
    ▼ ② Fusion (order=5)           RRF 融合 + 截断候选池
    ▼ ③ Rerank (order=10)          精排 top-k
    ▼ ④ EvidenceGate (order=15)    证据闸门
    ▼ ⑤ MetadataEnrichment (20)    元数据回表
    │
    ▼
最终结果
```

每个处理器实现 `SearchResultPostProcessor` 接口，按 `getOrder()` 排序执行。单个处理器失败不中断链路（catch 后跳过）。

---

## 六、Deduplication —— 去重

```java
Map<String, RetrievedChunk> chunkMap = new LinkedHashMap<>();
for (SearchChannelResult result : results) {
    for (RetrievedChunk chunk : result.getChunks()) {
        chunkMap.putIfAbsent(RetrievedChunkKey.of(chunk), chunk);
    }
}
```

- 按 `RetrievedChunkKey`（docId + chunkId 等）去重
- 同一 chunk 多路命中时保留首次出现的实例
- **不比较跨通道分数**，最终名次由下游 RRF 赋分

> **面试要点**："去重用 LinkedHashMap 保持顺序，多路命中的 chunk 只保留一个。不比较原始分数，因为向量分和 BM25 分量纲不同不可比，统一交给 RRF 处理。"

---

## 七、Fusion —— RRF 融合（⭐ 重点）

### 7.1 RRF 公式

```
score(chunk) = Σ_channel [ weight_channel / (k + rank_channel) ]
```

- `rank_channel`：chunk 在该通道召回列表中的名次（从 0 开始）
- `k`：平滑常数，配置 `rag.search.fusion.rrf-k=20`
- `weight_channel`：通道权重（向量 1.0、关键词 1.0、图谱 0.8、联网 0.5）

### 7.2 为什么用 RRF 而不是直接加分？

向量分（余弦相似度 0~1）和关键词分（BM25 分数）**量纲不同，不可直接比较**。RRF 只看名次，天然跨模态可比。

### 7.3 为什么 k=20 而不是经典的 60？

注释解释得很清楚：
> 候选池通常仅 20~40 条，k=60 会过度平滑名次；调低至 20 让头部更有区分度

经典 RRF 的 k=60 是针对大规模候选（几百上千条），Ragent 的候选池只有 20~40 条，k=60 会让所有名次的权重差异太小。

### 7.4 通道加权

```yaml
rag:
  search:
    fusion:
      channel-weights:
        vector: 1.0
        keyword: 1.0
        graph: 0.8      # 图谱降权，因为跑在全局图上噪声多
        web-search: 0.5 # 联网搜索降权，可信度较低
```

让可信度不同的通道话语权不同，避免噪声通道靠名次抢前排。

### 7.5 截断候选池

融合后按 `candidateLimit=40` 截断，只把高分前 40 个送入 Rerank：
- 控制 Rerank 成本（cross-encoder 慢且贵）
- 让多路命中的候选凭 RRF 分数优先入选

> **面试要点**："RRF 融合用倒数名次加权，k=20（不是经典 60，因为候选池小）。通道有权重——图谱和联网降权。融合后截断到 40 条送 Rerank，控制成本。"

---

## 八、Rerank —— 精排

### 8.1 粗排 vs 精排

| 阶段 | 模型 | 速度 | 精度 |
|------|------|------|------|
| 粗排（向量检索） | 双塔模型（query 和 doc 分别编码） | 快 | 一般 |
| 精排（Rerank） | cross-encoder（query 和 doc 拼一起） | 慢 | 高 |

**策略**：先粗排召回 top-40，再精排到 top-10，兼顾速度和精度。

### 8.2 精排归因日志

RerankPostProcessor 会打两类归因日志：

1. **精排分布**：最高分、最低分，用于校准 `evidence.min-rerank-score`
2. **通道存活率**：对比 Rerank 前后各通道的候选数，重点看图谱证据存活率
   - 如果图谱大量进入 Rerank 却几乎不存活 → 纯成本，应降权或优化

> **面试要点**："Rerank 用 cross-encoder 精排，比向量检索精度高但慢。策略是先粗排 40 条再精排 10 条。我们还打归因日志看各通道存活率，如果图谱证据存活率低说明是纯成本，该降权。"

---

## 九、EvidenceGate —— 证据相关性闸门

### 9.1 为什么需要闸门？

检索只保证返回"最像的 N 条"，**库里没答案时照样满额返回**。如果不做相关性判断，噪声证据会进入 Prompt，导致 LLM 幻觉。

### 9.2 判定逻辑

```java
Float topScore = maxRerankScore(chunks);  // 全批最高精排分
if (topScore >= minRerankScore) {
    return chunks;      // 放行
}
return List.of();        // 整批丢弃
```

- 判据：**整批最高精排分**（不是逐条判）
- `min-rerank-score=0.2`：最高分低于 0.2 → 整批丢弃
- 误丢比误放贵（丢了至少不会幻觉，放了噪声可能幻觉）

### 9.3 启动校验

```java
if (minRerankScore > 0 && rerankEnabled == false) {
    throw new IllegalStateException("闸门将无分可读、恒放行");
}
```

如果配置了证据闸门但没开 Rerank，启动直接报错——避免闸门空转。

> **面试要点**："证据闸门解决库里没答案时噪声进 Prompt 的问题。判据是整批最高精排分，低于阈值整批丢弃。用最高分而不是平均分，因为误丢比误放贵。启动时校验 Rerank 必须开启，否则闸门无分可读。"

---

## 十、通道级超时降级

```java
private CompletableFuture<SearchChannelResult> withTimeout(future, channel, timeoutMs) {
    return future.orTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .exceptionally(e -> {
            if (e.getCause() instanceof TimeoutException) {
                log.warn("通道 {} 超时，放弃结果", channel.getName());
            }
            return channel.emptyResult(latencyMs);  // 返回空结果，不影响其他通道
        });
}
```

- 超时只放弃该通道结果，不中断其他通道
- 降级结果记**真实耗时**（不是 0），否则下游统计会把"等满超时"误读成"秒回没料"
- 这两者的处置方向相反：超时→调大超时；没料→补语料

---

## 十一、设计模式总结

| 设计模式 | 应用位置 | 作用 |
|----------|----------|------|
| 策略模式 | SearchChannel（4 个实现） | 不同检索通道可互换 |
| 责任链 | SearchResultPostProcessor（5 个） | 后处理按序执行，可增删 |
| 扇出/并行 | CompletableFuture | 多通道同时检索 |
| 漏斗 | RetrievalBudget 三段 | 召回→候选→最终，单调收窄 |
| 降级 | withTimeout + emptyResult | 单通道失败不拖垮全局 |

---

## 十二、面试高频问题（检索引擎篇）

### Q1：你们的混合检索是怎么做的？
> 四个通道并行——向量（pgvector HNSW）、关键词（ES BM25）、图谱（LightRAG）、联网搜索。每通道召回 20 条，超时 15s，超时返回空结果不影响其他通道。然后走后处理器链：去重 → RRF 融合 → 截断 40 条 → Rerank 精排到 10 条 → 证据闸门。

### Q2：RRF 融合的公式是什么？k 为什么取 20？
> `score = Σ weight / (k + rank)`。k=20 是因为候选池只有 20~40 条，经典 k=60 会过度平滑名次，让头部区分度不够。k 越小头部权重越大。

### Q3：为什么需要 Rerank？不能只用向量检索吗？
> 向量检索是双塔模型，速度快但精度有限。Rerank 用 cross-encoder，query 和 doc 拼一起算相似度，精度高但慢。所以先粗排 40 条再精排 10 条，兼顾速度和精度。

### Q4：证据闸门解决什么问题？
> 检索只返回最像的 N 条，库里没答案时照样满额返回，噪声会进 Prompt 导致幻觉。证据闸门看整批最高精排分，低于阈值（0.2）整批丢弃，让 LLM 回答"知识库中没有相关内容"而不是编造。

### Q5：检索作用域怎么决定查哪些库？
> 由意图识别的 KB 意图最高分决定。>= 0.6 走定向检索（只查命中库 + 25% 补充路兜底），< 0.6 走全局检索。意图要按节点去重，否则 RRF 分数会虚高。

### Q6：三段预算为什么要拆开？
> 以前一个 topK 复用三处——召回、候选池、最终条数，调一处误动三处。现在拆成 recallBudget=20（保召回）、candidateLimit=40（控 Rerank 成本）、contextTopK=10（小而精），各阶段只读自己的。

---

## 十三、本篇小结

多路检索引擎的核心价值是**高精度 + 高可用**：

1. **四通道并行**：向量/关键词/图谱/联网互补，CompletableFuture 并行，单通道超时降级
2. **作用域路由**：意图置信度决定定向/全局，补充路兜底意图判错
3. **RRF 融合**：倒数名次加权，跨模态可比，k=20 适配小候选池，通道加权降噪
4. **两阶段排序**：粗排（向量）→ 精排（Rerank），兼顾速度和精度
5. **证据闸门**：整批最高分判相关性，库里没答案时丢弃噪声防幻觉
6. **三段预算**：召回 20 → 候选 40 → 最终 10，单调收窄，各管各的

下一篇预告：**学习6 —— ReAct Agent 执行框架详解**（工具体系、记忆管线、确认机制、MCP 桥接）。
