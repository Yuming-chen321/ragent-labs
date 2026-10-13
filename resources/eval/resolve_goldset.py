#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 gold set 的「文档码 + 段序号」解析成真实 chunk id，并校验标注确实落在引用的 chunk 里

为什么分成两层：
  chunk id 是雪花 id，重新分块就会变。若直接把 id 写进 gold set，这份评测集就只能在
  「恰好那条数据」上跑，换台机器或重建语料立刻全废。所以标注只写「哪篇文档的第几段」，
  由本脚本在评测前解析成 id —— 换环境只需重跑本脚本，标注本身不用动。

为什么断言关键词：
  标注是人写的，人会写错。每条标注额外声明「这段里必须出现哪些字符串」，脚本逐个检查；
  断言不过就直接失败退出，不允许一份标注错的 gold set 进入评测 —— 指标是算出来的，
  标注错会让结论整个反向，而这类错误从聚合指标上看不出来。

  断言分两级：
    expect_keywords  整串级 —— 关键词出现在「所有参考段拼接后」即可，多跳题够用；
    chunk_keywords   逐段级 —— 把关键词钉到具体某段（键为「文档#段号」），
                     专门拦截「某段标错、却被另一段的关键词挡过去」。
  只写整串级会漏检：Q032 的参考里曾混入一段「请假流程」，因为同批参考里有真正的
  「离职」段，整串断言稀里糊涂就过了，而聚合指标看不出任何异常 → 两级一起用。

chunks 导出方式（在项目根目录执行）：
  docker exec postgres psql -U postgres -d ragent -t -A -c "
    select json_agg(json_build_object(
      'chunkId', c.id, 'docCode', regexp_replace(d.doc_name, '\\.[^.]*$', ''),
      'chunkIndex', c.chunk_index, 'content', c.content)
      order by d.doc_name, c.chunk_index)
    from t_knowledge_chunk c join t_knowledge_document d on d.id = c.doc_id;"
    | python -c "import sys,json;[print(json.dumps(x,ensure_ascii=False)) for x in json.load(sys.stdin)]"
    > chunks_export.jsonl
"""

import argparse
import json
import os
import re
import sys

WS = re.compile(r"\s+")


def normalize(text):
    """去掉所有空白再比对：语料里数字两侧的空格并不统一，按原文逐字比会误报"""
    return WS.sub("", text or "")


def load_chunks(path):
    index, total, by_doc = {}, 0, {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = (row["docCode"], int(row["chunkIndex"]))
            if key in index:
                raise SystemExit("chunks 导出里有重复键 %s —— 导出语句是否按文档去重过？" % (key,))
            index[key] = row
            by_doc.setdefault(row["docCode"], []).append(int(row["chunkIndex"]))
            total += 1
    return index, total, by_doc


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="解析并校验 gold set")
    parser.add_argument("--goldset", default=os.path.join(here, "goldset.jsonl"))
    parser.add_argument("--chunks", default=os.path.join(here, "chunks_export.jsonl"))
    parser.add_argument("--out", default=os.path.join(here, "goldset.resolved.jsonl"))
    args = parser.parse_args()

    if not os.path.exists(args.chunks):
        raise SystemExit("找不到 chunks 导出文件：%s\n导出命令见本脚本顶部注释。" % args.chunks)

    index, total, by_doc = load_chunks(args.chunks)
    print("已载入 %d 个 chunk，覆盖 %d 篇文档" % (total, len(by_doc)))

    resolved, problems, warnings = [], [], []
    with open(args.goldset, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            item = json.loads(line)
            qid = item["qid"]

            chunk_ids, contents, missing = [], [], []
            refs = item["reference_chunks"]
            for ref in refs:
                row = index.get((ref["doc"], int(ref["index"])))
                if row is None:
                    missing.append("%s#%d" % (ref["doc"], ref["index"]))
                    continue
                chunk_ids.append(row["chunkId"])
                contents.append(row["content"])
            if missing:
                detail = "；".join("%s 现有段落 %s" % (d, sorted(v)) for d, v in by_doc.items())
                problems.append("%s 引用了不存在的段落：%s（%s）" % (qid, missing, detail))
                continue

            # 断言一：整串级。expect_keywords 只要求出现在「所引用段落拼接后的文本」里。
            # 多跳题的关键词常常只落在其中一段，这一级足够宽松；但也正因为宽松，它抓不到
            # 「某一段标错了、却被另一段的关键词挡过去」——Q032 就栽在这里（参考里混进一段
            # 请假流程，靠 IT资产管理#3 的「离职」二字蒙混过关）。
            blob = normalize("".join(contents))
            for kw in item.get("expect_keywords", []):
                if normalize(kw) not in blob:
                    problems.append("%s 断言失败：所引用的段落里找不到 %r" % (qid, kw))

            # 断言二：逐段级。chunk_keywords 把关键词钉死到某一具体段落上，专治上面那类错配。
            # 键的格式是「文档#段号」，且该段必须已在 reference_chunks 中，避免写出两套参考。
            for key, kws in (item.get("chunk_keywords") or {}).items():
                doc, sep, seg = key.rpartition("#")
                if not sep or not seg.isdigit():
                    problems.append("%s chunk_keywords 的键 %r 不是「文档#段号」格式" % (qid, key))
                    continue
                row = index.get((doc, int(seg)))
                if row is None:
                    problems.append("%s chunk_keywords 引用了不存在的段落 %s" % (qid, key))
                    continue
                if row["chunkId"] not in chunk_ids:
                    problems.append("%s chunk_keywords 指定的 %s 不在 reference_chunks 内" % (qid, key))
                    continue
                seg_text = normalize(row["content"])
                for kw in kws:
                    if normalize(kw) not in seg_text:
                        problems.append("%s 逐段断言失败：%s 里找不到 %r" % (qid, key, kw))

            # 覆盖告警（不失败）：某段一个 expect_keywords 都不含。多跳题的补充段常常如此
            # （Q031 的「Java 工程师场景」「培训体系」两段都不含「电脑」），属正常，
            # 但每次都打出来，逼人扫一眼，好过悄悄放过真正标错的段。
            expect = item.get("expect_keywords", [])
            if expect:
                for ref, content in zip(refs, contents):
                    if not any(normalize(k) in normalize(content) for k in expect):
                        warnings.append("%s 的 %s#%s 不含任何 expect_keywords%s"
                                        % (qid, ref["doc"], ref["index"], expect))

            resolved.append({
                "qid": qid,
                "category": item["category"],
                "question": item["question"],
                "reference_chunk_ids": chunk_ids,
                "reference_doc_codes": sorted({r["doc"] for r in refs}),
                "reference_chunks": refs,
                "expect_keywords": item.get("expect_keywords", []),
            })

    if warnings:
        print("\n覆盖告警（不阻断）：以下参考段不含任何 expect_keywords。"
              "多跳题的补充段属正常，但请扫一眼确认不是标错：")
        for w in warnings:
            print("  ~ %s" % w)

    if problems:
        print("\n标注校验未通过，共 %d 处问题：" % len(problems))
        for p in problems:
            print("  - %s" % p)
        raise SystemExit("请修正 goldset.jsonl 后重跑")

    with open(args.out, "w", encoding="utf-8") as fh:
        for item in resolved:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    cats = {}
    for item in resolved:
        cats[item["category"]] = cats.get(item["category"], 0) + 1
    print("\n标注校验通过：%d 条 query，%d 个参考 chunk"
          % (len(resolved), sum(len(i["reference_chunk_ids"]) for i in resolved)))
    for cat, n in sorted(cats.items()):
        print("  %-8s %d 条" % (cat, n))
    print("已写出 %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
