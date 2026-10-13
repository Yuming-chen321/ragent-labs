# 我的改动清单（相对上游 nageoffer/ragent）

> 查看命令：`git log upstream-snapshot..main --oneline` 与 `git diff --stat upstream-snapshot..main`
>
> `upstream-snapshot` 标签打在导入时的上游快照上（`08c77d0`），对应上游 commit `62a81a58c3b8b44ad1501ef70d9b70f2b9480ef4`。
> 上游在此之后已继续演进（>70 个提交），因此**不要**用 `upstream/main` 作为对比基准，否则会把上游的新工作算进本次改动。
>
> 上游基线：`62a81a58c3b8b44ad1501ef70d9b70f2b9480ef4`（Apache-2.0，版权与 LICENSE 保留）

每条记录按「动机 → 改动 → 代价 → 后续」组织。

---

## 1. 密钥外置化（安全）

- **动机**：本地部署需要真实 API Key（百炼 / MinerU），但明文 key 不能进入版本库。
- **改动**：`application.yaml` 保持上游的 `${ENV:}` 占位符约定；新增 `application-secret.yaml`（已被 `.gitignore` 忽略）通过 `spring.config.import: optional:classpath:application-secret.yaml` 覆盖；提交 `application-secret.yaml.example` 作为模板。
- **代价**：无（本地开发体验不变，首次部署需复制 example 填 key）。

## 2. Agent 聊天供应商切换为百炼 / qwen3-max

- **动机**：本地无 siliconflow 配额；与知识库链路的 embedding / rerank 供应商（百炼 DashScope）对齐，统一账号与计费、统一熔断观测点。
- **改动**：`application.yaml` 中 `agent.chat.provider / model` 两行。

## 3. MQ topic 与消费组去掉 `${unique-name:}` 后缀

- **现象**：点击「切分」后文档永久卡在 `RUNNING`。
- **定位**：查 commitlog 确认消息已落盘；查 Broker 端消费者连接记录与 `PullMessageThread` 触发情况，确认消费端从未拉取 —— 消息进得来、没人消费。
- **原因**：单实例部署下 Broker 实际注册的 topic / consumerGroup 不带 `${unique-name:}` 后缀，`@RocketMQMessageListener` 注解值与 Broker 不一致，消费组订阅不到。
- **改动**：`KnowledgeDocumentChunkConsumer.java` 注解中 topic / consumerGroup 去掉后缀。
- **教训**：生产者侧命名与消费者侧注解必须同源，否则错误表现为「静默不消费」而非显式报错。

## 4. 事务消息替换为本地事务 + 同步消息（中间方案）

> **2026-10-13 复查修正**：下面「自动注册失败」这一根因判断**证据不足，需重新验证**。
> 复查发现：上游在同一依赖版本组合下**至今仍在使用事务消息**，并在 `b0f27491`（2026-09-13，
> 即我导入快照后 4 天）修掉了 `DelegatingTransactionListener.checkLocalTransaction` 中的一处缺陷 ——
> 回查时把 Broker 传来的 `byte[]` 直接强转为 `MessageWrapper<?>`，抛 `ClassCastException`，
> 被 catch 后返回 `UNKNOWN`，导致**半消息始终不被提交**。
> 这与我观察到的现象（消息在 Broker、消费端从未拉取、文档永久 `RUNNING`）高度吻合，
> 而「监听器未注册」通常会在**发送阶段**直接报错，而非表现为消息静默不可见。
> 待验证动作：cherry-pick `b0f27491` → 恢复事务消息 → 回归验证回查链路。
> 结论以验证结果为准，未验证前不得写成定论。

- **现象**：同上，文档卡在 `RUNNING`。
- **当时的根因判断（待修正）**：Spring Boot 4.1.0 + rocketmq-spring-boot-starter 2.3.6 下 `@RocketMQTransactionListener` 自动注册失败，half 消息无人回查、永远不提交。
- **改动**：`KnowledgeBaseServiceImpl` / `KnowledgeDocumentServiceImpl` 中，把「事务消息 + 本地事务回调」改为「本地事务先落库 + 普通同步消息通知消费者」。
- **代价（明确接受）**：丧失 Broker 侧回滚与事务回查；若落库后发送失败，清理任务需人工或定时任务补做。删除知识库场景发送失败会记录 error 日志（DB 已软删，资源清理可补偿）。
- **后续计划**：恢复事务消息。**优先方案已变更** —— 不再是「手工注册 `TransactionListener`」，而是先同步上游 `b0f27491` 的修复并回归验证；仅当验证仍失败时，才考虑绕过 starter 自动配置手工注册。

## 5. 仓库链接指向本仓库 + 清理上游作者残留路径

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

---

## 后续改造计划

| 项 | 状态 | 说明 |
|---|---|---|
| 向量维度三源一致性启动校验（配置 / pgvector 列 / 模型实测输出） | 计划中 | 维度漂移 1536 → 1024 曾导致嵌入阶段失败，现把校验提前到启动期 fail-fast |
| 检索评估集 + Recall@K / MRR 消融 | 计划中 | 在既有 `/rag/eval` 接口上补 gold set 与指标 |
| 恢复事务消息（手工注册 TransactionListener） | 计划中 | 见上文第 4 条 |
