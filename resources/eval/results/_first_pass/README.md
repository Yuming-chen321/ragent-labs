# 首轮采集原始件

这里是三组评测**第一次采集**的原始结果，用于事后核对，不要直接引用作为最终结果。

用途有两个：

1. **确定性校验**：组 1 / 组 2 各重跑了一轮，与这里的首轮结果**逐题命中名次完全一致**
   （MRR@10 也相同），说明在这套配置下评测结果是可复现的。
2. **组 3 的来源件**：正式的 `../vec-kw-rrf-rr.json` 由本目录的 `vec-kw-rrf-rr.json` 经
   `run_eval.py --reaudit` 生成。重算只有两个原因 —— gold set 修正了 Q032 的标注、
   以及补入链路健康证据；重算前后指标一致（0.9531 → 0.9531，逐题命中名次无变化）。

对比命令：

```bash
python resources/eval/run_eval.py --compare \
  resources/eval/results/_first_pass/vec-only.json \
  resources/eval/results/vec-only.json
```
