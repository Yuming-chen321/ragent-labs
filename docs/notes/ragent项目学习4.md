# Ragent 项目学习（四）：知识库入库链路详解

> 系列：ragent项目学习4
> 本篇目标：搞懂知识库入库的完整链路——上传→存储→分块→嵌入→落库，以及状态管理、信号量、维度校验等工程细节
> 对应源码：`d:\javaCODE\Practical trainning two\ragent\rag\src\main\java\com\nageoffer\ai\ragent\knowledge` + `core\ingest`

---

## 一、入库链路全景

```
用户上传文档
    │
    ▼
┌─────────────────────────────────────────────┐
│ 1. upload()  上传入库                        │
│    ├─ 校验知识库存在                          │
│    ├─ 存储文件到 S3/rustfs                    │
│    ├─ 校验解析器支持该 MIME                   │
│    └─ 插入文档记录（状态 PENDING）             │
└─────────────────┬───────────────────────────┘
                  │ 用户点击"切分"
                  ▼
┌─────────────────────────────────────────────┐
│ 2. startChunk()  启动分块                    │
│    ├─ 本地事务：PENDING → RUNNING（乐观锁）   │
│    └─ CompletableFuture.runAsync 异步执行    │
└─────────────────┬───────────────────────────┘
                  ▼
┌─────────────────────────────────────────────┐
│ 3. runChunkTask()  分块执行                  │
│    ├─ 解析 processMode                       │
│    ├─ 插入 chunk_log（RUNNING）              │
│    └─ ingestionKernel.run() 五步摄取          │
└─────────────────┬───────────────────────────┘
                  ▼
┌─────────────────────────────────────────────┐
│ 4. DefaultIngestionKernel.run()  摄入内核    │
│    ① identity：MIME 类型识别                  │
│    ② parse：解析器提取文本（MinerU/Tika）      │
│    ③ chunk：分块服务切块                      │
│    ④ embed：向量化（指定模型 + 维度校验）      │
│    ⑤ index：扇出写入（pgvector/ES/图谱）      │
└─────────────────┬───────────────────────────┘
                  ▼
┌─────────────────────────────────────────────┐
│ 5. 状态更新                                  │
│    ├─ 成功 → 文档状态 SUCCESS + chunk_count  │
│    └─ 失败 → 文档状态 FAILED + error_message │
└─────────────────────────────────────────────┘
```

---

## 二、文档状态机

```
PENDING ──startChunk──→ RUNNING ──成功──→ SUCCESS
                            │
                            └──失败──→ FAILED
```

| 状态 | 含义 | 触发时机 |
|------|------|----------|
| PENDING | 待处理 | 上传完成，等待切分 |
| RUNNING | 处理中 | 用户点击"切分"，本地事务更新 |
| SUCCESS | 成功 | 五步摄取全部完成，chunk_count > 0 |
| FAILED | 失败 | 任意一步异常，记录 error_message |

**状态更新用乐观锁**（startChunk 中）：
```java
documentMapper.update(wrapper
    .set(status, RUNNING)
    .eq(id, docId)
    .ne(status, RUNNING)   // 只有当前不是 RUNNING 才更新
);
if (updated == 0) {
    throw new ClientException("文档分块操作正在进行中");
}
```

> **面试要点**："用 `ne(status, RUNNING)` 做乐观锁，防止用户重复点击切分。如果已经是 RUNNING，update 返回 0，直接拒绝。"

---

## 三、upload() —— 上传入库

### 3.1 核心流程

```java
public KnowledgeDocumentVO upload(kbId, request, file) {
    // ① 校验知识库存在
    KnowledgeBaseDO kbDO = knowledgeBaseMapper.selectById(kbId);
    
    // ② 解析来源类型（FILE/URL）和处理模式
    SourceType sourceType = SourceType.normalize(request.getSourceType());
    ProcessModeConfig modeConfig = resolveProcessModeConfig(request);
    
    // ③ 存储文件到 S3/rustfs
    StoredFileDTO stored = resolveStoredFile(kbDO.getCollectionName(), sourceType, ...);
    
    // ④ 前置校验：无解析器的类型直接拒绝，不留下孤儿文件
    if (!parserRegistry.canParse(stored.getMimeType())) {
        fileStorageService.deleteByUrl(stored.getUrl());
        throw new ClientException("暂不支持的文件类型");
    }
    
    // ⑤ 插入文档记录（状态 PENDING）
    KnowledgeDocumentDO documentDO = KnowledgeDocumentDO.builder()
        .kbId(kbId)
        .fileUrl(stored.getUrl())
        .status(DocumentStatus.PENDING.getCode())
        .processMode(modeConfig.processMode().getValue())
        .ingestionSpec(modeConfig.ingestionSpec())
        .build();
    documentMapper.insert(documentDO);
}
```

### 3.2 设计要点

1. **校验前置**：解析器校验在存储文件之前？不，是**存储之后、插库之前**。注释解释了原因：摄取配置校验只看请求参数，一旦落了对象再抛异常，存储里就留下孤儿文件。所以"纯校验一律前置到第一个副作用之前"。

2. **按 collectionName 隔离**：所有知识库文档存在同一个桶 `ragent-sources`，按 `collectionName` 目录隔离。

3. **MIME 类型双保险**：上传时 `parserRegistry.canParse(mimeType)` 校验，摄取时 `MimeTypeDetector.detect()` 再次识别（可能探测出不同的真实 MIME）。

> **面试要点**："上传时先存文件再校验解析器支持，不支持就删文件抛异常。文档状态初始为 PENDING，等用户点切分才开始处理。"

---

## 四、startChunk() —— 启动分块（你改过的代码）

### 4.1 核心流程

```java
public void startChunk(docId) {
    // ① 本地事务：PENDING → RUNNING（乐观锁防重复）
    transactionOperations.executeWithoutResult(status -> {
        int updated = documentMapper.update(wrapper
            .set(status, RUNNING)
            .eq(id, docId)
            .ne(status, RUNNING));
        if (updated == 0) throw new ClientException("正在进行中");
    });
    
    // ② 同步更新定时调度（URL 类型才继续）
    scheduleService.upsertSchedule(documentDO);
    
    // ③ 异步执行分块（绕过 RocketMQ）
    String username = UserContext.getUsername();
    CompletableFuture.runAsync(() -> {
        try {
            UserContext.set(LoginUser.builder().username(username).build());
            executeChunk(docId);
        } finally {
            UserContext.clear();
        }
    });
}
```

### 4.2 为什么用 CompletableFuture 绕过 RocketMQ？

这是你踩过的坑（详见 `ragent项目问题解决.md`）：

- **原方案**：RocketMQ 事务消息，`@RocketMQTransactionListener` 执行本地事务 + COMMIT
- **问题**：Spring Boot 4.1.0 + rocketmq-spring-boot-starter 2.3.6 兼容性问题，事务监听器注册失败，HALF 消息不 COMMIT，消费端拉不到
- **现方案**：`CompletableFuture.runAsync()` 直接调用 `executeChunk()`

**异步线程必须手动传 UserContext**：
```java
String username = UserContext.getUsername();  // 主线程取
CompletableFuture.runAsync(() -> {
    UserContext.set(LoginUser.builder().username(username).build());  // 子线程设
    executeChunk(docId);
});
```

> **面试要点**："原来用 RocketMQ 事务消息做异步解耦，但 Spring Boot 4 和 starter 2.3.6 有兼容性问题，事务监听器注册不上。短期绕过方案是 CompletableFuture 异步执行，因为分块本身就是后台任务，单机部署下 JVM 线程够用。注意异步线程里要手动 set UserContext，TTL 不会自动传递。"

---

## 五、DefaultIngestionKernel —— 摄入内核五步（⭐ 核心）

### 5.1 五步骨架

```java
public IngestionOutcome run(doc, bytes, spec, target) {
    // ① identity：全链路唯一一次 MIME 类型识别
    String mimeType = MimeTypeDetector.detect(bytes, doc.filename());
    
    // ② parse：(MIME × 档位) → 解析器 → 文本 Block 列表
    DocumentParser parser = parserRegistry.require(mimeType, spec.parseProfile());
    ParsedDocument parsed = parser.parseStructured(bytes, mimeType, options);
    List<Block> blocks = parsed.blocks();
    
    // ③ chunk：Block 列表 → Chunk 列表（按预算切块）
    List<Chunk> chunks = chunkingService.chunk(blocks, spec.budget());
    
    // ④ embed：Chunks → EmbeddedChunks（指定模型 + 维度校验）
    List<EmbeddedChunk> embedded = chunkEmbeddingService.embed(chunks, target);
    
    // ⑤ index：扇出写入全部落点（pgvector + ES + 图谱）
    chunkIndexWriter.replaceDocument(target, doc, embedded);
    
    return new IngestionOutcome(mimeType, ..., timings);
}
```

### 5.2 每一步详解

#### ① identity —— MIME 类型识别
- 用 `MimeTypeDetector.detect(bytes, filename)` 基于文件内容探测（不是扩展名）
- 全链路**只识别一次**，后续步骤复用这个 MIME
- 无法识别直接抛异常

#### ② parse —— 文档解析
- `ParserRegistry.require(mimeType, parseProfile)` 按 MIME × 档位选解析器
- 解析器分两类：
  - **MinerU**：PDF/Word/PPT，走 SaaS API 轮询（支持表格、公式、OCR）
  - **Tika**：txt/md 等纯文本
- 输出 `ParsedDocument`（Block 列表），Block 带类型（标题/段落/表格/图片）

#### ③ chunk —— 分块
- `ChunkingService.chunk(blocks, budget)` 按预算切块
- 预算来自 `IngestionSpec.budget()`（chunk size、overlap 等）
- 输出 `List<Chunk>`，每个 Chunk 有 `embeddingText`（用于向量化的文本）

#### ④ embed —— 向量化（详见第六节）

#### ⑤ index —— 索引扇出（详见第七节）

### 5.3 计时埋点

`IngestionOutcome.IngestionTimings` 记录每一步耗时：
- `parseMillis` 解析耗时
- `chunkMillis` 分块耗时
- `embedMillis` 嵌入耗时
- `indexMillis` 索引耗时

最终写入 `t_knowledge_document_chunk_log`，管理后台可查看分块耗时分布。

> **面试要点**："摄入内核是固定的五步骨架——识别、解析、分块、嵌入、索引。每步独立计时，便于性能分析。解析器按 MIME × 档位路由，MinerU 处理 PDF/Word，Tika 处理纯文本。"

---

## 六、ChunkEmbeddingService —— 向量化与维度校验

### 6.1 核心逻辑

```java
public List<EmbeddedChunk> embed(List<Chunk> chunks, VectorTarget target) {
    // ① 批量向量化（指定模型，不降级）
    List<String> texts = chunks.stream().map(Chunk::embeddingText).toList();
    List<List<Float>> vectors = embeddingService.embedBatch(texts, target.embeddingModel());
    
    // ② 条数校验
    if (vectors.size() != chunks.size()) {
        throw new ServiceException("向量结果条数与分块不符");
    }
    
    // ③ 逐条维度校验
    for (int i = 0; i < chunks.size(); i++) {
        List<Float> row = vectors.get(i);
        if (row.size() != target.dimension()) {
            throw new ServiceException(String.format(
                "嵌入维度不符：模型 %s 输出 %d 维，物理空间要求 %d 维",
                target.embeddingModel(), row.size(), target.dimension()));
        }
    }
}
```

### 6.2 为什么指定模型不降级？

`embeddingService.embedBatch(texts, target.embeddingModel())` 传了 `modelId`，走的是 `RoutingEmbeddingService.embed(texts, modelId)` 单模型模式，**不降级**。

**原因**：不同 Embedding 模型输出维度不同（百炼 text-embedding-v4 = 1024，text-embedding-3-large = 1536）。如果降级到其他维度的模型，向量写入 pgvector 会报维度不匹配。**宁可失败也不能降级**。

### 6.3 维度校验的意义

`row.size() != target.dimension()` 校验是**最后一道防线**。如果模型输出维度和表结构不一致，在这里直接报错，而不是漂到 pgvector 的类型转换才暴露。

> ⚠️ **关联你的坑**：之前 `expected 1536 dimensions, not 1024` 就是维度不一致。现在 ChunkEmbeddingService 会在写入前校验，提前暴露问题。

---

## 七、ChunkIndexWriter —— 索引扇出

### 7.1 扇出架构

```
ChunkIndexWriter
    │
    ├─ 遍历 List<ChunkSink>
    │   ├─ PgVectorStoreService      → pgvector 向量库
    │   ├─ KeywordSyncingVectorStoreService → ES 关键词索引（装饰器）
    │   └─ GraphSyncingVectorStoreService   → LightRAG 知识图谱（装饰器）
    │
    └─ 全部在同一个事务里
```

### 7.2 事务一致性

```java
public void replaceDocument(target, doc, chunks) {
    transactionOperations.executeWithoutResult(status ->
        sinks.forEach(sink -> sink.replaceDocument(target, doc, chunks)));
}
```

所有落点在**同一个本地事务**里，要么全部成功，要么全部回滚。保证向量、关键词、图谱的数据一致性。

### 7.3 装饰器模式

`KeywordSyncingVectorStoreService` 和 `GraphSyncingVectorStoreService` 是装饰器，在向量写入时"顺手"同步 ES 和图谱：

```
业务调 vectorStoreService.write(chunks)
    │
    ▼
KeywordSyncingVectorStoreService (装饰器)
    ├─ 调真正的 PgVectorStoreService.write(chunks)  → 写向量
    └─ 同步写 ES 关键词索引
    │
    ▼
GraphSyncingVectorStoreService (装饰器)
    ├─ 调下层（可能是 KeywordSyncing 或 PgVector）
    └─ 同步写 LightRAG 图谱
```

> **面试要点**："索引写入用扇出架构——一个 ChunkIndexWriter 遍历多个 ChunkSink，全部在一个事务里。新增索引后端只需加一个 ChunkSink bean，内核和写入器都不用改。ES 和图谱同步用装饰器模式，业务无感知。"

---

## 八、VectorTargetResolver —— 向量目标解析

### 8.1 两层配置

```
知识库级（L2）：t_knowledge_base.embedding_model  → 模型 ID
部署级（L1）：rag.default.dimension              → 向量维度
                          │
                          ▼
                    VectorTarget(collectionName, embeddingModel, dimension)
```

### 8.2 缺配置直接失败

```java
public VectorTarget resolve(KnowledgeBaseDO kbDO) {
    if (!StringUtils.hasText(kbDO.getEmbeddingModel())) {
        throw new ClientException("知识库未配置嵌入模型");
    }
    Integer dimension = ragDefaultProperties.getDimension();
    if (dimension == null || dimension <= 0) {
        throw new ClientException("部署未配置向量维度");
    }
    return new VectorTarget(kbDO.getCollectionName(), kbDO.getEmbeddingModel(), dimension);
}
```

**为什么不回落默认值？** 注释说得很清楚：
> 原先每个写向量的调用点各自从知识库取模型、各自决定要不要回落系统默认，于是上传路径用知识库配置的模型、管道路径用系统默认模型，同一个分区里混进了两种语义空间的向量。

所以统一成一个组件产生 VectorTarget，缺配置直接失败，避免语义空间混乱。

---

## 九、流量保护：上传信号量

### 9.1 配置

```yaml
rag:
  semaphore:
    document-upload:
      name: rag:document:upload
      max-concurrent: 10      # 最大并发 10
      max-wait-seconds: 5     # 等待 5 秒
      lease-seconds: 300      # 许可 5 分钟自动释放
```

### 9.2 作用

- 防止大量上传请求同时打满对象存储和数据库连接
- `max-wait-seconds` 超时后拒绝，避免请求堆积
- `lease-seconds` 防止 Worker 宕机导致许可泄漏

> **面试要点**："上传用 Redisson 信号量限流，最大 10 并发。许可有自动释放时间，防止 Worker 宕机导致许可泄漏。"

---

## 十、chunk_log —— 分块日志

每次分块执行都会插入一条 `t_knowledge_document_chunk_log` 记录：

| 字段 | 说明 |
|------|------|
| doc_id | 文档 ID |
| status | running/success/failed |
| process_mode | direct/pipeline |
| parse_profile | 解析档位 |
| chunk_count | 切出的块数 |
| extract_duration | 解析耗时（ms） |
| chunk_duration | 分块耗时（ms） |
| embed_duration | 嵌入耗时（ms） |
| persist_duration | 持久化耗时（ms） |
| total_duration | 总耗时（ms） |
| error_message | 失败原因 |

管理后台可查看每次分块的详细耗时和错误信息。

---

## 十一、设计模式总结

| 设计模式 | 应用位置 | 作用 |
|----------|----------|------|
| 模板方法 | DefaultIngestionKernel | 固定五步骨架，每步可扩展 |
| 策略模式 | ParserRegistry / ChunkingService | 按 MIME/类型选不同实现 |
| 装饰器 | KeywordSyncing/GraphSyncingVectorStoreService | 向量写入时同步 ES/图谱 |
| 扇出 | ChunkIndexWriter | 遍历多个 ChunkSink |
| 乐观锁 | startChunk 状态更新 | ne(status, RUNNING) 防重复 |
| 本地事务 | ChunkIndexWriter.replaceDocument | 多落点一致性 |

---

## 十二、面试高频问题（入库链路篇）

### Q1：文档从上传到可检索，经历了哪些步骤？
> 五步：① MIME 类型识别；② 解析器提取文本（MinerU 处理 PDF/Word，Tika 处理纯文本）；③ 分块服务切块；④ Embedding 向量化（指定模型，维度校验）；⑤ 扇出写入 pgvector + ES + 图谱。文档状态从 PENDING → RUNNING → SUCCESS。

### Q2：为什么分块用 CompletableFuture 而不是 RocketMQ？
> 原来用 RocketMQ 事务消息，但 Spring Boot 4 和 starter 2.3.6 有兼容性问题，事务监听器注册不上，消息不消费。短期绕过用 CompletableFuture，因为分块是后台异步任务，单机部署 JVM 线程够用。后续要横向扩展再切回 MQ。

### Q3：Embedding 为什么指定模型不降级？
> 不同模型输出维度不同（如 1024 vs 1536），降级会导致同一知识库向量维度不一致，pgvector 写入报错。所以宁可失败也不降级，失败后在 ChunkEmbeddingService 维度校验环节提前暴露。

### Q4：怎么防止用户重复点击切分？
> 用乐观锁——更新状态时加 `ne(status, RUNNING)` 条件，如果已经是 RUNNING，update 返回 0，抛"正在进行中"异常。

### Q5：多个索引后端（向量+关键词+图谱）怎么保证一致性？
> ChunkIndexWriter 遍历所有 ChunkSink，全部在同一个本地事务里执行，要么全部成功要么全部回滚。新增后端只需加一个 ChunkSink bean。

### Q6：维度不一致问题怎么防？
> 三道防线：① VectorTargetResolver 统一产生模型和维度，缺配置直接失败；② ChunkEmbeddingService 逐条校验 `row.size() == target.dimension()`；③ pgvector 表结构硬约束。三道防线确保不会出现维度漂移。

---

## 十三、本篇小结

知识库入库链路的核心价值是**工程化的文档处理流水线**：

1. **状态机管理**：PENDING → RUNNING → SUCCESS/FAILED，乐观锁防重复
2. **五步摄入内核**：识别→解析→分块→嵌入→索引，每步独立计时
3. **维度三重防护**：Resolver 统一配置 → EmbeddingService 校验 → pgvector 硬约束
4. **索引扇出**：多落点同事务，装饰器同步 ES/图谱
5. **流量保护**：上传信号量限流，许可自动释放防泄漏
6. **可观测**：chunk_log 记录每步耗时和错误

下一篇预告：**学习5 —— 多路检索引擎详解**（四通道并行、作用域路由、RRF 融合、Rerank 精排、证据闸门）。
