#!/bin/bash
# -------------------------------------------------------------
# AI-Meeting 一键启动脚本
# -------------------------------------------------------------

set -e
cd "$(dirname "$0")"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 未检测到 python3，请先安装 Python 3.8+！"
    exit 1
fi

# 依赖目录：优先使用项目内 .venv，避免污染系统 Python（PEP 668 环境会拒绝全局 pip install）
VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "📦 首次运行，创建虚拟环境 $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi

PY="$VENV_DIR/bin/python"
if [ ! -x "$PY" ]; then
    PY="python3"
fi

if ! "$PY" -c "import flask" 2>/dev/null; then
    echo "📦 正在安装依赖 (requirements.txt)..."
    "$PY" -m pip install --disable-pip-version-check -q -r requirements.txt
fi

PORT="${PORT:-5050}"
echo "=========================================================="
echo "🏛️  正在启动 AI-Meeting (AI 圆桌会议室)..."
echo "🌐 访问地址: http://127.0.0.1:${PORT}"
echo "按 Ctrl+C 停止服务"
echo "=========================================================="

exec "$PY" app.py
