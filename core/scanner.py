import os
import re
import json
import time
import shutil
import subprocess
from typing import List, Dict, Any, Optional, Tuple

from core.executor import normalize_cli_tools
from core.cli_registry import (
    CLI_SPECS,
    REASONING_IDS,
    REASONING_LABELS,
    DEFAULT_REASONING,
    build_cli_cmd,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_FILE = os.path.join(DATA_DIR, "scanned_models.json")
CACHE_TTL_SECONDS = 24 * 3600
# 结构 / 扫描范围升级过就调大：旧缓存一律重扫，别拿老结构糊弄界面
CACHE_SCHEMA = 3
# 枚举模型要联网问 CLI，单条命令不能把整个「重新扫描」拖死
MODEL_FETCH_TIMEOUT = 15

MODEL_ALIASES = {
    "hy4std": "腾讯混元 4 (标准版 hy4std)",
    "hy4free": "腾讯混元 4 (免费版 hy4free)",
    "hy3": "腾讯混元 3 (hy3)",
    "hy3-x": "腾讯混元 3-X (hy3-x)",
    "deepseek-v4-pro": "DeepSeek V4 Pro (深度推理)",
    "deepseek-v4.1-flash": "DeepSeek V4.1 Flash (快速版)",
    "glm-5.3": "智谱 GLM 5.3",
    "glm-5.3-flash": "智谱 GLM 5.3 Flash",
    "glm-5.2": "智谱 GLM 5.2",
    "kimi-k3.1-pro": "月之暗面 Kimi K3.1 Pro",
    "kimi-k3-1": "月之暗面 Kimi K3.1",
    "kimi-k2.8-preview": "月之暗面 Kimi K2.8 Preview",
    "minimax-m3.1-flash": "MiniMax M3.1 Flash",
    "minimax-m2.7": "MiniMax M2.7",
    "space-bunny": "Space Bunny",
    "step-5-preview": "阶跃星辰 Step 5 Preview",
    "mimo-v2.6-pro": "Mimo V2.6 Pro"
}

_VARIANT_SUFFIX_RE = re.compile(r"-(low|medium|high)$", re.I)
_VARIANT_LABEL_RE = re.compile(r"\s*\((?:Low|Medium|High)\)\s*$", re.I)
_OPENCODE_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(?:#[A-Za-z0-9._-]+)?$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_free_model(model_id: str, display_name: str = "") -> bool:
    """CLI 没有统一的「是否免费」元数据，只能按模型 ID / 展示名里的 free 标记推断。"""
    text = f"{model_id} {display_name}".lower()
    return "free" in text or "免费" in (display_name or "")


def _expired(ts: float) -> bool:
    return not ts or (time.time() - float(ts)) > CACHE_TTL_SECONDS


def _load_cache() -> List[Dict[str, Any]]:
    """读取缓存；超过 TTL、结构版本不符或格式不合法都视为无缓存。"""
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except Exception:
        return []

    # 兼容旧的「纯列表」缓存格式。它没有 ts 字段，旧实现直接返回、永不过期：
    # 装了新 CLI / CLI 更新了模型列表，界面也一直显示几周前的那份。
    if isinstance(cached, list):
        try:
            ts = os.path.getmtime(CACHE_FILE)
        except OSError:
            return []
        if _expired(ts):
            return []
        models = cached
    elif isinstance(cached, dict):
        if cached.get("schema") != CACHE_SCHEMA:
            return []
        try:
            ts = float(cached.get("ts") or 0)
        except (TypeError, ValueError):
            return []
        if _expired(ts):
            return []
        models = cached.get("models") or []
    else:
        return []

    if not isinstance(models, list):
        return []
    # 旧缓存里存的是 `--tools ""` 模板，直接拿来建成员会得到一个
    # 「读不到任何文件」的只读成员；统一按只读权限补回 Read/Glob/Grep。
    healed = []
    for m in models:
        if not isinstance(m, dict) or not m.get("cli_cmd"):
            continue
        m = dict(m)
        m["cli_cmd"] = normalize_cli_tools(m["cli_cmd"], read_access=True, write_access=False)
        m.setdefault("is_free", is_free_model(m.get("model_id", ""), m.get("display_name", "")))
        # 老缓存没有推理档位：补一个「默认」单档，界面仍能正常展示/执行
        if not m.get("reasoning_levels"):
            m["reasoning_levels"] = [{
                "id": "default", "label": "默认", "cli_cmd": m["cli_cmd"]
            }]
        m.setdefault("default_reasoning", "default")
        healed.append(m)
    return healed


def _run(argv: List[str], timeout: int = MODEL_FETCH_TIMEOUT) -> str:
    """跑一条只读的枚举命令；失败返回空串，绝不让扫描把整个请求带崩。"""
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        ).stdout or ""
    except Exception as e:
        print(f"扫描 {argv[0]} 模型异常:", e)
        return ""


def _build_model(
    spec: Dict[str, Any],
    base_id: str,
    display: str,
    variants: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """把一个基础模型和它的推理档位打包成一条扫描结果。"""
    cli = spec["name"]
    if spec.get("reasoning") == "none":
        # 该 CLI 没有推理档位参数：只给「默认」单档，别伪造不存在的开关
        cmd = build_cli_cmd(cli, base_id, "default")
        levels = [{"id": "default", "label": "默认", "cli_cmd": cmd}]
        default = "default"
    else:
        levels = []
        for rid in REASONING_IDS:
            if variants is not None:
                if rid not in variants:
                    continue
                vid = variants[rid]
            else:
                vid = None
            levels.append({
                "id": rid,
                "label": REASONING_LABELS[rid],
                "cli_cmd": build_cli_cmd(cli, base_id, rid, vid),
            })
        if not levels:
            levels = [{
                "id": "default", "label": "默认",
                "cli_cmd": build_cli_cmd(cli, base_id, "default"),
            }]
        default = (
            DEFAULT_REASONING
            if any(x["id"] == DEFAULT_REASONING for x in levels)
            else levels[-1]["id"]
        )

    default_cmd = next(x["cli_cmd"] for x in levels if x["id"] == default)
    return {
        "key": f"{cli}:{base_id}",
        "cli": cli,
        "model_id": base_id,
        "display_name": display,
        # 兼容旧字段：等于默认档位的命令
        "cli_cmd": default_cmd,
        "default_prompt": "",
        "is_free": is_free_model(base_id, display),
        "reasoning_levels": levels,
        "default_reasoning": default,
    }


# --------------------------------------------------------------------------
# 各 CLI 的模型枚举解析器：统一返回 [(model_id, display_name), ...]
# --------------------------------------------------------------------------
def _parse_agy_table(out: str, spec: Dict[str, Any]) -> List[Tuple[str, str]]:
    """`agy models` 输出 `gemini-3.8-flash-high\\tGemini 3.8 Flash (High)`。"""
    pairs = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "Fetching" in line:
            continue
        parts = re.split(r"\t+|\s{2,}", line, maxsplit=1)
        mid = parts[0].strip()
        label = parts[1].strip() if len(parts) > 1 else mid
        if _MODEL_ID_RE.match(mid):
            pairs.append((mid, label))
    return pairs


def _parse_codebuddy_help(out: str, spec: Dict[str, Any]) -> List[Tuple[str, str]]:
    match = re.search(r"Currently supported:\s*\((.*?)\)", out)
    if not match:
        return []
    return [
        (mid, MODEL_ALIASES.get(mid, mid))
        for mid in (m.strip() for m in match.group(1).split(","))
        if mid
    ]


def _parse_opencode_lines(out: str, spec: Dict[str, Any]) -> List[Tuple[str, str]]:
    """只保留 spec.providers 里的 provider（默认排除 github-copilot / gitlab）。"""
    providers = set(spec.get("providers") or [])
    pairs = []
    for line in out.splitlines():
        mid = line.strip()
        if not _OPENCODE_MODEL_RE.match(mid):
            continue
        provider, _, name = mid.partition("/")
        if providers and provider not in providers:
            continue
        pairs.append((mid, f"{name} ({provider})"))
    return pairs


def _parse_help_hint(out: str, spec: Dict[str, Any]) -> List[Tuple[str, str]]:
    """CLI 没有 models 子命令：先从 --help 里抓示例模型 ID，再用内置候选补齐。"""
    ids: List[str] = []
    for line in out.splitlines():
        if "--model" not in line.lower():
            continue
        hint = re.search(r"\(e\.g\.,?\s*([^)]*)\)", line, re.I)
        if hint:
            ids += [t.strip().strip("'\"") for t in re.split(r"[,/]|\bor\b", hint.group(1))]
        ids += re.findall(r"'([A-Za-z][A-Za-z0-9._-]{1,})'", line)
    for mid in spec.get("fallback_models") or []:
        if mid not in ids:
            ids.append(mid)

    seen, pairs = set(), []
    for mid in ids:
        mid = mid.strip()
        if mid and mid not in seen and _MODEL_ID_RE.match(mid):
            seen.add(mid)
            pairs.append((mid, mid))
    return pairs


def _parse_claude_help(out: str, spec: Dict[str, Any]) -> List[Tuple[str, str]]:
    return _parse_help_hint(out, spec)


def _parse_codex_config(out: str, spec: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Codex 不提供模型列表：读 ~/.codex/config.toml 里正在用的模型，再补常见 ID。"""
    ids: List[str] = []
    try:
        with open(os.path.expanduser("~/.codex/config.toml"), "r", encoding="utf-8") as f:
            hit = re.search(r'^\s*model\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
        if hit:
            ids.append(hit.group(1))
    except Exception:
        pass
    for mid in spec.get("fallback_models") or []:
        if mid not in ids:
            ids.append(mid)
    return [(mid, mid) for mid in ids if _MODEL_ID_RE.match(mid)]


def _parse_cursor_help(out: str, spec: Dict[str, Any]) -> List[Tuple[str, str]]:
    return _parse_help_hint(out, spec)


def _parse_ollama_table(out: str, spec: Dict[str, Any]) -> List[Tuple[str, str]]:
    pairs = []
    for line in out.splitlines():
        parts = line.split()
        if not parts or parts[0].upper() == "NAME":
            continue
        mid = parts[0]
        if _MODEL_ID_RE.match(mid) or re.fullmatch(r"[\w.-]+:[\w.-]+", mid):
            pairs.append((mid, mid))
    return pairs


PARSERS = {
    "agy_table": _parse_agy_table,
    "codebuddy_help": _parse_codebuddy_help,
    "opencode_lines": _parse_opencode_lines,
    "claude_help": _parse_claude_help,
    "codex_config": _parse_codex_config,
    "cursor_help": _parse_cursor_help,
    "help_hint": _parse_help_hint,
    "ollama_table": _parse_ollama_table,
}


def _legacy_agy(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """老版本 agy 没有 models 子命令、或临时取不到列表：退回不指定模型的记录。"""
    cmd = 'agy -p "{prompt}"'
    return [{
        "key": "agy:default",
        "cli": "agy",
        "model_id": "default",
        "display_name": "Google Antigravity (Gemini)",
        "cli_cmd": cmd,
        "default_prompt": "",
        "is_free": False,
        "reasoning_levels": [{"id": "default", "label": "默认", "cli_cmd": cmd}],
        "default_reasoning": "default",
    }]


def _aggregate_agy(
    spec: Dict[str, Any], pairs: List[Tuple[str, str]]
) -> List[Dict[str, Any]]:
    """把 -low/-medium/-high 变体聚合成同一个基础模型。"""
    groups: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for mid, label in pairs:
        hit = _VARIANT_SUFFIX_RE.search(mid)
        if hit:
            rid = hit.group(1).lower()
            base = mid[:hit.start()]
            display = _VARIANT_LABEL_RE.sub("", label).strip()
        else:
            rid, base, display = None, mid, label

        if base not in groups:
            groups[base] = {"display": display, "variants": {}, "plain": False}
            order.append(base)
        g = groups[base]
        if rid:
            g["variants"][rid] = mid
        else:
            # 该模型自己也作为独立 ID 出现：用 --effort 给三档
            g["plain"] = True

    models = []
    for base in order:
        g = groups[base]
        variants = None if g["plain"] else (g["variants"] or None)
        models.append(_build_model(spec, base, g["display"], variants))
    return models


def _scan_one(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = _run(spec["probe"]) if spec.get("probe") else ""
    pairs = PARSERS[spec["parser"]](out, spec)

    if spec["parser"] == "agy_table":
        return _aggregate_agy(spec, pairs) if pairs else _legacy_agy(spec)
    return [_build_model(spec, mid, display) for mid, display in pairs]


def scan_local_models(force_refresh: bool = False) -> List[Dict[str, Any]]:
    if not force_refresh:
        cached = _load_cache()
        if cached:
            return cached

    # 遍历注册表：本机装了哪些常用大模型 CLI，就把哪些列出来。
    # 新增 CLI 只需在 core/cli_registry.py 加一条 spec，不必改这里的分支。
    models: List[Dict[str, Any]] = []
    for spec in CLI_SPECS:
        if not shutil.which(spec["name"]):
            continue
        try:
            models.extend(_scan_one(spec))
        except Exception as e:
            print(f"扫描 {spec['name']} 模型失败:", e)

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "schema": CACHE_SCHEMA,
                "ts": int(time.time()),
                "models": models,
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存模型扫描缓存失败:", e)

    return models
