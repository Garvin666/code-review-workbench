# -*- coding: utf-8 -*-
"""
试点第一步: 从统一语料 test 分片分层抽样真实代码审查样本
用法: python sample.py [--n-per-source 4] [--seed 42]
输出: output/samples.jsonl (带稳定 id, 供审查主流程消费)
"""
import argparse
import json
import os
import random
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.normpath(os.path.join(BASE, "..", "code_review_data", "processed", "unified", "test.jsonl"))
OUT = os.path.join(BASE, "output", "samples.jsonl")

# 期望覆盖的语言优先级(抽样时倾斜), None=不限
LANG_ORDER = ["python", "Python", "javascript", "JavaScript", "typescript",
              "TypeScript", "java", "Java", "go", "Go", "rust", "Rust"]
MAX_IN_LINES = 140      # 输入代码行数上限(可展示性)
MAX_IN_CHARS = 20000
MAX_OUT_CHARS = 12000
MIN_IN_CHARS = 40


def lang_rank(lang):
    if not lang:
        return 99
    ll = str(lang).lower()
    for i, lg in enumerate(LANG_ORDER):
        if ll == lg.lower():
            return i
    return 50


def choose(rows, n, rng):
    """尽量拉开语言与行数, 保证代表性"""
    chosen, seen_lang, pool = [], set(), rows[:]
    rng.shuffle(pool)
    # 第一轮: 每种语言最多 1 条
    for r in pool:
        if len(chosen) >= n:
            break
        lk = (r["language"] or "?").lower()
        if lk in seen_lang:
            continue
        chosen.append(r)
        seen_lang.add(lk)
    # 第二轮: 补齐(可重复语言但不同样本即可)
    if len(chosen) < n:
        for r in pool:
            if len(chosen) >= n:
                break
            if r["_key"] not in {c["_key"] for c in chosen}:
                chosen.append(r)
    return chosen[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-per-source", type=int, default=4)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    if not os.path.exists(CORPUS):
        sys.exit(f"[x] 语料不存在: {CORPUS}\n    请先运行 code_review_data/preprocess.py")
    rng = random.Random(args.seed)

    buckets = {}
    with open(CORPUS, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            inp = (rec.get("input") or "").strip()
            out = (rec.get("output") or "").strip()
            if len(inp) < MIN_IN_CHARS or len(out) < 10:
                continue
            if len(inp) > MAX_IN_CHARS or len(out) > MAX_OUT_CHARS:
                continue
            if inp.count("\n") > MAX_IN_LINES:
                continue
            buckets.setdefault(rec["source"], []).append(rec)

    picked, seq = [], 0
    src_seq = {}
    for source in sorted(buckets):
        rows = buckets[source]
        rows.sort(key=lambda r: lang_rank(r.get("language")))
        for i, rec in enumerate(rows):
            rec["_key"] = f"{source}-{i}"
        for rec in choose(rows, args.n_per_source, rng):
            seq += 1
            src_seq[source] = src_seq.get(source, 0) + 1
            rec = dict(rec)
            lang = rec.get("language")
            tag = (lang or "na").lower().replace(" ", "-").replace("#", "sharp")[:6] if lang else "na"
            rec["id"] = f"{source.split('_')[0]}-{tag}-{src_seq[source]:02d}"
            rec["_seq"] = seq
            picked.append(rec)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    picked.sort(key=lambda r: r["_seq"])
    with open(args.out, "w", encoding="utf-8") as f:
        for rec in picked:
            clean = {k: rec[k] for k in ("id", "source", "task", "language", "instruction", "input", "output", "meta")}
            f.write(json.dumps(clean, ensure_ascii=False) + "\n")

    by_src = {}
    langs = {}
    for r in picked:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
        lk = (r.get("language") or "?").lower()
        langs[lk] = langs.get(lk, 0) + 1
    print(f"[ok] 抽样完成 -> {args.out}")
    print(f"  总数: {len(picked)}  按来源: {by_src}  按语言: {dict(sorted(langs.items(), key=lambda x: -x[1]))}")


if __name__ == "__main__":
    main()
