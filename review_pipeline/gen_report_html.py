# -*- coding: utf-8 -*-
"""
从 workbench_payload.json 渲染试点结果可视化页 (pilot_report.html, 深色主题)
用法: python gen_report_html.py
"""
import html
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "output", "workbench_payload.json")
DST = os.path.join(BASE, "pilot_report.html")

SEV_COLOR = {
    "P0": ("#E5484D", "#3A0D10", "阻断级"),
    "P1": ("#F5A623", "#3A2405", "高危"),
    "P2": ("#E4C400", "#332B05", "建议"),
    "P3": ("#8B9BB4", "#17202E", "提示"),
}


def esc(s):
    return html.escape(str(s or ""), quote=True)


def main():
    data = json.load(open(SRC, encoding="utf-8"))
    meta, stats = data["meta"], data["stats"]
    records = data["records"]

    def badge(sev):
        fg, bg, label = SEV_COLOR.get(sev, ("#fff", "#333", sev))
        return (f'<span class="sev" style="color:{fg};background:{bg}">'
                f'{esc(sev)} · {label}</span>')

    cards = []
    for r in records:
        rev = r["review"]
        mc = r["model_call"]
        src_name = {"cwe_vuln_review": "漏洞样本", "dahoas_critique_revision": "批评+修订",
                    "github_codereview": "GitHub PR"}.get(rev["source"], rev["source"])
        origin = (rev["meta"] or {}).get("cwe") or (rev["meta"] or {}).get("repo") \
            or (rev["meta"] or {}).get("file") or "—"
        fs = []
        for f_ in r["findings"]:
            fs.append(
                f'<div class="fcard"><div class="fhead">{badge(f_["severity"])}'
                f'<span class="rule">{esc(f_["rule"])}</span>'
                f'<span class="cat">{esc(f_["category"])}</span></div>'
                f'<div class="ftitle">{esc(f_["title"])}</div>'
                f'<div class="fdesc">{esc(f_["description"])}</div>'
                f'<details class="fev"><summary>证据片段 / 原文</summary>'
                f'<pre>{esc(f_["evidence"] or f_["description"])}</pre></details></div>')
        cards.append(
            f'<div class="rcard">'
            f'<div class="rhead"><span class="rid">{esc(rev["id"])}</span>'
            f'<span class="chip">{esc(src_name)}</span>'
            f'<span class="chip">{esc(rev["task"])}</span>'
            f'<span class="chip">{esc(rev["language"] or "—")}</span>'
            f'<span class="chip dim">{esc(origin)}</span></div>'
            f'<div class="rmeta">backend={esc(mc["backend"])} · model={esc(mc["model"])} '
            f'· {esc(mc["latency_s"])}s · findings={len(r["findings"])}</div>'
            + "".join(fs)
            + f'<details class="code"><summary>查看被审查代码 ({len(rev["input"])} 字符)</summary>'
            + f'<pre>{esc(rev["input"])}</pre></details></div>')

    sev_cards = "".join(
        f'<div class="scard"><div class="snum" style="color:{SEV_COLOR[k][0]}">{v}</div>'
        f'<div class="slab">{k} · {SEV_COLOR[k][1] and SEV_COLOR[k][2]}</div></div>'
        for k, v in sorted(stats["by_severity"].items(), reverse=True))

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>代码审查端到端试点 · 结果概览</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0E1220; color:#DDE3F0;
        font:14px/1.6 Inter,"Noto Sans SC","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 64px; }}
  h1 {{ font-size:22px; margin:0 0 4px; color:#fff; letter-spacing:.2px; }}
  .sub {{ color:#93A0B8; margin-bottom:22px; }}
  .metabar {{ display:flex; flex-wrap:wrap; gap:10px; margin:18px 0 8px; }}
  .metabar .m {{ background:#161C30; border:1px solid #232B45; border-radius:8px;
        padding:6px 12px; color:#B9C4DC; font-size:12.5px; }}
  .metabar b {{ color:#fff; font-weight:600; }}
  .sevcards {{ display:flex; gap:12px; margin:16px 0 26px; }}
  .scard {{ flex:1; background:#161C30; border:1px solid #232B45; border-radius:12px;
        padding:14px 10px; text-align:center; }}
  .snum {{ font:600 30px/1.2 "JetBrains Mono",monospace; }}
  .slab {{ color:#93A0B8; font-size:12px; margin-top:4px; }}
  .rcard {{ background:#151A2C; border:1px solid #242E4D; border-radius:12px;
        padding:16px 18px; margin-bottom:16px; }}
  .rhead {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-bottom:6px; }}
  .rid {{ font:600 14px "JetBrains Mono",monospace; color:#8E7BFF; }}
  .chip {{ background:#1D2440; color:#B9C4DC; border:1px solid #2A3456; border-radius:999px;
        padding:2px 10px; font-size:11.5px; }}
  .chip.dim {{ color:#7C88A3; }}
  .rmeta {{ color:#7C88A3; font-size:11.5px; margin-bottom:12px;
        font-family:"JetBrains Mono",monospace; }}
  .fcard {{ background:#10152a; border:1px solid #27314f; border-left:3px solid #39435F;
        border-radius:8px; padding:10px 14px; margin:10px 0; }}
  .fhead {{ display:flex; gap:8px; align-items:center; margin-bottom:6px; }}
  .sev {{ border-radius:999px; padding:1px 10px; font:600 11px "JetBrains Mono",monospace; }}
  .rule {{ font:500 11px "JetBrains Mono",monospace; color:#6F7B96; }}
  .cat {{ font-size:11.5px; color:#93A0B8; margin-left:auto; }}
  .ftitle {{ color:#E8EDF7; font-weight:600; margin:2px 0 4px; }}
  .fdesc {{ color:#C6CFE0; font-size:13px; white-space:pre-wrap; }}
  .fev summary {{ cursor:pointer; color:#8E7BFF; font-size:12px; margin-top:6px; }}
  details.code summary {{ cursor:pointer; color:#93A0B8; font-size:12px; margin-top:10px; }}
  pre {{ background:#0B0F1E; border:1px solid #1F2742; border-radius:8px; padding:12px;
        overflow:auto; max-height:340px; color:#A9C1E8; font:12px/1.55 "JetBrains Mono",monospace;
        white-space:pre-wrap; word-break:break-word; }}
  .foot {{ color:#6F7B96; font-size:12px; margin-top:28px; border-top:1px solid #1F2742; padding-top:14px; }}
</style></head><body><div class="wrap">
  <h1>代码审查工作台 · 端到端试点结果</h1>
  <div class="sub">真实数据集 → 审查模型后端 → 工作台格式结构化 → 存档</div>
  <div class="metabar">
    <div class="m">后端 <b>{esc(meta["backend"])}</b></div>
    <div class="m">模型 <b>{esc(meta["model"])}</b></div>
    <div class="m">样本 <b>{meta["samples_total"]}</b> · 成功 <b style="color:#46D28A">{meta["ok"]}</b> / 失败 <b style="color:#E5484D">{meta["error"]}</b></div>
    <div class="m">解析引擎 <b>{esc(meta["engine"])}</b></div>
    <div class="m">运行 <b>{esc(meta["run_at"])}</b></div>
  </div>
  <div class="sevcards">{sev_cards}</div>
  {''.join(cards)}
  <div class="foot">产物: output/samples.jsonl · reviews_raw.jsonl · findings.jsonl · workbench_payload.json · stats.json ｜ 重新生成: python run_pilot.py --backend &lt;golden|ollama|openai&gt; 后 python gen_report_html.py</div>
</div></body></html>"""
    with open(DST, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"[ok] {DST} ({os.path.getsize(DST)} B)")


if __name__ == "__main__":
    main()
