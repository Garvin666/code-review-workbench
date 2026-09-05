# -*- coding: utf-8 -*-
"""
模型输出 → 工作台 finding 结构解析引擎 (v0.1)
将后端原始审查文本按任务类型结构化, 输出工作台 schema 的 finding 列表。
规则: 漏洞样本按 CWE 定级; 评论/批评类文本用关键词启发式定级并截断展示。
"""
import re

CWE_CATEGORY = {
    "89": "SQL注入", "90": "SQL注入", "94": "代码注入", "77": "命令注入",
    "78": "OS命令注入", "79": "XSS跨站脚本", "352": "CSRF跨站请求伪造",
    "434": "任意文件上传", "798": "硬编码凭据", "22": "路径穿越",
    "502": "不安全反序列化", "611": "XXE外部实体注入", "306": "缺失访问控制",
    "287": "认证绕过", "918": "SSRF服务端请求伪造", "327": "弱加密算法",
    "200": "敏感信息泄露", "601": "开放重定向", "93": "CRLF注入", "95": "代码注入",
}
CWE_SEVERITY = {
    "89": "P0", "90": "P0", "94": "P0", "77": "P0", "78": "P0",  # 注入类 → 高危
    "79": "P1", "352": "P1", "434": "P1", "798": "P1", "22": "P1",
    "502": "P1", "611": "P1", "306": "P1", "287": "P1", "918": "P1",
    "601": "P1", "93": "P1",
}
DEFAULT_CWE_SEV = "P1"
MAX_DESC = 900
MAX_EVIDENCE = 600

# 强漏洞特征(非漏洞任务也据此升 P1) —— 工作台语义: P0/P1=阻断级真实缺陷
STRONG_RISK = [
    "inject", "注入", "xss", "csrf", "cwe-", "硬编码", "密钥泄露", "越权",
    "反序列化", "命令执行", "eval(", "exec(", "命令注入", "sql 注入",
    "sql injection", "sqli", "credential", "明文密码", "任意文件上传",
]
POSITIVE_WORDS = ["没有问题", "无问题", "无风险", "lgtm", "无显著", "没有发现", "看似良好"]


def _trunc(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def _sev_from_text(text, default="P2"):
    """非漏洞任务定级: 仅区分「改进意见(P2)/纯正面(P3)」。
    P0/P1 只由 vuln_detect 的 CWE 映射产生 —— 与工作台'阻断级=工具确证漏洞'语义一致,
    避免普通 review 文本中 inject/credential 等宽泛词造成误升。"""
    if any(w in text for w in POSITIVE_WORDS):
        return "P3"
    return default


def _src_of(meta, label="代码片段"):
    f = (meta or {}).get("file") or (meta or {}).get("repo")
    if f:
        return f"{label} · {f}"
    return label


def _base(sample, severity, title, rule, category, desc, evidence, extra=None):
    meta = sample.get("meta") or {}
    f = {
        "id": f"{sample['id']}-F1",
        "severity": severity,
        "category": category,
        "title": title,
        "line": None,
        "src": _src_of(meta),
        "rule": rule,
        "description": _trunc(desc, MAX_DESC),
        "evidence": _trunc(evidence, MAX_EVIDENCE),
        "actions": {"mark_fp": True, "add_fix": True},
    }
    if extra:
        f.update(extra)
    return f


# ---------------- vuln_detect: CWE 结构化 ----------------
def parse_vuln(sample, text):
    meta = sample.get("meta") or {}
    cwe = (meta.get("cwe") or "").strip()
    name = (meta.get("vuln_name") or "").strip()
    findings = []
    # golden 模式直接从 meta 取 CWE/漏洞名, 描述从 output 提取"分析:"之后正文
    desc = text
    m = re.search(r"(?:分析|分析[:：])\s*(.*)", text, re.S)
    if m:
        desc = m.group(1).strip()
    if not desc or len(desc) < 10:
        desc = text
    if cwe:
        cw = cwe.lower().replace("cwe-", "").replace("cwe", "").strip()
        sev = CWE_SEVERITY.get(cw, DEFAULT_CWE_SEV)
        cat = CWE_CATEGORY.get(cw, "安全漏洞")
        rule = f"CWE-{cw}"
    else:
        sev, cat, rule = _sev_from_text(text, "P1"), "安全漏洞", "cwe.heuristic"
    title = f"[{cwe or '漏洞'}] {name}" if name else (cat + "风险")
    if not name and not cwe:
        title = "疑似安全漏洞"
    findings.append(_base(sample, sev, _trunc(title, 120), rule, cat, desc, text,
                          {"actions": {"mark_fp": True, "add_fix": True}}))
    return findings


# ---------------- review_critique: CRITIQUE + REVISED ----------------
def parse_critique(sample, text):
    meta = sample.get("meta") or {}
    findings = []
    crit, rev = None, None
    m = re.search(r"CRITIQUE[:：]?\s*(.*?)(?=REVISED[:：]|$)", text, re.S)
    if m:
        crit = m.group(1).strip()
    m2 = re.search(r"REVISED[:：]?\s*(.*)$", text, re.S)
    if m2:
        rev = m2.group(1).strip()
    body = crit or text
    sev = _sev_from_text(body, "P2")
    lang = sample.get("language") or "未知语言"
    findings.append(_base(
        sample, sev, f"代码质量与设计缺陷({lang})", "critique.review",
        "可维护性与正确性", body, text,
        {"actions": {"mark_fp": True, "add_fix": True}}))
    if rev and rev not in body:
        findings.append(_base(
            sample, "P3", "修订建议(REVISED)", "critique.revision",
            "修复方案", rev, _trunc(rev, MAX_EVIDENCE),
            {"actions": {"mark_fp": False, "add_fix": True}}))
    return findings


# ---------------- review_comment: 单条审查意见 ----------------
def parse_comment(sample, text):
    sev = _sev_from_text(text, "P2")
    meta = sample.get("meta") or {}
    ct = (meta.get("comment_type") or "general").replace("_", " ")
    rule = f"github.review.{ct}"
    title = text.split("\n")[0][:80] or "审查意见"
    findings = [_base(sample, sev, title, rule, "变更审查意见", text, text,
                      {"actions": {"mark_fp": True, "add_fix": True}})]
    return findings


def extract_findings(sample, text):
    """统一入口: 按任务类型路由"""
    if not text or not text.strip():
        return [{
            "id": f"{sample['id']}-F1", "severity": "P3",
            "category": "异常", "title": "无输出", "line": None,
            "src": "N/A", "rule": "error.empty-output",
            "description": "模型未返回任何审查内容", "evidence": "",
            "actions": {"mark_fp": False, "add_fix": False},
        }]
    task = sample.get("task")
    if task == "vuln_detect":
        return parse_vuln(sample, text)
    if task == "review_critique":
        return parse_critique(sample, text)
    return parse_comment(sample, text)
