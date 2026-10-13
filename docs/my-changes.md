# 我的改动清单（相对上游 nageoffer/ragent）

> 查看命令：`git log upstream/main..main --oneline`（先 `git remote add upstream https://github.com/nageoffer/ragent.git && git fetch upstream`）
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

- **现象**：同上，文档卡在 `RUNNING`。
- **根因**：Spring Boot 4.1.0 + rocketmq-spring-boot-starter 2.3.6 下 `@RocketMQTransactionListener` 自动注册失败，half 消息无人回查、永远不提交。
- **改动**：`KnowledgeBaseServiceImpl` / `KnowledgeDocumentServiceImpl` 中，把「事务消息 + 本地事务回调」改为「本地事务先落库 + 普通同步消息通知消费者」。
- **代价（明确接受）**：丧失 Broker 侧回滚与事务回查；若落库后发送失败，清理任务需人工或定时任务补做。删除知识库场景发送失败会记录 error 日志（DB 已软删，资源清理可补偿）。
- **后续计划**：恢复事务消息 —— 绕过 starter 自动配置，手工构造 `DefaultMQProducer` 并显式注册 `TransactionListener`，保留回查能力。

---

## 后续改造计划

| 项 | 状态 | 说明 |
|---|---|---|
| 向量维度三源一致性启动校验（配置 / pgvector 列 / 模型实测输出） | 计划中 | 维度漂移 1536 → 1024 曾导致嵌入阶段失败，现把校验提前到启动期 fail-fast |
| 检索评估集 + Recall@K / MRR 消融 | 计划中 | 在既有 `/rag/eval` 接口上补 gold set 与指标 |
| 恢复事务消息（手工注册 TransactionListener） | 计划中 | 见上文第 4 条 |
