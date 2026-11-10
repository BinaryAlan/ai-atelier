import os
import re
import json
import shutil
import subprocess
from typing import List, Dict, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_FILE = os.path.join(DATA_DIR, "scanned_models.json")

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

def scan_local_models(force_refresh: bool = False) -> List[Dict[str, Any]]:
    if not force_refresh and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                if cached:
                    return cached
        except Exception:
            pass

    models = []

    # 1. Antigravity CLI
    if shutil.which("agy"):
        models.append({
            "cli": "agy",
            "model_id": "default",
            "display_name": "Google Antigravity (Gemini)",
            "cli_cmd": 'agy -p "{prompt}"',
            "default_title": "全局首席架构师",
            "default_prompt": "你是 Google Antigravity，技术评审专家。注重工程规范、模块解耦、长远扩展性与高可用性，以客观理性的态度提供方案。"
        })

    # 2. CodeBuddy CLI
    if shutil.which("codebuddy"):
        try:
            out = subprocess.run(["codebuddy", "--help"], capture_output=True, text=True, timeout=5).stdout
            match = re.search(r"Currently supported:\s*\((.*?)\)", out)
            if match:
                raw_models = [m.strip() for m in match.group(1).split(",")]
                for mid in raw_models:
                    display = MODEL_ALIASES.get(mid, f"CodeBuddy ({mid})")
                    title = "AI 技术顾问"
                    prompt = f"你是基于 {mid} 模型的资深技术顾问，具备扎实的工程实现与逻辑推演能力，客观务实地提供技术建议。"

                    if "deepseek" in mid.lower():
                        title = "系统架构与性能专家"
                        prompt = "你是技术评审团的底层系统与性能架构专家。关注算法复杂度、并发安全与高可用防护，以严谨专业的态度提出建设性意见。"
                    elif "hy4" in mid.lower() or "hy3" in mid.lower():
                        title = "业务落地与工程效能专家"
                        prompt = "你是技术评审团的工程效能与落地顾问。注重开发周期、团队维护成本与业务适配度，提供务实平稳的技术路径。"
                    elif "kimi" in mid.lower():
                        title = "复杂上下文分析顾问"
                        prompt = "你是复杂文档与规范分析顾问，善于梳理复杂架构规范与详细边界条件。"

                    models.append({
                        "cli": "codebuddy",
                        "model_id": mid,
                        "display_name": display,
                        "cli_cmd": f'codebuddy --model {mid} --tools "" -p "{{prompt}}"',
                        "default_title": title,
                        "default_prompt": prompt
                    })
        except Exception as e:
            print("扫描 CodeBuddy 模型异常:", e)

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(models, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存模型扫描缓存失败:", e)

    return models
