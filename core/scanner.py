import os
import re
import json
import time
import shutil
import subprocess
from typing import List, Dict, Any

from core.executor import normalize_cli_tools

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_FILE = os.path.join(DATA_DIR, "scanned_models.json")
CACHE_TTL_SECONDS = 24 * 3600

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

def _load_cache() -> List[Dict[str, Any]]:
    """读取缓存；超过 TTL 或格式不合法则视为无缓存。"""
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except Exception:
        return []

    # 兼容旧的「纯列表」缓存格式
    if isinstance(cached, list):
        models = cached
    elif isinstance(cached, dict):
        if time.time() - float(cached.get("ts") or 0) > CACHE_TTL_SECONDS:
            return []
        models = cached.get("models") or []
    else:
        return []

    if not isinstance(models, list):
        return []
    # 旧缓存（以及旧版列表格式）里存的是 `--tools ""` 模板，直接拿来建成员会得到
    # 一个「读不到任何文件」的只读成员 —— 这是新建成员后只会回开场白的根因之一。
    # 这里统一按只读权限补回 Read/Glob/Grep，让历史缓存也能自愈。
    healed = []
    for m in models:
        if not isinstance(m, dict) or not m.get("cli_cmd"):
            continue
        m = dict(m)
        m["cli_cmd"] = normalize_cli_tools(m["cli_cmd"], read_access=True, write_access=False)
        healed.append(m)
    return healed


def scan_local_models(force_refresh: bool = False) -> List[Dict[str, Any]]:
    if not force_refresh:
        cached = _load_cache()
        if cached:
            return cached

    models = []

    # 1. Antigravity CLI
    if shutil.which("agy"):
        models.append({
            "key": "agy:default",
            "cli": "agy",
            "model_id": "default",
            "display_name": "Google Antigravity (Gemini)",
            "cli_cmd": 'agy -p "{prompt}"',
            "default_prompt": ""
        })

    # 2. CodeBuddy CLI
    if shutil.which("codebuddy"):
        try:
            out = subprocess.run(
                ["codebuddy", "--help"], capture_output=True, text=True, timeout=5
            ).stdout
            match = re.search(r"Currently supported:\s*\((.*?)\)", out)
            if match:
                raw_models = [m.strip() for m in match.group(1).split(",")]
                for mid in raw_models:
                    if not mid:
                        continue
                    display = MODEL_ALIASES.get(mid, f"CodeBuddy ({mid})")
                    models.append({
                        "key": f"codebuddy:{mid}",
                        "cli": "codebuddy",
                        "model_id": mid,
                        "display_name": display,
                        # 只读工具：模型要能看工作目录里的方案/原型，否则会只回一句
                        # 「我先去读取…」就结束本轮（--tools "" 会把工具全禁掉）
                        "cli_cmd": f'codebuddy --model {mid} --tools "Read,Glob,Grep" -p "{{prompt}}"',
                        "default_prompt": ""
                    })
        except Exception as e:
            print("扫描 CodeBuddy 模型异常:", e)

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": int(time.time()), "models": models}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存模型扫描缓存失败:", e)

    return models
