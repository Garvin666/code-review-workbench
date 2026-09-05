# -*- coding: utf-8 -*-
"""
模型后端适配器 —— 三种可切换后端, 统一 generate(sample) -> dict
  1. golden : 直接返回数据集人工真值 (离线可用, 用于链路验证/回归基线)
  2. ollama : 本地 Ollama (http://127.0.0.1:11434), 模型名经 --model 指定
  3. openai : 任意 OpenAI 兼容端点 (DeepSeek/Qwen/硅基流动...), 凭据走环境变量
"""
import json
import os
import time
import urllib.request
import urllib.error

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OPENAI_BASE = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")


def _build_prompt(sample):
    inst = (sample.get("instruction") or "").strip()
    code = (sample.get("input") or "").strip()
    return f"{inst}\n\n【代码】\n```\n{code}\n```\n\n请直接输出审查结果。"


def call_golden(sample):
    text = (sample.get("output") or "").strip()
    return {
        "text": text,
        "backend": "golden",
        "model": "dataset-reference(人工真值)",
        "latency_s": 0.0,
        "truncated": False,
        "error": None,
        "prompt_chars": len(_build_prompt(sample)),
        "output_chars": len(text),
    }


def call_ollama(sample, model="qwen2.5-coder:7b", timeout=180):
    t0 = time.time()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": _build_prompt(sample)}],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 4096},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = (data.get("message", {}).get("content") or "").strip()
        return {
            "text": text,
            "backend": "ollama",
            "model": model,
            "latency_s": round(time.time() - t0, 2),
            "truncated": False,
            "error": None,
            "prompt_chars": len(_build_prompt(sample)),
            "output_chars": len(text),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "text": "",
            "backend": "ollama",
            "model": model,
            "latency_s": round(time.time() - t0, 2),
            "truncated": False,
            "error": f"ollama 调用失败: {e}. 请确认已启动 ollama serve 且已拉取模型 {model}",
            "prompt_chars": len(_build_prompt(sample)),
            "output_chars": 0,
        }


def call_openai(sample, model=None, timeout=180):
    t0 = time.time()
    model = model or OPENAI_MODEL or "deepseek-chat"
    if not OPENAI_BASE or not OPENAI_KEY:
        return {
            "text": "", "backend": "openai", "model": model, "latency_s": 0.0,
            "truncated": False,
            "error": "缺少凭据: 请设置环境变量 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL",
            "prompt_chars": len(_build_prompt(sample)), "output_chars": 0,
        }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": _build_prompt(sample)}],
        "temperature": 0.2,
        "max_tokens": 4096,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{OPENAI_BASE}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = (data["choices"][0]["message"]["content"] or "").strip()
        return {
            "text": text, "backend": "openai", "model": model,
            "latency_s": round(time.time() - t0, 2), "truncated": False,
            "error": None,
            "prompt_chars": len(_build_prompt(sample)), "output_chars": len(text),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "text": "", "backend": "openai", "model": model,
            "latency_s": round(time.time() - t0, 2), "truncated": False,
            "error": f"openai 兼容调用失败: {e}",
            "prompt_chars": len(_build_prompt(sample)), "output_chars": 0,
        }


def generate(sample, backend="golden", model=None):
    if backend == "ollama":
        return call_ollama(sample, model=model or "qwen2.5-coder:7b")
    if backend == "openai":
        return call_openai(sample, model=model)
    return call_golden(sample)


if __name__ == "__main__":
    # 自检: 构造最小样例验证三后端接口不抛异常
    demo = {"instruction": "审查代码", "input": "x = 1", "output": "无问题"}
    for b in ("golden", "ollama", "openai"):
        r = generate(demo, backend=b)
        print(b, "->", r.get("error") or f"text={len(r['text'])}ch model={r['model']}")
