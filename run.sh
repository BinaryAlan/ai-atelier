#!/bin/bash
# -------------------------------------------------------------
# AI-Meeting 一键启动脚本
# -------------------------------------------------------------

cd "$(dirname "$0")"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 未检测到 python3，请先安装 Python 3.8+！"
    exit 1
fi

# 检查 Flask
python3 -c "import flask" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "📦 正在安装依赖 (Flask)..."
    pip install flask
fi

echo "=========================================================="
echo "🏛️  正在启动 AI-Meeting (AI 圆桌会议室)..."
echo "🌐 访问地址: http://127.0.0.1:5050"
echo "按 Ctrl+C 停止服务"
echo "=========================================================="

python3 app.py
