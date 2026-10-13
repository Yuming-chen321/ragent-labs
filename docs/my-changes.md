# 我的改动清单（相对上游 nageoffer/ragent）

> 查看命令：`git log upstream-snapshot..main --oneline` 与 `git diff --stat upstream-snapshot..main`
>
> `upstream-snapshot` 标签打在导入时的上游快照上（`08c77d0`），对应上游 commit `62a81a58c3b8b44ad1501ef70d9b70f2b9480ef4`。
> 上游在此之后已继续演进（>70 个提交），因此**不要**用 `upstream/main` 作为对比基准，否则会把上游的新工作算进本次改动。
>
> 上游基线：`62a81a58c3b8b44ad1501ef70d9b70f2b9480ef4`（Apache-2.0，版权与 LICENSE 保留）

每条记录按「动机 → 改动 → 代价 → 后续」组织。

**归属声明（重要）**：本清单中标注「上游」的修复是上游作者的成果，本仓库做的是同步、理解与回归验证；
标注「本仓库」的才是本仓库在部署与排障过程中产生的工作。两者不混为一谈。

---

## 1. 密钥外置化（安全）— 本仓库

- **动机**：本地部署需要真实 API Key（百炼 / MinerU），但明文 key 不能进入版本库。
- **改动**：`application.yaml` 保持上游的 `${ENV:}` 占位符约定；新增 `application-secret.yaml`（已被 `.gitignore` 忽略）通过 `spring.config.import: optional:classpath:application-secret.yaml` 覆盖；提交 `application-secret.yaml.example` 作为模板。
- **代价**：无（本地开发体验不变，首次部署需复制 example 填 key）。
- **注意**：打出的 jar 内仍包含 `BOOT-INF/classes/application-secret.yaml`，**该 jar 不可外发**。

## 2. Agent 聊天供应商切换为百炼 / qwen3-max — 本仓库

- **动机**：本地无 siliconflow 配额；与知识库链路的 embedding / rerank 供应商（百炼 DashScope）对齐，统一账号与计费、统一熔断观测点。
- **改动**：`application.yaml` 中 `agent.chat.provider / model` 两行。

## 3. 部署实例的仓库链接改指本仓库 + 清理上游作者残留路径 — 本仓库

- **动机**：这是本仓库自己的部署实例，「打开 GitHub 仓库」按钮与星标数应当指向/反映**本仓库**，而不是上游；同时清掉上游作者遗留的本机绝对路径。
- **改动**：
  - `frontend/src/components/layout/Header.tsx`、`frontend/src/components/agent/AgentLayout.tsx`、`frontend/src/pages/admin/AdminLayout.tsx`：GitHub 入口由上游仓库改指本仓库；
  - `frontend/src/hooks/useGitHubStars.ts`：星标数据源由上游仓库改指本仓库（部署实例应展示自身数据）；
  - `frontend/TESTING.md`：删除上游作者本机绝对路径 `/Users/machen/workspace/nageoffer/ragent/frontend`，改为相对路径；
  - `README.md` 顶部声明补充本仓库地址，并注明下方徽章 / Star History / 贡献者名单均为**上游仓库**数据。
- **刻意不做的**：
  - **不改 `groupId` / 包名**（`com.nageoffer.ai`）。它是坐标不是链接，改动会制造数千文件级噪声 diff，且无实际收益；
  - **不删 README 里的上游徽章与 Star 曲线**。它们是上游的署名与项目背景，删除反而像在抹除来源；改为明确标注归属。
- **保留（合规要求）**：原 `LICENSE`、各文件头部版权声明、README 中的上游仓库链接。

## 4. 归档 9 篇项目学习笔记 — 本仓库

- **改动**：`docs/notes/` 下新增摄取链路、检索链路、MQ 排障等 9 篇学习笔记。

---

# 排障记录：三个假设，两个被实验证伪

> 下面的过程是这份清单里最值得看的部分。
> 「切分文档后卡在 `RUNNING`」这一个现象，先后导向三个假设：
> 前两个都在用实验验证后被**证伪并回滚**，第三个才是真正的根因。
> 保留被证伪的假设与证伪它的证据，因为它们定义了「怎么排除」而不是「猜到了什么」。

## 5. 假设一（已证伪）：topic / consumerGroup 的 `${unique-name:}` 后缀不一致 — 已回滚

- **提交**：`35c5e8fe`（改动）+ `7887828f`（回滚）
- **假设**：单实例部署下 Broker 注册的 topic/consumerGroup 不带 `${unique-name:}` 后缀，
  而 `@RocketMQMessageListener` 注解值带后缀，导致消费组订阅不上。
- **证伪**：启动日志显示**实际注册的 topic 与 consumerGroup 正是 `knowledge-document-chunk_topic` /
  `knowledge-document-chunk_cg`，本来就不带后缀** —— 因为 `unique-name` 属性未配置，
  `${unique-name:}` 展开为空串，注解值去掉后缀后**与原来完全等价**。也就是说该改动是 no-op。
- **结论**：假设不成立，已回滚。
- **教训**：`${prop:default}` 的默认值可以是空串，改一个恒为空的后缀不产生任何行为变化。
  验证一个改动是否有效，要先确认它真的改变了最终值。

## 6. 假设二（已证伪）：`@RocketMQTransactionListener` 自动注册失败 — 已回滚

- **提交**：`792ded87`（改动：事务消息 → 本地事务 + 普通同步消息）+ `7887828f`（回滚）
- **假设**：Spring Boot 4.1.0 + rocketmq-spring-boot-starter 2.3.6 组合下事务监听器自动注册失败，
  half 消息无人回查、永不提交，因此文档卡 `RUNNING`。
- **证伪**：
  1. 生产端日志明确打出 `事务消息发送结果: SEND_OK, 本地事务状态: COMMIT_MESSAGE` ——
     本地事务回调**确实被调用并返回了 COMMIT**，监听器是注册成功的；
     若监听器未注册，问题会暴露在**发送阶段**直接报错，而不是「消息静默不可见」。
  2. 上游在同一依赖版本组合下**至今仍在使用事务消息**。
- **结论**：假设不成立，已回滚，恢复上游的事务消息实现。
- **代价（若当时保留）**：会丧失 Broker 侧回滚与事务回查能力。

## 7. 缺陷 A（真实，属**上游**修复）：事务回查时消息体类型转换异常

- **提交**：上游 `b0f27491`（2026-09-13，作者 ding.ma）→ 本仓库 cherry-pick 为 `d40102d0`
- **缺陷**：`DelegatingTransactionListener.checkLocalTransaction` 中：
  ```java
  MessageWrapper<?> wrapper = (MessageWrapper<?>) message.getPayload();
  ```
  Broker 回查时 `getPayload()` 返回的是 **`byte[]`**，强转 `MessageWrapper` 抛 `ClassCastException`，
  被 `catch` 后返回 `UNKNOWN` → 半消息始终无法提交、最终被丢弃。
- **修复**：改为按 `checker.bodyType()` 构造参数化类型后反序列化：
  ```java
  JavaType messageType = objectMapper.getTypeFactory()
          .constructParametricType(MessageWrapper.class, checker.bodyType());
  MessageWrapper<T> wrapper = objectMapper.readValue(payload, messageType);
  ```
  并在 `TransactionChecker` 上新增 `Class<T> bodyType()`。
- **验证**：上游随修复附带了回归测试
  `framework/src/test/.../DelegatingTransactionListenerTest.java`，
  用真实序列化链路（`RocketMQMessageConverter` → `createRocketMqMessage` → `MessageExt`）构造 Broker 回查报文，
  断言反序列化后可正确取到类型化 body 并返回 `COMMIT` / `ROLLBACK`。
  本仓库运行该测试确认通过（见下方「验证记录」）。
- **归属**：**这是上游的修复**。本仓库的工作是同源同步、理解其触发路径、并运行回归测试确认。
  不得表述为「本仓库修复了该缺陷」。
- **重要澄清**：该缺陷只在 Broker **回查路径**（`CHECK_BACK`）触发。
  本机观察到的「文档卡 `RUNNING`」**不是**它造成的 —— 见缺陷 B。
  本机历史日志中从未出现过回查路径的痕迹（无 `回查` / `UNKNOWN` / `ClassCastException` 记录），
  说明本地部署的正常发送路径压根没走到回查，症状另有原因。

## 8. 缺陷 B（真实，属**本仓库**定位并修复）：Broker 侧 JDK cgroup v2 缺陷导致全量消息不可消费

- **提交**：`resources/docker/rocketmq-stack-5.2.0.compose.yaml`（本仓库）
- **现象**：生产端 `SEND_OK / COMMIT_MESSAGE`，但消费端 `Consume TPS = 0.00`、
  消费位点恒为 `0`、`Consume Diff` 持续堆积、文档永久 `RUNNING`。
- **分层排查路径**：
  1. `mqadmin topicStatus` → 消息**确实在** Broker 上（queue 2 max offset 与生产日志时间戳吻合）。
  2. `consumerOffset.json` → 位点全为 0，且 `dataVersion.timestamp` 停留在 9 月
     → **本次运行消费端一次都没有提交过**，即连拉取都没成功。
  3. Broker `remoting.log` → 收到大量 `code=11`（PULL_MESSAGE）请求，
     `queueOffset=0` 正是正确位点，但**每一条都被记为 ERROR**。
  4. 异常正文：
     ```
     java.lang.NoClassDefFoundError: Could not initialize class org.apache.rocketmq.store.StoreUtil
         at org.apache.rocketmq.store.DefaultMessageStore.estimateInMemByCommitOffset(DefaultMessageStore.java:1710)
         at org.apache.rocketmq.store.DefaultMessageStore.getMessage(DefaultMessageStore.java:866)
     ```
  5. 回溯首次初始化失败（`transaction.log`）：
     ```
     java.lang.ExceptionInInitializerError
       Caused by: java.lang.NullPointerException
         at jdk.internal.platform.cgroupv2.CgroupV2Subsystem.getInstance(CgroupV2Subsystem.java:81)
         at jdk.internal.platform.Container.metrics(Container.java:41)
         at sun.management.OperatingSystemImpl.<init>(OperatingSystemImpl.java:182)
         at java.lang.management.ManagementFactory.getOperatingSystemMXBean(ManagementFactory.java:374)
         at org.apache.rocketmq.store.StoreUtil.getTotalPhysicalMemorySize(StoreUtil.java:39)
         at org.apache.rocketmq.store.StoreUtil.<clinit>(StoreUtil.java:34)
     ```
- **根因**：RocketMQ 镜像内的 **JDK 8u372（Temurin）**在 Docker Desktop（Windows/WSL2，cgroup v2，
  `/proc/self/cgroup` 为 `0::/`、`/sys/fs/cgroup` 只读）下探测容器指标时抛 `NullPointerException`。
  这导致 `StoreUtil` 的**静态初始化块失败**，而 JVM 会把「初始化失败」的类**永久标记为不可用** ——
  之后每次访问都直接抛 `NoClassDefFoundError`，不再重试。
  而 `DefaultMessageStore.getMessage()` 要经 `estimateInMemByCommitOffset` 访问 `StoreUtil`，
  于是 **Broker 上所有消息读取全部失败**。
- **影响面（比想象中大）**：这不是某一个消费组的问题，而是**任何**消费者都拉不到消息；
  事务回查服务同样持续报错。本机 9 月 9 日之后写入的消息全部无法被消费。
  （分块日志显示 9 月 9 日 14:26 流程仍是 `success`，说明该隐患是当天晚些时候才出现的，
  与「导入后改了什么代码」无关。）
- **修复**：在 compose 的 `rmqbroker` 服务上关闭 JVM 容器感知：
  ```yaml
  JAVA_OPT_EXT: "-Xms512m -Xmx512m -XX:-UseContainerSupport"
  ```
  （`runbroker.sh` 会把 `JAVA_OPT_EXT` 追加进 `JAVA_OPT`，因此该变量是有效的注入点。）
  在容器内以同一 JDK 直接验证过该开关的有效性（见下方「验证记录」）。
- **注意**：该 `rmqbroker` 服务**没有配置任何数据卷**，`docker compose up -d rmqbroker`
  重建容器会**清空 store**（topic、消息、消费位点一并丢失），需重建 topic。
- **归属**：这是**本仓库**在本机环境定位并修复的 Broker 侧缺陷，
  不属于上游代码缺陷，也不应表述为「修复了项目业务代码」。

---

# 二次开发（本仓库）

## 9. 向量维度三源一致性启动校验（fail-fast）— 本仓库

- **新增**：`rag/src/main/java/com/nageoffer/ai/ragent/rag/config/VectorDimensionGuard.java`
  （db 侧配置 `VectorDimensionCheckProperties`，配置前缀 `rag.vector.dimension-check`）
- **动机**：向量维度同时被三处约束，任一处漂移都**不会在写入时立刻报错**，而是以
  「检索结果莫名其妙变差」这种**无异常**的方式表现出来，事后极难定位。既有的
  `ChunkEmbeddingService` 只在**入库时逐条**校验模型输出维度，属于「已经进到流水线里才发现」；
  维度冲突应当在**任何一次入库之前**就被拦下。
- **三源**：
  | 源 | 取值方式 | 漂移后果 |
  |---|---|---|
  | 配置 | `rag.default.dimension` | 向量化请求与落点判定都取自这里 |
  | 物理列 | `t_knowledge_vector.embedding` 的声明宽度 | 由建表语句决定，应用改不动，写入时列宽不符即失败 |
  | 模型实测输出 | 真实调用一次默认嵌入模型量出向量长度 | 配置写对不代表模型照做，部分提供商忽略请求里的 `dimensions` 参数 |
- **实现要点**：
  - 维度读取走 `format_type(a.atttypid, a.atttypmod)`：`information_schema` 对 `vector` 类型只给出
    `USER-DEFINED`，**看不到维度**；`atttypmod` 也无 `varchar`/`numeric` 那种 `+4` 偏移
    （实测 `vector(1536)` → `atttypmod` 就是 `1536`）。
  - 列类型若为**未声明宽度**的 `vector`（无 typmod），直接判失败：这种列会静默接受任意长度向量，
    维度漂移永远不会在写入时暴露。
  - 探测**显式指定模型**（`ai.embedding.default-model`），不走候选降级：降级到另一个模型会把
    「目标模型输出错维度」掩盖成「恰好能用」。
- **失败策略分两档**（这是本改动最需要解释的一个决定）：
  - **维度不一致 → 启动失败**。属于配置错误，必须挡在启动期。
  - **模型探测失败（网络 / 鉴权）→ 默认只告警放行**（`fail-on-probe-error: false`）。
    把一次远端不可达放大成整个应用起不来，是拿可用性换一致性，代价不对等；需要强一致时置 `true`。
- **代价**：启动多一次约 0.5s 的嵌入调用（`probe-model: false` 可关）。
- **验证**：单元测试 11 个用例；真实启动三态实测（见验证 4 / 5 / 6）。

## 10. 合并重复的 HNSW 向量索引 — 本仓库

- **改动**：`rag/src/main/java/com/nageoffer/ai/ragent/rag/core/vector/PgVectorStoreAdmin.java`
  + 清理脚本 `resources/database/upgrades/v1.1.0/261013_drop_duplicate_hnsw_index.sql`
- **缺陷**：库中同时存在两个**定义完全相同**的 HNSW 索引
  （`idx_kv_embedding` 由建表脚本建、`idx_kv_embedding_hnsw` 由本类建）。
  根因是 `ensureVectorSpace()` 的判存语句：
  ```java
  SELECT COUNT(*) FROM pg_indexes WHERE indexname = 'idx_kv_embedding_hnsw'
  ```
  只认**自己起的名字**，于是在「已经由 DDL 建过索引」的库上又建了一个。
- **触发路径**：`ensureVectorSpace()` 有四个调用点，其中 `KnowledgeBaseServiceImpl` 在**创建知识库**时无条件调用
  —— 这就是重复索引的实际产生时刻。（`VectorSpaceInitializer` 因 `vectorSpaceExists()` 对已有表恒为 true，
  反而走不到这一步，所以此前没人注意到。）
- **代价**：双份索引存储、双倍写入放大（每次 INSERT 维护两棵 HNSW 图）；查询优化器在两者间任选，
  排查索引相关问题时极易误判「改的索引没生效」。
- **修复**：
  - 判存改为按「**表 + 列 + 访问方法**」枚举等价索引（join `pg_index` / `pg_am` / `pg_attribute`，
    条件为 `amname='hnsw'`、`indnkeyatts=1`、`indisvalid`），名字不再参与判断：
    只要该列上已有任一 HNSW 索引就复用，不再重复建。
  - 新增 `@PostConstruct reportRedundantHnswIndexes()` 启动自检：存量库不会再去建知识库，
    等不到 `ensureVectorSpace()` 被调用，必须主动把冗余索引暴露出来并给出清理脚本路径。
  - 清理脚本按同一判据实时枚举，**保留按名排序的第一个**（通常即建表脚本的 `idx_kv_embedding`），
    删除其余等价索引；幂等，无重复时执行结果为空。
  - 顺手删除类内未被真正使用的 `dimension` 变量与 `RAGDefaultProperties` 依赖
    （原代码把它打进了「创建 HNSW 索引」的日志里，而 `CREATE INDEX ... USING hnsw` 根本不接受维度参数，
    这行日志会误导人以为索引是按该维度建的）。
- **归属**：**本仓库**发现并修复。

---

# 验证记录

## 验证 1：`-XX:-UseContainerSupport` 是否能绕过 cgroup v2 的 NPE

在 `rmqbroker` 容器内用镜像自带 JDK 8u372 运行探针程序
（`resources/docker/OsMxBeanProbe.java`，调用 `ManagementFactory.getOperatingSystemMXBean()`）：

| 启动参数 | 结果 |
|---|---|
| 默认 | `RESULT=FAIL java.lang.NullPointerException`，栈顶即 `CgroupV2Subsystem.getInstance` |
| `-XX:-UseContainerSupport` | `RESULT=OK totalPhysicalMemory=8181673984` |

结论：开关有效，采用。复现命令见该探针文件的类注释。

## 验证 2：`DelegatingTransactionListener` 回归测试

```bash
JAVA_HOME=<jdk17> ./mvnw -pl framework test -Dtest=DelegatingTransactionListenerTest
```

实测输出（2026-10-13 13:35）：

```
[INFO] Running com.nageoffer.ai.ragent.framework.mq.producer.DelegatingTransactionListenerTest
13:35:06.307 [main] INFO DelegatingTransactionListener -- [事务消息] 回查结果: topic=document-chunk, state=ROLLBACK
13:35:06.327 [main] INFO DelegatingTransactionListener -- [事务消息] 回查结果: topic=document-chunk, state=COMMIT
[INFO] Tests run: 2, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.816 s
[INFO] BUILD SUCCESS
```

两条用例分别覆盖回查后 `COMMIT` 与 `ROLLBACK` 两个分支，
说明 Broker 传来的 `byte[]` 能按业务类型正确反序列化为 `MessageWrapper<T>`。

## 验证 3：修复 broker 后的端到端回归

修复前：`Consume TPS = 0.00`，四个队列消费位点恒为 `0`，`Consume Diff Total = 11`，文档永久 `RUNNING`。

修复后，重新触发文档 `2097567527151439872`（英语四级作文.doc）分块，全链路实测：

| 时间 | 环节 |
|---|---|
| 13:32:30.165 | 生产者：`SEND_OK`，本地事务状态 `COMMIT_MESSAGE`，消息 ID `A9FEC8B2776037A71E9340B42D380000` |
| 13:32:30.259 | 消费者：开始消费（**距发送仅 94 ms**） |
| 13:32:31.311 | MinerU 文件上传成功 |
| 13:32:46.682 | 摄取-解析完成，blocks=65 |
| 13:32:47.370 | embedding 完成，模型 `text-embedding-v4`，9 条，673 ms |
| 13:32:47.797 | 向量批量写入 PostgreSQL，count=9 |
| 13:32:47.802 | 块索引写入完成，块数=9 |

数据库侧结果：

- `t_knowledge_document`：`status='success'`，`chunk_count=9`
- `t_knowledge_document_chunk_log`：`status='success'`，`chunk_count=9`，`total_duration=17512`（毫秒）
- `mqadmin consumerProgress`：该队列 `Broker Offset = Consumer Offset = 1`，**Diff = 0**

同一轮还复跑了此前卡死在 `running` 的历史文档 `2097567824481456128`（教资考试复习.docx），
同样恢复：13:34:46.592 发送 → 13:34:46.601 消费开始（**距发送 9 ms**）→ 13:35:42.693 完成，
`status` 由 `running` 变为 `success`。最终 `consumerProgress` 的 `Consume Diff Total = 0`。

结论：端到端链路恢复，`RUNNING → SUCCESS` 正常流转。

## 验证 4：向量维度校验单元测试

```bash
JAVA_HOME=<jdk17> ./mvnw -pl rag test -Dtest=VectorDimensionGuardTest
```

实测输出（2026-10-13 14:23）：

```
[INFO] Running com.nageoffer.ai.ragent.rag.config.VectorDimensionGuardTest
[INFO] Tests run: 11, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 1.116 s
[INFO] BUILD SUCCESS
```

11 个用例覆盖：三源一致放行 / 配置与列不符 / 模型输出与配置不符 / 列未声明宽度 /
表或列不存在 / 模型探测失败可放行且可切换为失败 / 开关关闭 / 未配默认模型 /
未配维度 / 关掉探测后不再发远端调用 / 模型返回空向量。

## 验证 5：真实启动的三源校验与冗余索引自检

正常配置启动（2026-10-13 14:25，清理索引**之前**）：

```
14:25:36.821 WARN  PgVectorStoreAdmin : 向量列上存在 2 个等价 HNSW 索引
                                       [idx_kv_embedding, idx_kv_embedding_hnsw]：查询结果不受影响，
                                       但会占用双份存储并放大写入开销。请执行
                                       resources/database/upgrades/v1.1.0/261013_drop_duplicate_hnsw_index.sql 清理
14:25:41.132 INFO  VectorDimensionGuard : 向量维度校验通过：配置=1536 / 列=1536 / 模型实测=1536
14:25:48.816 INFO  RagentApplication    : Started RagentApplication in 25.882 seconds
```

`模型实测=1536` 是真实调用 `text-embedding-v4` 量出来的结果（同批日志可见
`bailian embedding 完成 - 模型：text-embedding-v4，条数：1，耗时：475ms`），不是读配置得来的。

执行清理脚本：

```
NOTICE:  已保留 idx_kv_embedding，删除重复索引 {idx_kv_embedding_hnsw}
DO
```

清理后该表索引由 5 个降为 4 个，HNSW 索引只剩 1 个。再次启动（14:28）时
**冗余索引告警消失**，维度校验依旧通过：

```
14:28:02.812 INFO  VectorDimensionGuard : 向量维度校验通过：配置=1536 / 列=1536 / 模型实测=1536
14:28:09.903 INFO  RagentApplication    : Started RagentApplication in 26.295 seconds
```

## 验证 6：维度不一致时启动被拦下（fail-fast 实测）

只改配置、不动代码，用命令行参数把维度配成与物理列不符：

```bash
java -jar bootstrap/target/bootstrap-0.0.1-SNAPSHOT.jar --server.port=9090 --rag.default.dimension=1024
```

实测输出（2026-10-13 14:27，进程退出码 **1**）：

```
14:27:18.205 WARN  ConfigServletWebServerApplicationContext : Exception encountered during context initialization
                   - cancelling refresh attempt: BeanCreationException: Error creating bean with name
                   'vectorDimensionGuard': Invocation of init method failed
14:27:20.186 ERROR SpringApplication : Application run failed
Caused by: ServiceException{code='B000001',message='向量维度不一致：配置 rag.default.dimension=1024，
           但 t_knowledge_vector.embedding 的声明宽度为 1536 维。二者必须一致——
           请改配置对齐物理列，或按目标维度重建向量表（列宽无法在线修改）'}
```

对比：修复前这一错误要到**文档入库、向量写库**时才以
`expected 1536 dimensions, not 1024` 抛出（本机 9 月 9 日 14:22 的历史日志），
此时前面的解析、分块、向量化算力已经白花，且如果只写了一半就中断还会留下脏数据。
现在错误被提前到启动期，且报错同时给出**两个具体值**与两条可选处置路径。

## 验证 7：新代码下的入库链路回归

在清理索引后的实例上重新触发文档 `2097567527151439872` 分块（2026-10-13 14:28）：

| 时间 | 环节 |
|---|---|
| 14:28:46.997 | 生产者：`SEND_OK` / `COMMIT_MESSAGE` |
| 14:28:47.065 | 消费者：开始消费（**距发送 68 ms**） |
| 14:29:53.239 | 摄取-解析完成，blocks=65（MinerU 本轮较慢） |
| 14:29:53.863 | embedding 完成 9 条 / 619 ms |
| 14:29:54.192 | 向量批量写入 PostgreSQL count=9 |
| 14:29:54.195 | 块索引写入完成，块数=9 |

结果：`t_knowledge_document.status='success'`、`chunk_count=9`、
`t_knowledge_document_chunk_log` 新增一条 `success / 9 块 / total_duration=67107`；
`mqadmin consumerProgress` 的 `Consume Diff Total = 0`。
说明索引判存改造与新增的启动校验均未影响入库链路。

---

# 后续改造计划

| 项 | 状态 | 说明 |
|---|---|---|
| 向量维度三源一致性启动校验（配置 / pgvector 列 / 模型实测输出） | **已完成** | 见第 9 节；`VectorDimensionGuard`，不一致启动失败（`B000001`），模型不可达默认仅告警 |
| 合并重复 HNSW 索引 | **已完成** | 见第 10 节；判存改按「表 + 列 + 访问方法」，存量库由 `261013_drop_duplicate_hnsw_index.sql` 清理 |
| 清理 `PgVectorStoreAdmin` 未使用变量 | **已完成** | 随第 10 节一并删除 `dimension` 变量与 `RAGDefaultProperties` 依赖 |
| 检索评估集 + Recall@K / MRR 消融 | 计划中 | 在既有 `/rag/eval` 接口上补 gold set 与指标，锁定 `RetrievalBudget` 三段预算做控制变量 |

# 已知未跟进项

| 项 | 说明 |
|---|---|
| 维度校验对**知识库级**嵌入模型的覆盖 | 启动校验只探测部署级默认模型（`ai.embedding.default-model`）。知识库可在 L2 单独指定 `embedding_model`，这类覆盖由 `ChunkEmbeddingService` 在入库时逐条校验兜底，属于「运行期」而非「启动期」。若要一并前置，需要遍历所有知识库配置并在启动期逐个探测，代价是与知识库数量成正比的启动耗时 |
| `vector(1536)` 列宽的在线变更 | 目前只能重建表，校验失败时的提示语已说明这一点 |
