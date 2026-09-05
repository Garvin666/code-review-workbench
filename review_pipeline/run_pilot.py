# -*- coding: utf-8 -*-
"""
试点主流程: 真实样本 → LLM 后端审查 → 结构化 findings → 工作台格式存档
用法:
  python run_pilot.py --backend golden                 # 离线链路验证(默认)
  python run_pilot.py --backend ollama --model qwen2.5-coder:7b   # 本地模型
  python run_pilot.py --backend openai                 # 需 OPENAI_BASE_URL/KEY/MODEL 环境变量
输出 output/:
  reviews_raw.jsonl        模型原始输出(含调用元数据)
  findings.jsonl           结构化 finding(工作台 schema)
  workbench_payload.json   单文件汇总 payload
  stats.json               统计
"""
import argparse
import datetime
import json
import os
import sys
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import backend as llm            # noqa: E402
import extract                   # noqa: E402

OUT = os.path.join(BASE, "output")
SAMPLES = os.path.join(OUT, "samples.jsonl")


def ts():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="golden", choices=["golden", "ollama", "openai"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--samples", default=SAMPLES)
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 条(0=全部)")
    args = ap.parse_args()

    if not os.path.exists(args.samples):
        sys.exit(f"[x] 无样本文件: {args.samples}\n    请先运行 sample.py")

    samples = []
    with open(args.samples, encoding="utf-8") as f:
        for line in f:
            samples.append(json.loads(line))
    if args.limit:
        samples = samples[: args.limit]
    print(f"[i] 载入 {len(samples)} 条样本 · backend={args.backend}"
          + (f" · model={args.model}" if args.model else ""))

    os.makedirs(OUT, exist_ok=True)
    raw_fh = open(os.path.join(OUT, "reviews_raw.jsonl"), "w", encoding="utf-8")
    fin_fh = open(os.path.join(OUT, "findings.jsonl"), "w", encoding="utf-8")

    ok = err = 0
    sev_counter = Counter()
    src_counter = Counter()
    lang_counter = Counter()
    total_lat = 0.0
    records = []  # workbench_payload 每样本汇总
    t_start = datetime.datetime.now()

    for sample in samples:
        res = llm.generate(sample, backend=args.backend, model=args.model)
        if res.get("error"):
            err += 1
            print(f"  [x] {sample['id']}: {res['error'][:100]}")
        else:
            ok += 1
        total_lat += res.get("latency_s", 0.0)

        findings = extract.extract_findings(sample, res.get("text") or "")

        # 计数
        for f_ in findings:
            sev_counter[f_["severity"]] += 1
        src_counter[sample["source"]] += 1
        lang_counter[str(sample.get("language") or "未知")] += 1

        rec = {
            "review": {
                "id": sample["id"], "source": sample["source"], "task": sample["task"],
                "language": sample.get("language"), "input": sample["input"],
                "output_ref": sample["output"], "meta": sample.get("meta"),
            },
            "model_call": {
                "backend": args.backend,
                "model": res.get("model"),
                "engine": "extract.py v0.1",
                "latency_s": res.get("latency_s", 0.0),
                "prompt_chars": res.get("prompt_chars", 0),
                "output_chars": res.get("output_chars", 0),
                "truncated": res.get("truncated", False),
                "error": res.get("error"),
                "generated_at": ts(),
            },
            "findings": findings,
            "stats": {
                "by_severity": dict(Counter(f_["severity"] for f_ in findings)),
                "finding_count": len(findings),
            },
        }
        records.append(rec)
        raw_fh.write(json.dumps({
            "id": sample["id"], "backend": args.backend, "model": res.get("model"),
            "latency_s": res.get("latency_s"), "text": res.get("text"),
            "error": res.get("error"),
        }, ensure_ascii=False) + "\n")
        fin_fh.write(json.dumps({
            "id": sample["id"], "backend": args.backend,
            "findings": findings,
        }, ensure_ascii=False) + "\n")
        print(f"  [{'ok' if not res.get('error') else 'x'}] {sample['id']:<16} "
              f"findings={len(findings)} sev={dict(sev_counter)}")

    raw_fh.close()
    fin_fh.close()

    elapsed = (datetime.datetime.now() - t_start).total_seconds()
    last_model = next((r["model_call"]["model"] for r in reversed(records)), None)
    payload = {
        "meta": {
            "pipeline": "data→model→workbench 端到端试点 v0.1",
            "run_at": ts(), "backend": args.backend, "model": last_model,
            "samples_total": len(samples), "ok": ok, "error": err,
            "elapsed_s": round(elapsed, 2),
            "avg_latency_s": round(total_lat / max(len(samples), 1), 3),
            "engine": "extract.py v0.1",
        },
        "records": records,
        "stats": {
            "by_severity": dict(sev_counter),
            "by_source": dict(src_counter),
            "by_language": dict(lang_counter),
        },
    }
    with open(os.path.join(OUT, "workbench_payload.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "stats.json"), "w", encoding="utf-8") as f:
        json.dump({"run_at": ts(), "backend": args.backend, "ok": ok, "err": err,
                   "elapsed_s": round(elapsed, 2), "total_findings": sum(sev_counter.values()),
                   "by_severity": dict(sev_counter), "by_source": dict(src_counter),
                   "by_language": dict(lang_counter)}, f, ensure_ascii=False, indent=1)

    print("\n===== 完成 =====")
    print(f"成功 {ok} / 失败 {err} · 耗时 {elapsed:.1f}s · 平均单条 {total_lat / max(len(samples), 1):.2f}s")
    print(f"严重度分布: {dict(sev_counter)}")
    print(f"存档: {os.path.join(OUT, 'workbench_payload.json')}")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
