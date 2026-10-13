#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RAG 检索链路离线评测 harness

读一份 gold set（query + 人工标注的参考 chunk），逐条调 `/rag/eval`，按当前应用配置
统计 Recall@K 与 MRR@10，并把原始响应一并落盘。

为什么用 `/rag/eval` 而不是 `/rag/v3/chat`：
  后者会把答案生成（另一个模型、另一套超时与降级）引入测量，检索质量被生成噪声掩盖。
  `/rag/eval` 直接返回这条链路的产出物：检索到的 chunk id 列表（按相关度排序）。

三组对照靠**重启应用换配置**实现，本脚本不改配置、只测当前状态：
  组 1  纯向量、无 Rerank       rag.search.channels.vector.enabled=true  其余通道 false
                                rag.rerank.enabled=false
                                rag.search.evidence.min-rerank-score=0（闸门依赖精排分，关精排必须同时关闸门）
  组 2  向量 + 关键词 + RRF      rag.keyword.type=es、rag.search.channels.keyword.enabled=true
                                仍关 Rerank，闸门仍为 0
  组 3  组 2 + Rerank           rag.rerank.enabled=true、min-rerank-score=0.2

控制变量：三组下 `rag.search.default-top-k` / `recall-budget` / `rerank-candidate-limit`
必须保持一致，否则比的是预算不是链路。脚本会把当前生效的预算读回来记录进结果文件。

用法：
  python run_eval.py --label vec-only --out results/vec-only.json
  python run_eval.py --compare results/*.json
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:9090/api/ragent"


def http_json(url, method="GET", body=None, token=None, timeout=180):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def login(base_url, username, password):
    resp = http_json(base_url + "/auth/login", "POST",
                     {"username": username, "password": password}, timeout=30)
    if not resp.get("success"):
        raise SystemExit("登录失败：%s" % resp.get("message"))
    return resp["data"]["token"]


def load_goldset(path):
    items = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit("%s 第 %d 行不是合法 JSON：%s" % (path, lineno, exc))
            for field in ("qid", "question", "reference_chunk_ids"):
                if field not in item:
                    raise SystemExit("%s 第 %d 行缺字段 %s" % (path, lineno, field))
            items.append(item)
    if not items:
        raise SystemExit("gold set 为空：%s" % path)
    return items


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# 评测依赖外部模型（embedding / rerank / 意图模型）。这些服务一旦不可用，应用会**静默降级**：
# embedding 失败 → 向量通道返回 0 条；rerank 失败 → 证据闸门「无精排分可读」空转放行。
# 降级后指标会大幅下滑，但聚合数字本身看不出任何异常 —— 唯一的证据落在应用日志里。
# 所以把日志扫描列为评测的强制自检：不传 --app-log 时跳过，并明确记 null，而不是假装没问题。
DEGRADE_PATTERNS = {
    "上游拒绝(欠费/限额)": r"Arrearage|overdue-payment",
    "embedding 调用失败": r"embedding 请求失败",
    "rerank 调用失败": r"Rerank model failed",
    "证据闸门空转(无精排分)": r"闸门空转",
}
FUSION_ATTR_RE = re.compile(r"融合池按通道:([^\r\n]+)")


def scan_app_log(path):
    """扫应用日志找链路降级证据；未提供日志时返回 None（明确记为"未检查"）"""
    if not path:
        return None
    if not os.path.exists(path):
        return {"log": os.path.abspath(path), "_error": "日志文件不存在"}

    raw = open(path, "rb").read()
    text = raw.decode("gbk", errors="replace")  # Windows 控制台重定向是 GBK

    hits = {name: len(re.findall(pat, text)) for name, pat in DEGRADE_PATTERNS.items()}
    fusion_lines = FUSION_ATTR_RE.findall(text)
    vec_lines = sum(1 for line in fusion_lines if "向量=" in line)
    # 融合归因行全部不出现「向量=」→ 向量通道一次都没贡献，基本可判定 embedding 挂了
    vector_silent = bool(fusion_lines) and vec_lines == 0

    return {
        "log": os.path.abspath(path),
        "degrade_hits": hits,
        "fusion_attribution_lines": len(fusion_lines),
        "fusion_lines_with_vector": vec_lines,
        "vector_channel_silent": vector_silent,
        "degraded": any(hits.values()) or vector_silent,
    }


def rank_of_first_hit(retrieved, references):
    """返回第一个命中的参考 chunk 的 1-based 名次；未命中返回 None"""
    refs = set(references)
    for idx, chunk_id in enumerate(retrieved, 1):
        if chunk_id in refs:
            return idx
    return None


def evaluate_one(retrieved_chunk_ids, retrieved_doc_codes, item, ks):
    refs = item["reference_chunk_ids"]
    ref_docs = set(item.get("reference_doc_codes") or [])

    row = {
        "qid": item["qid"],
        "category": item.get("category"),
        "question": item["question"],
        "reference_chunk_ids": refs,
        "reference_doc_codes": sorted(ref_docs),
        "hit_rank": rank_of_first_hit(retrieved_chunk_ids, refs),
        "retrieved_chunk_ids": retrieved_chunk_ids,
        "retrieved_doc_codes": retrieved_doc_codes,
    }
    row["mrr"] = 0.0 if row["hit_rank"] is None else 1.0 / row["hit_rank"]
    for k in ks:
        row["recall@%d" % k] = 1.0 if row["hit_rank"] is not None and row["hit_rank"] <= k else 0.0
        # 文档级：chunk 切分方式改变时 chunk id 会变，文档级命中更稳，两套都记
        row["doc_recall@%d" % k] = 1.0 if ref_docs and (ref_docs & set(retrieved_doc_codes[:k])) else 0.0
    return row


def summarize(rows, ks):
    n = len(rows)
    out = {"count": n, "mrr@10": sum(r["mrr"] for r in rows) / n}
    for k in ks:
        out["recall@%d" % k] = sum(r["recall@%d" % k] for r in rows) / n
        out["doc_recall@%d" % k] = sum(r["doc_recall@%d" % k] for r in rows) / n
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r["category"] or "未分类", []).append(r)
    out["by_category"] = {
        cat: {"count": len(items),
              "mrr@10": sum(i["mrr"] for i in items) / len(items),
              "recall@10": sum(i["recall@10"] for i in items) / len(items)}
        for cat, items in sorted(by_cat.items())
    }
    return out


def read_runtime_config(base_url, token):
    """把当前生效的检索配置读回来，作为「三组控制变量一致」的证据落盘

    只保留与消融相关的项：通道开关、融合策略、三段预算、精排开关、闸门。
    整份 settings 太大（含所有模型候选与 provider 配置），会被无关内容淹没。
    """
    try:
        resp = http_json(base_url + "/rag/settings", token=token, timeout=30)
        if not resp.get("success"):
            return {"_error": resp.get("message")}
        data = resp.get("data") or {}
    except Exception as exc:  # 读不到不阻塞评测，但要留痕
        return {"_error": str(exc)}

    wanted_suffixes = ("channels.vector.enabled", "channels.keyword.enabled",
                       "channels.graph.enabled", "channels.webSearch.enabled",
                       "channels.vector.weight", "channels.keyword.weight",
                       "fusion.strategy", "fusion.rrfK", "fusion.rerankCandidateLimit",
                       "defaultTopK", "recallBudget", "features.rerank")
    picked = {}

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                child = "%s.%s" % (path, key) if path else key
                if isinstance(value, (dict, list)):
                    walk(value, child)
                elif any(child.endswith(s) for s in wanted_suffixes):
                    picked[child] = value
        elif isinstance(node, list):
            for item in node:
                walk(item, path)

    walk(data)
    return dict(sorted(picked.items()))


def run(args):
    goldset = load_goldset(args.goldset)
    token = login(args.base_url, args.username, args.password)
    ks = sorted(set(args.k))

    runtime_config = read_runtime_config(args.base_url, token)
    print("gold set: %d 条，配置标签：%s" % (len(goldset), args.label))
    print("运行时检索配置：%s" % json.dumps(runtime_config, ensure_ascii=False))

    rows, failures = [], []
    for idx, item in enumerate(goldset, 1):
        url = args.base_url + "/rag/eval?" + urllib.parse.urlencode({"question": item["question"]})
        started = time.time()
        try:
            resp = http_json(url, token=token, timeout=args.timeout)
        except urllib.error.HTTPError as exc:
            failures.append({"qid": item["qid"], "error": "HTTP %s" % exc.code})
            print("  [%d/%d] %s HTTP %s" % (idx, len(goldset), item["qid"], exc.code))
            continue
        except Exception as exc:
            failures.append({"qid": item["qid"], "error": str(exc)})
            print("  [%d/%d] %s 失败：%s" % (idx, len(goldset), item["qid"], exc))
            continue

        if not resp.get("success"):
            failures.append({"qid": item["qid"], "error": resp.get("message")})
            print("  [%d/%d] %s 业务失败：%s" % (idx, len(goldset), item["qid"], resp.get("message")))
            continue

        data = resp["data"]
        row = evaluate_one(data.get("retrievedChunkIds") or [],
                           data.get("retrievedContextDocIds") or [],
                           item, ks)
        row["latency_ms"] = data.get("latencyMs")
        row["wall_ms"] = int((time.time() - started) * 1000)
        row["sub_intents"] = data.get("subIntents")
        rows.append(row)
        mark = "命中@%s" % row["hit_rank"] if row["hit_rank"] else "未命中"
        print("  [%d/%d] %s %s（召回 %d 条，%dms）"
              % (idx, len(goldset), item["qid"], mark, len(row["retrieved_chunk_ids"]), row["wall_ms"]))
        if args.sleep:
            time.sleep(args.sleep)

    result = {
        "label": args.label,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "goldset": os.path.abspath(args.goldset),
        # 记哈希：gold set 改过标注后，旧结果文件能一眼看出是不是同一份标注跑出来的
        "goldset_sha256": sha256_file(args.goldset),
        "base_url": args.base_url,
        "k": ks,
        "runtime_config": runtime_config,
        # 链路健康自检：见 scan_app_log 的说明。没有日志就是 null，不掩盖"没检查"这件事。
        "pipeline_health": scan_app_log(args.app_log),
        "summary": summarize(rows, ks) if rows else None,
        "failures": failures,
        "details": rows,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    if result["summary"]:
        s = result["summary"]
        print("\n=== %s ===" % args.label)
        print("样本 %d 条，失败 %d 条" % (s["count"], len(failures)))
        print("MRR@10 = %.4f" % s["mrr@10"])
        for k in ks:
            print("Recall@%-2d = %.4f   文档级 Recall@%-2d = %.4f"
                  % (k, s["recall@%d" % k], k, s["doc_recall@%d" % k]))
        for cat, m in s["by_category"].items():
            print("  %-8s n=%-3d MRR@10=%.4f Recall@10=%.4f"
                  % (cat, m["count"], m["mrr@10"], m["recall@10"]))
    health = result["pipeline_health"]
    exit_code = 0 if not failures else 2
    if health and health.get("degraded"):
        print("\n" + "!" * 74)
        print("!! 链路降级告警：本次评测的检索链路不完整，以上指标不可直接采信")
        print("!! 证据来自应用日志：%s" % health.get("log"))
        for name, count in (health.get("degrade_hits") or {}).items():
            if count:
                print("!!   - %s ×%d" % (name, count))
        if health.get("vector_channel_silent"):
            print("!!   - 融合归因里从未出现「向量=」：向量通道整场未贡献，embedding 疑似不可用")
        print("!! 处置：恢复上游模型可用性后重跑；切勿把降级结果与正常结果并排对比")
        print("!" * 74)
        exit_code = 3
    print("\n结果已写入 %s" % args.out)
    return exit_code


def reaudit(args):
    """对已有结果做事后审计：用当前 gold set 重算指标 + 补链路健康证据。

    适用场景：结果已经采集，但之后 (a) 修正了 gold set 的标注，或 (b) 想补上日志健康证据。
    前提是检索结果不受影响 —— 标注与日志都不改变检索本身，所以沿用原文件里的
    retrieved_chunk_ids。若重算后**任何一条** query 的命中名次发生变化，说明改动确实
    影响了判定，此时必须重跑评测，本模式不提供"悄悄改数"的通道。
    """
    items = {i["qid"]: i for i in load_goldset(args.goldset)}
    res = json.load(open(args.reaudit, encoding="utf-8"))
    ks = args.k
    old_mrr = (res.get("summary") or {}).get("mrr@10")
    changed, rows = [], []

    for row in res["details"]:
        item = items.get(row["qid"])
        if item is None:
            raise SystemExit("gold set 里找不到 %s" % row["qid"])
        refs = item["reference_chunk_ids"]
        ref_docs = set(item.get("reference_doc_codes") or [])
        hit_rank = rank_of_first_hit(row["retrieved_chunk_ids"], refs)
        if hit_rank != row.get("hit_rank"):
            changed.append("%s: %s → %s" % (row["qid"], row.get("hit_rank"), hit_rank))

        row = dict(row)
        row["reference_chunk_ids"] = refs
        row["reference_doc_codes"] = sorted(ref_docs)
        row["hit_rank"] = hit_rank
        row["mrr"] = 0.0 if hit_rank is None else 1.0 / hit_rank
        for k in ks:
            row["recall@%d" % k] = 1.0 if hit_rank and hit_rank <= k else 0.0
            row["doc_recall@%d" % k] = 1.0 if ref_docs and (
                ref_docs & set((row.get("retrieved_doc_codes") or [])[:k])) else 0.0
        rows.append(row)

    res["details"] = rows
    res["summary"] = summarize(rows, ks)
    res["goldset"] = os.path.abspath(args.goldset)
    res["goldset_sha256"] = sha256_file(args.goldset)
    res["reaudited_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    res["reaudit_note"] = ("以修正后的 gold set 重算指标（检索结果沿用原文件，标注不改变检索）；"
                           "链路健康证据由应用日志事后核查补入")
    if args.app_log:
        res["pipeline_health"] = scan_app_log(args.app_log)

    print("MRR@10：原 %.4f → 重算 %.4f" % (old_mrr, res["summary"]["mrr@10"]))
    if changed:
        print("\n!! 有 %d 条 query 命中名次发生变化 —— 标注改动影响了判定，必须重跑评测：" % len(changed))
        for line in changed:
            print("   - %s" % line)
    else:
        print("逐题命中名次无变化：标注修正未影响任何指标，可安全采用")
    health = res.get("pipeline_health")
    if health:
        print("链路健康：%s（向量贡献行 %d/%d）"
              % ("降级" if health.get("degraded") else "健康",
                 health.get("fusion_lines_with_vector", 0),
                 health.get("fusion_attribution_lines", 0)))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2)
    print("已写出 %s" % args.out)
    return 4 if changed else 0


def compare(paths):
    results = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            results.append(json.load(fh))
    if not results:
        raise SystemExit("没有可对比的结果文件")

    ks = results[0]["k"]
    base = results[0]["summary"]
    header = ["组", "样本", "MRR@10"] + ["Recall@%d" % k for k in ks] + ["文档级 Recall@%d" % k for k in ks]
    print("\t".join(header))
    for res in results:
        s = res["summary"]
        cells = [res["label"], str(s["count"]), "%.4f" % s["mrr@10"]]
        cells += ["%.4f" % s["recall@%d" % k] for k in ks]
        cells += ["%.4f" % s["doc_recall@%d" % k] for k in ks]
        print("\t".join(cells))

    print("\n相对首组的增量：")
    for res in results[1:]:
        s = res["summary"]
        print("  %-14s MRR@10 %+.4f   Recall@10 %+.4f"
              % (res["label"], s["mrr@10"] - base["mrr@10"],
                 s["recall@%d" % ks[-1]] - base["recall@%d" % ks[-1]]))

    # 逐题对比：哪些题在哪些组下命中情况不同 —— 便于人工复核，避免只看聚合值
    print("\n逐题命中名次对比（None = 未命中）：")
    by_qid = {res["label"]: {r["qid"]: r["hit_rank"] for r in res["details"]} for res in results}
    for qid in sorted({r["qid"] for r in results[0]["details"]}):
        ranks = [by_qid[res["label"]].get(qid) for res in results]
        if len(set(map(str, ranks))) > 1:
            print("  %-8s %s" % (qid, ranks))
    return 0


def main():
    parser = argparse.ArgumentParser(description="RAG 检索链路离线评测")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--goldset", default=os.path.join(os.path.dirname(__file__), "goldset.resolved.jsonl"))
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "results", "run.json"))
    parser.add_argument("--label", default="unnamed")
    parser.add_argument("--k", type=int, nargs="+", default=[5, 10])
    parser.add_argument("--sleep", type=float, default=0.2, help="每条之间的间隔秒数，避免打满上游")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--app-log", default=None,
                        help="应用日志路径。提供后会扫描链路降级证据"
                             "（上游欠费/限额、embedding 或 rerank 失败、闸门空转），"
                             "并把结论写入结果文件的 pipeline_health 字段")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--compare", nargs="+", help="只对比已有结果文件，不发起评测")
    parser.add_argument("--reaudit", metavar="RESULT_JSON",
                        help="事后审计已有结果：用当前 gold set 重算指标并补链路健康证据，"
                             "写出的文件由 --out 指定")
    args = parser.parse_args()

    if args.compare:
        return compare(args.compare)
    if args.reaudit:
        return reaudit(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
