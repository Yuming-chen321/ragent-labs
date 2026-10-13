# Ragent 知识库分块功能 · 问题排查与解决

> 排查时间：2026-09-09
> 项目路径：`d:\javaCODE\Practical trainning two\ragent`

---

## 一、项目简介

Ragent 是一个基于 **Spring Boot 4.1.0 + Java 21** 构建的 AI Agent 平台，集成了 RAG（检索增强生成）知识库管理、MinerU 文档解析、阿里云百炼大模型、pgvector 向量存储等组件。后端运行在 Docker Compose 环境中，包含 PostgreSQL、Redis、RocketMQ、MinIO 等中间件。

**核心链路（知识库分块）：**
```
管理后台 → 创建知识库 → 上传文件 → 点击"切分"
  → [解析] MinerU / Tika 提取文本
  → [切分] 分块器按策略切块
  → [嵌入] 调用 Embedding 模型生成向量
  → [持久化] 写入 pgvector (t_knowledge_vector)
  → [结果] 文档状态变为 success，chunk_count 有值
```

---

## 二、问题概览

| # | 问题 | 根因 | 解决方式 | 涉及技术 |
|---|------|------|----------|----------|
| 1 | 点击"切分"后无响应，文档状态卡在 running | RocketMQ 事务消息在 Spring Boot 4 + rocketmq-spring-boot-starter 2.3.6 下消费端不触发 | 绕过 RocketMQ，改用 `CompletableFuture.runAsync()` 直接调用分块执行方法 | RocketMQ、Spring Boot 4 兼容性、CompletableFuture |
| 2 | 分块执行报错 `ollama embedding 请求失败: Failed to connect to localhost:11434` | 数据库知识库配置 `embedding_model=qwen-emb-local`（Ollama 本地），但未安装 Ollama；问答走全局默认 bailian 所以正常 | 更新数据库 `t_knowledge_base.embedding_model` 为 `qwen-emb-8b`（阿里云百炼 text-embedding-v4） | 知识库级模型路由、模型候选列表、bailian vs ollama |
| 3 | 向量化写入报错 `ERROR: expected 1536 dimensions, not 1024` | pgvector 表 `t_knowledge_vector.embedding` 类型是 `vector(1536)`，但百炼 text-embedding-v4 实际输出 1024 维；YAML 配置 `rag.default.dimension=1024` 与建表语句不一致 | 统一为 1024 维：修改 YAML 或重建表结构（根据最终运行结果确认） | pgvector、向量维度一致性、DDL 与配置对齐 |

---

## 三、问题一：RocketMQ 事务消息消费端不触发

### 现象

- 点击"切分"按钮 → 后端日志打出 `发送分块MQ消息`，Producer 返回 `SEND_OK`
- 数据库 `t_knowledge_document` 状态从 pending 变为 running
- 但**没有任何消费者日志**，文档永远卡在 running
- 数据库 `t_knowledge_document_chunk_log` 始终为空（0 行）
- 阿里云百炼平台无调用记录

### 排查过程

**Step 1：验证消息是否真的进入 Broker**

```bash
# 查看 Broker commitlog 文件大小
docker exec rmqbroker sh -c "ls -la /home/rocketmq/store/commitlog/"
# 输出：00000000000000000000 文件大小 1073741824（有写入）
```

**Step 2：验证消费者是否连接 Broker**

```bash
# Broker 日志搜索 consumer group
docker exec rmqbroker sh -c "grep 'knowledge-document-chunk_cg' /home/rocketmq/logs/rocketmqlogs/broker.log"
# 输出：有 "new consumer connected" 日志 ✓
```

**Step 3：验证消费者是否真的被拉消息**

```bash
# Broker 日志搜索 SendMessageThread 和 PullMessageThread
docker exec rmqbroker sh -c "grep 'SendMessageThread\|PullMessageThread' /home/rocketmq/logs/rocketmqlogs/broker.log"
# 输出：Broker 创建了 topic 但 PullMessageThread 从未被触发 ✗
```

**Step 4：定位兼容性问题**

- 框架版本：`Spring Boot 4.1.0` + `rocketmq-spring-boot-starter 2.3.6` + `RocketMQ Server 5.2.0`
- 原代码使用 `@RocketMQTransactionListener` 实现事务消息 HALF → COMMIT 流程
- 在 Spring Boot 4 的自动装配变更下，事务监听器无法正确注册到 Producer 的回调链
- HALF 消息虽然本地执行了（文档状态变了），但消息本身未被正确 COMMIT，导致消费端 Pull 请求不到

### 解决方案

**绕过 RocketMQ，改为 JVM 线程异步执行：**

修改文件：`KnowledgeDocumentServiceImpl.java`

```java
// 原方案（已废弃）：
// transactionOperations.executeWithoutResult(...) // 本地事务更新状态
// messageQueueProducer.send(CHUNK_TOPIC, ...)      // 发送事务消息

// 新方案：
transactionOperations.executeWithoutResult(...)       // 本地事务更新状态
java.util.concurrent.CompletableFuture.runAsync(() -> {
    UserContext.set(LoginUser.builder().username(username).build());
    executeChunk(docId);                              // 直接同步调用分块执行方法
});
```

同时清理掉不再需要的 `MessageQueueProducer` 注入、`KnowledgeDocumentChunkEvent` 构造、`CHUNK_TOPIC` 常量等。

### 为什么这样做

- RocketMQ 事务消息的 HALF → COMMIT 机制在当前版本组合下不稳定，短期没必要深挖 starter 和 server 版本匹配
- 分块任务本身就是**后台异步任务**，用 `CompletableFuture.runAsync()` 已经能保证与 HTTP 请求解耦
- 生产者-消费者分离的设计初衷是**削峰填谷**，在单机/小规模部署下 JVM 内线程完全够用
- 后续如果需要横向扩展，可再切回 MQ 并升级 starter 版本

### 涉及技术

| 技术 | 版本 | 作用 |
|------|------|------|
| RocketMQ | 5.2.0 | 消息中间件，事务消息 HALF/COMMIT |
| rocketmq-spring-boot-starter | 2.3.6 | Spring Boot 整合 RocketMQ |
| Spring Boot | 4.1.0 | 自动装配变更导致 @RocketMQTransactionListener 注册失败 |
| CompletableFuture | JDK 21 | 替代 MQ 的异步执行方案 |
| Spring TransactionOperations | - | 本地事务管理器 |

---

## 四、问题二：知识库嵌入模型配置错误

### 现象

绕过 MQ 后分块链路终于跑起来了，但执行到嵌入环节时报错：

```
ollama embedding 请求异常 - 模型：qwen3-embedding:8b-fp16，条数：1，耗时：10ms，
原因：Failed to connect to localhost/[0:0:0:0:0:0:0:1]:11434

RemoteException: All Embedding model candidates failed: 
ollama embedding 请求失败: Failed to connect to localhost/[0:0:0:0:0:0:0:1]:11434
```

有趣的是，**问答功能正常**——问答时 bailian embedding 成功（日志显示 `bailian embedding 完成 - 模型：text-embedding-v4`）。

### 排查过程

**Step 1：理解模型路由机制**

项目有两层模型配置：
- **部署级默认值**（YAML）：`rag.default.embedding-model = qwen-emb-8b`
- **知识库级配置**（数据库表）：`t_knowledge_base.embedding_model`

分块流程调用链：
```
ChunkEmbeddingService.embed(chunks, target)
  → target.embeddingModel()   // 来自知识库配置！
  → embeddingService.embedBatch(texts, modelId)
  → RoutingEmbeddingService: 只传了一个 modelId → 只用这一个候选，不走 fallback
```

问答流程调用链：
```
MultiChannelRetrievalEngine
  → 无显式 modelId → selector.selectEmbeddingCandidates()
  → 候选列表：[qwen-emb-8b(bailian), qwen-emb-local(ollama), ...]
  → 第一个 bailian 成功 → 返回
```

**Step 2：查看数据库实际配置**

```sql
SELECT id, name, embedding_model FROM t_knowledge_base WHERE deleted=0;
```

结果：

| 知识库 | embedding_model | 实际 provider | 状态 |
|--------|----------------|---------------|------|
| 666（当前在用） | `qwen-emb-local` | Ollama localhost:11434 | ❌ 未安装 |
| 12345 | `qwen-emb-8b` | 阿里云百炼 | ✅ |
| ai应用开发2 | `qwen-emb-8b` | 阿里云百炼 | ✅ |

**Step 3：理解候选模型定义（YAML）**

```yaml
ai:
  embedding:
    default-model: qwen-emb-8b     # 默认选这个
    candidates:
      - id: qwen-emb-8b
        provider: bailian
        model: text-embedding-v4
        priority: 1                # 优先级最高
      - id: qwen-emb-local
        provider: ollama
        model: qwen3-embedding:8b-fp16
        priority: 2                # 本地 fallback
```

### 解决方案

直接修改数据库，把知识库 666 的嵌入模型从 Ollama 改为百炼：

```sql
UPDATE t_knowledge_base SET embedding_model = 'qwen-emb-8b' WHERE id = '2097567470406701056';
```

### 根因分析

前端创建知识库时，嵌入模型下拉列表默认选中了 `qwen-emb-local`（Ollama），用户没有切换。但 Ollama 未部署，分块时命中了这个本地模型就直接失败了。

**为什么这个问题被 RocketMQ 的问题掩盖了？** 因为之前 RocketMQ 根本没把消息送到消费者，所以永远走不到嵌入环节。

### 涉及技术

| 技术 | 作用 |
|------|------|
| 模型路由（ModelSelector + ModelRoutingExecutor） | 多候选故障转移 |
| 知识库级模型配置（t_knowledge_base.embedding_model） | 每个知识库独立配置 |
| BailianEmbeddingClient | 阿里云百炼 Embedding 客户端 |
| OllamaEmbeddingClient | 本地 Ollama Embedding 客户端（未部署） |
| RoutingEmbeddingService | 统一路由，支持 fallback |

---

## 五、问题三：向量维度不一致

### 现象

嵌入调用成功后，写入 pgvector 表时报错：

```
PreparedStatementCallback; SQL [INSERT INTO t_knowledge_vector (...)];
ERROR: expected 1536 dimensions, not 1024
```

### 排查过程

**Step 1：确认表结构**

```sql
\d t_knowledge_vector;
-- embedding       | vector(1536)    -- 建表时写死了 1536 维
```

**Step 2：确认 YAML 配置**

```yaml
rag:
  default:
    dimension: 1024               -- 部署级配置是 1024
```

**Step 3：确认模型输出**

百炼 `text-embedding-v4` 输出 **1024 维**向量（官方文档可查）。

| 来源 | 维度 |
|------|------|
| pgvector 建表 DDL | 1536 |
| YAML `rag.default.dimension` | 1024 |
| 百炼 text-embedding-v4 实际输出 | 1024 |

**三处不一致，建表 DDL 与实际模型输出不符。**

### 解决方案

需统一为 1024 维（以实际模型输出为准）：

```sql
ALTER TABLE t_knowledge_vector ALTER COLUMN embedding TYPE vector(1024);
```

或重建表：

```sql
DROP TABLE t_knowledge_vector;
CREATE TABLE t_knowledge_vector (
    id              VARCHAR(20) PRIMARY KEY,
    collection_name VARCHAR(64) NOT NULL,
    content         TEXT,
    metadata        JSONB,
    embedding       vector(1024)       -- 对齐百炼 text-embedding-v4
);
CREATE INDEX idx_kv_embedding ON t_knowledge_vector
    USING hnsw (embedding vector_cosine_ops);
```

### 根因分析

初始建表时可能参考了其他嵌入模型（如 text-embedding-3-large 是 1536 维），但后来切换到百炼 text-embedding-v4 后忘记同步修改 DDL。这是典型的**配置漂移（Configuration Drift）**。

### 涉及技术

| 技术 | 作用 |
|------|------|
| pgvector | PostgreSQL 向量扩展 |
| vector(1536) / vector(1024) | 向量列类型，写死维度 |
| HNSW 索引 | 近似最近邻搜索索引 |
| 阿里云百炼 text-embedding-v4 | 输出 1024 维 |
| YAML 配置 `rag.default.dimension` | 部署级约束 |

---

## 六、涉及技术栈总览

| 分类 | 技术 | 版本 | 说明 |
|------|------|------|------|
| 语言 | Java | 21.0.8 | LTS |
| 框架 | Spring Boot | 4.1.0 | 最新版本 |
| 消息 | RocketMQ Server | 5.2.0 | Docker Compose 部署 |
| MQ Starter | rocketmq-spring-boot-starter | 2.3.6 | ⚠️ 与 Boot 4.1.0 有兼容性问题 |
| 数据库 | PostgreSQL | - | Docker Compose 部署 |
| 向量扩展 | pgvector | - | 1024 维 |
| 缓存 | Redis | - | Docker Compose 部署 |
| 对象存储 | MinIO (rustfs) | - | Docker Compose 部署 |
| 大模型 | 阿里云百炼 DashScope | text-embedding-v4, qwen3-max | Embedding 1024 维 |
| 文档解析 | MinerU | - | Word/PDF 解析 |
| 文档解析 | Apache Tika | - | txt/md 解析 |
| API Key | MinerU Token | - | 已配置有效 |
| 并发 | CompletableFuture | JDK 21 | 替代 MQ 的异步方案 |

---

## 七、经验总结

1. **RocketMQ Starter 版本要对齐** — Spring Boot 4 是大版本升级，starter 2.3.6 与事务消息监听器有注册问题。生产环境优先用已验证的版本组合（如 Boot 3.x + rocketmq-starter 2.2.x）。

2. **模型配置有两层** — 部署级默认 + 知识库级独立配置。排查模型问题时，**不要只看 YAML 默认值**，一定要查数据库表（`t_knowledge_base.embedding_model`），分块和检索走的路径不一样。

3. **pgvector 建表维度硬约束** — `vector(1536)` 这种写法是写死的，模型切换后必须同步 DDL。建议在应用启动时加一个维度校验，不符合直接 fail-fast。

4. **问题掩盖问题** — RocketMQ 消费者不触发（问题 A）掩盖了嵌入模型配置错误（问题 B）和向量维度不一致（问题 C）。只有先解决 A，才能走到 B 和 C。排查时**从入口到终点逐步放行**，每放行一步就能暴露下一层的问题。

5. **IDE 运行和后台进程会冲突** — 用 `java -jar` 启动后再从 IDEA 启动会导致端口冲突。切换运行方式前先杀掉旧进程（`netstat -ano | findstr :9090` → `taskkill /PID xxx /F`）。

6. **Windows 路径中的长文件名** — Trae IDEA 缓存文件路径非常深，路径中包含 `.txt` 后缀时直接用 Glob 搜索比手敲完整路径更可靠。
