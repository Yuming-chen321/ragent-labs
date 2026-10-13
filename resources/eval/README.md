# 检索链路离线评测

对「向量 / 关键词 / 图谱 / 联网」多通道召回 + RRF 融合 + Rerank 精排做消融对比，回答一个问题：
**每一环到底带来了多少增益，还是只是增加了延迟**。

## 怎么测

测量点是 `GET /rag/eval?question=...`（`ragent.eval.enabled=true` 时开启）。它返回这条检索链路
的产出物——按相关度排序的 chunk id 与 doc 业务码，**不含答案生成**。用问答接口测不行：答案生成
是另一个模型、另一套超时与降级策略，检索的质量差异会被生成的噪声盖掉。

指标：

| 指标 | 定义 |
|---|---|
| Recall@K | 标注的参考 chunk 有没有出现在前 K 条里 |
| MRR@10 | 第一条命中参考 chunk 的名次倒数（未命中记 0） |
| 文档级 Recall@K | 把 chunk 归到文档后是否命中。chunk 切分参数变化时 chunk id 会变，这组更稳，两套都记 |

数据分两层，`goldset.jsonl` 由人工维护：

- `goldset.jsonl`：一条一行，只写 `qid / category / question / reference_chunks[{doc, index}]`
  与 `expect_keywords` / `chunk_keywords`。**刻意不写雪花 chunk id** —— 重新分块 id 就全变，
  写死等于把评测集锁死在一次切分结果上。
- `goldset.resolved.jsonl`：由 `resolve_goldset.py` 生成，把「文档码 + 段序号」解析成真实
  chunk id，并跑两级关键词断言。换环境只需重跑解析，标注本身不动。

## 三组对照

靠**重启换配置**实现，不改代码。三组的检索预算（`default-top-k` / `recall-budget` /
`rerank-candidate-limit`）必须完全相同，否则比的是预算不是链路。

| 组 | 配置 |
|---|---|
| 1 纯向量 | `rag.search.channels.vector.enabled=true`，其余通道 `false`；`rag.rerank.enabled=false`；`rag.search.evidence.min-rerank-score=0` |
| 2 向量 + 关键词 + RRF | 组的 1 基础上 `rag.keyword.type=es`、`rag.search.channels.keyword.enabled=true`；仍关 Rerank |
| 3 组 2 + Rerank | 组的 2 基础上 `rag.rerank.enabled=true`、`rag.search.evidence.min-rerank-score=0.2` |

注意闸门：`evidence.min-rerank-score` 依赖精排分，关掉 Rerank 时必须同时把闸门置 0，
否则整批会被闸门丢弃，测出来的是「闸门效果」而不是「检索能力」。

## 怎么跑

### 1. 准备语料

用项目自带的初始化器把环境重置成模板状态（2 个知识库 / 10 份文档 / 67 个 chunk）。

> Windows 检出注意：`checksums.sha256` 是按 LF 内容算的，`core.autocrlf=true` 会把模板文件
> 转成 CRLF 导致预检必然失败。已在仓库根 `.gitattributes` 里对模板文件锁死 `eol=lf`，
> 若仍报 checksum 不一致，先确认这几个属性生效（`git check-attr text -- <文件>`）。

### 2. 导出分块并解析标注

```bash
cd <项目根>
docker exec postgres psql -U postgres -d ragent -t -A -c "
  select json_agg(json_build_object(
    'chunkId', c.id, 'docCode', regexp_replace(d.doc_name, '\.[^.]*$', ''),
    'chunkIndex', c.chunk_index, 'content', c.content)
    order by d.doc_name, c.chunk_index)
  from t_knowledge_chunk c join t_knowledge_document d on d.id = c.doc_id;" \
  | python -c "import sys,json;[print(json.dumps(x,ensure_ascii=False)) for x in json.load(sys.stdin)]" \
  > resources/eval/chunks_export.jsonl

python resources/eval/resolve_goldset.py      # 解析并校验标注，必须通过才继续
```

### 3. 按组重启并评测

三组只差命令行参数，不改代码、不用重新打包：

```bash
# 组 1 纯向量（- Rerank）
java -jar bootstrap/target/bootstrap-0.0.1-SNAPSHOT.jar --server.port=9090 \
  --rag.search.channels.keyword.enabled=false \
  --rag.rerank.enabled=false --rag.search.evidence.min-rerank-score=0
python resources/eval/run_eval.py --label vec-only --out resources/eval/results/vec-only.json

# 组 2 向量 + 关键词 + RRF（- Rerank）
java -jar bootstrap/target/bootstrap-0.0.1-SNAPSHOT.jar --server.port=9090 \
  --rag.search.channels.keyword.enabled=true \
  --rag.rerank.enabled=false --rag.search.evidence.min-rerank-score=0
python resources/eval/run_eval.py --label vec-kw-rrf --out resources/eval/results/vec-kw-rrf.json

# 组 3 组 2 + Rerank
java -jar bootstrap/target/bootstrap-0.0.1-SNAPSHOT.jar --server.port=9090 \
  --rag.search.channels.keyword.enabled=true \
  --rag.rerank.enabled=true --rag.search.evidence.min-rerank-score=0.2
python resources/eval/run_eval.py --label vec-kw-rrf-rr --out resources/eval/results/vec-kw-rrf-rr.json

# 对比
python resources/eval/run_eval.py --compare resources/eval/results/*.json
```

组 2/3 的前置：`rag.keyword.type=es`（关键词通道的 bean 条件），ES 见
`resources/docker/elasticsearch-stack-9.4.2.compose.yaml`。

每次运行都会把**当前生效的检索配置**读回来记进结果文件（通道开关、融合策略、
`defaultTopK` / `recallBudget` / `rerankCandidateLimit` / `rrfK`、精排与闸门开关），
用来证明三组之间这些值一致——控制变量靠证据而不是靠自觉。


## 实测结果

环境：语料 2 个知识库 / 10 份文档 / 67 个 chunk；gold set 32 条（条款单跳 23 / 数值表格 6 /
多跳实体 3）；ES 9.4.2 单节点（内置 standard 分词器）；Rerank 走百炼 `qwen3-rerank`。

控制变量三组一致：`defaultTopK=10`、`recallBudget=20`、`rerankCandidateLimit=40`、`rrfK=20`、
`fusion.strategy=rrf` —— 当时实际生效的值记在结果文件的 `runtime_config` 字段里。

| 组 | MRR@10 | Recall@5 | Recall@10 | 条款单跳 MRR | 数值表格 MRR | 多跳实体 MRR |
|---|---|---|---|---|---|---|
| 1 纯向量 | 0.8776 | 1.0000 | 1.0000 | 0.8732 | 0.9167 | 0.8333 |
| 2 + 关键词 + RRF | 0.8672 | 1.0000 | 1.0000 | 0.8804 | 0.9167 | 0.6667 |
| 3 + Rerank | **0.9531** | 1.0000 | 1.0000 | 0.9565 | 1.0000 | 0.8333 |

结论（**只在这份语料与这套配置下成立**）：

1. **召回在这份语料上已经触顶**。三组 Recall@5 / Recall@10 全部 1.0，`Recall` 这个指标区分
   不出任何链路差异；能看出差别的只有**排序质量（MRR）**。
2. **加关键词通道 + RRF 融合，MRR 反而略降（−0.0104）**。32 题里 6 题名次变化、有升有降。
   典型反例 Q024（工资核对申诉）：纯向量命中 @1，加关键词后掉到 @4 —— 关键词通道把
   「人事制度#6 / 公司规章制度#2」等含相同字样但不相干的段拉了进来，融合时稀释了头部排序。
   原因大概率是语料规模小（67 chunk）+ 中文条款用词与提问高度重合，向量语义检索本身已足够强，
   BM25 的边际信息量不足。
3. **Rerank 是排序质量的主杠杆**。相对组 1 提升 MRR +0.0755，相对组 2 提升 +0.0859，
   并把组 2 被关键词通道拉低的多跳实体 MRR 从 0.6667 恢复到 0.8333。它不改变召回
   （三组 Recall 都是 1.0），只重排 —— 这正是精排该做的事。
4. Rerank 不是万能的。Q029（权限申请入口）是唯一在组 3 变差的题（@1 → @2），精排把
   「招聘信息#5」排到了正确段之前。样本量 32 条，单题波动不足以推翻趋势，但也不该被藏起来。

三组各跑了一轮，另外组 1 / 组 2 各重跑一轮做确定性校验，**逐题命中名次完全一致**
（首轮原始件见 `results/_first_pass/`）。

> **天花板效应必须主动说明**：组 1 已经 Recall@5/10 全 1.0，说明这份 gold set 对「召回」
> 这一维度已失去区分度（问题与出处基本一对一）。所以上面的结论只谈 MRR，**不宣称
> 「Recall 提升」**。要测召回差异，需要更难的语料（同义改写、多跳、干扰项密集）。

## 链路降级自检（必须做）

评测依赖外部模型（embedding / rerank / 意图模型）。这些服务不可用时应用会**静默降级**：

- embedding 失败 → 向量通道返回 0 条，融合池里只剩关键词；
- rerank 失败 → 证据闸门「无精排分可读」空转放行，等于没精排。

**聚合指标本身看不出降级**，只会安静地变低。这不是假设，本仓库实测踩到过：

| 采集 | 链路状态 | MRR@10 |
|---|---|---|
| 首轮（健康） | `Arrearage` 0 次；融合归因「向量=20 关键词=20」 | 0.9531 |
| 重跑（上游欠费） | `Arrearage` 22 次；融合归因 0/32 行含「向量=」 | 0.5907 |

同一条链路、同一份 gold set、同一台机器，仅因上游账号欠费（HTTP 400 `code: Arrearage`），
指标从 0.9531 掉到 0.5907。若把这份降级结果当「另一组配置」去对比，会得出一套完全错误的结论。

因此每次评测都带上应用日志，让脚本自己找证据：

```bash
python resources/eval/run_eval.py --label vec-kw-rrf-rr \
  --out resources/eval/results/vec-kw-rrf-rr.json \
  --app-log scripts/logs/eval-g3.log
```

脚本扫描日志并把结论写进结果文件的 `pipeline_health`：上游拒绝次数、embedding / rerank
失败次数、闸门空转次数，以及「融合归因行里有没有出现过『向量=』」。任一命中即判 `degraded`，
打印显著告警并以**退出码 3** 结束 —— 自动化流程也能拦住它。

已有结果若事后才发现问题（改了标注、想补日志证据），用事后审计模式，不必重跑：

```bash
python resources/eval/run_eval.py --reaudit resources/eval/results/vec-kw-rrf-rr.json \
  --out resources/eval/results/vec-kw-rrf-rr.json --app-log scripts/logs/eval-g3.log
```

`--reaudit` 用当前 gold set 重算指标（沿用原检索结果 —— 标注不影响检索，这是它成立的前提），
并且**只要有一条 query 的命中名次发生变化就报告出来**，那种情况下必须重跑。本模式不提供
「悄悄改数」的通道。`results/vec-kw-rrf-rr-degraded-arrearage.json` 是那次降级的原始记录，留作反例。

## 标注质量：两级关键词断言

标注是人写的，人会写错，而**标注错会让结论整个反向，从聚合指标上完全看不出来**。
所以 `resolve_goldset.py` 在解析时跑两级断言，不通过就直接失败退出：

| 级别 | 字段 | 语义 |
|---|---|---|
| 整串级 | `expect_keywords` | 关键词出现在「所有参考段拼接后」即可。多跳题够用 |
| 逐段级 | `chunk_keywords`（键为 `文档#段号`） | 关键词必须出现在**指定那一段**里 |

只写整串级会漏检。实测踩到：Q032（离职流程，多跳）的参考里原本混进了一段「请假流程」和一段
「权限申请入口」，因为同批参考里有真正讲离职的段（IT资产管理#3），整串断言稀里糊涂就过了 ——
3 段参考里实际只有 1 段是对的。补上逐段断言后，故意把断言改错会被立刻拦下：

```
标注校验未通过，共 1 处问题：
  - Q032 逐段断言失败：IT支持#5 里找不到 '不存在词'
退出码=1
```

修正后 Q032 的参考改为 `IT资产管理#3`（资产归还）+ `IT支持#5`（离职/转岗权限回收），
参考 chunk 总数 38 → 37。**修正前后三组指标完全不变**（`--reaudit` 重算，逐题命中名次无变化），
说明这是纯粹的标注质量改进，不影响任何结论。

脚本还会对「某段一个 `expect_keywords` 都不含」的情况打**告警但不失败**（多跳题的补充段常如此，
例如 Q031 的「Java 工程师场景」「培训体系」两段都不含「电脑」），每次解析都打出来逼人扫一眼，
好过悄悄放过真正标错的段。

## 已知边界（不要超出这些结论）

- **不是四路**。图谱通道依赖 LightRAG（需另起服务）、联网搜索依赖外部搜索 API key，本地均未接入。
  实际可测的是**向量 + 关键词两路**。说「四路并行」是不准确的。
- **样本量是趋势性结论**，不是统计显著性结论。要主动说明并按类别看分布，而不是只报一个总均值。
- **分词器是弱化版**。未安装 IK 插件，ES 用内置 `standard`（中文按单字切分）。装了 IK 后关键词
  通道的召回与精度都会变，这里的数字不能外推到 IK 环境。
- 语料是企业制度类文档（条款、SOP、FAQ），一对一的「问题 → 出处」关系较明确；换成多跳或表格
  数值型语料，通道贡献的排序可能不同。
- **天花板效应**：见「实测结果」——组 1 已经 Recall@5/10 全 1.0，这份 gold set 对召回维度没有
  区分度。结论只覆盖排序质量（MRR），不要外推成「召回提升」。
- **单次采集**：三组各跑一轮（组 1 / 组 2 另做一轮重跑做确定性校验，逐题命中名次完全一致）。
  没有多轮取均值的置信区间，所以报告的是「名次」这种离散量，而不是浮点分的微小差异。
