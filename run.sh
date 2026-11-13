#!/bin/bash
# -------------------------------------------------------------
# AI Atelier 服务管理脚本
#
#   ./run.sh            同 ./run.sh start
#   ./run.sh start      前台启动（正常使用，默认不开热重载）
#   ./run.sh dev        开发模式启动（FLASK_DEBUG=1，改 .py 自动重载）
#   ./run.sh stop       优雅停止（先走 API，失败再按端口 / PID 兜底）
#   ./run.sh restart    重启（进行中的任务标记为已取消，可在卡片里重试）
#   ./run.sh status     查看服务状态
#   ./run.sh logs       跟踪重启后的服务日志（.run/service.log）
# -------------------------------------------------------------

cd "$(dirname "$0")" || exit 1

PORT="${PORT:-5050}"
HOST="${HOST:-127.0.0.1}"
BASE="http://${HOST}:${PORT}"
PID_FILE=".run/app.pid"
SERVICE_LOG=".run/service.log"

# 这类接口能杀掉整个服务，必须带确认头（与前端一致，防跨站表单误触发）
ACTION_HEADER="X-Atelier-Action: 1"

# 依赖目录：优先使用项目内 .venv，避免污染系统 Python（PEP 668 环境会拒绝全局 pip install）
VENV_DIR=".venv"
PY=""

resolve_python() {
    if [ -x "$VENV_DIR/bin/python" ]; then
        PY="$VENV_DIR/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        PY="python3"
    fi
}

api_alive() {
    curl -fsS --max-time 2 "$BASE/api/system/status" >/dev/null 2>&1
}

api_json() {
    curl -fsS --max-time 2 "$BASE/api/system/status" 2>/dev/null
}

json_field() {
    # $1 = 字段名；JSON 从 stdin 读取
    "$PY" -c 'import json, sys
try:
    print(json.load(sys.stdin).get(sys.argv[1], ""))
except Exception:
    print("")' "$1"
}

read_pid() {
    [ -f "$PID_FILE" ] && cat "$PID_FILE" 2>/dev/null
}

port_pids() {
    lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null | tr '\n' ' '
}

ensure_env() {
    if ! command -v python3 >/dev/null 2>&1; then
        echo "❌ 未检测到 python3，请先安装 Python 3.8+！"
        exit 1
    fi

    if [ ! -d "$VENV_DIR" ]; then
        echo "📦 首次运行，创建虚拟环境 $VENV_DIR ..."
        python3 -m venv "$VENV_DIR" || exit 1
    fi

    resolve_python
    if [ -z "$PY" ]; then
        echo "❌ 找不到可用的 Python 解释器"
        exit 1
    fi

    if ! "$PY" -c "import flask" 2>/dev/null; then
        echo "📦 正在安装依赖 (requirements.txt)..."
        "$PY" -m pip install --disable-pip-version-check -q -r requirements.txt || exit 1
    fi
}

do_status() {
    if api_alive; then
        echo "✅ 服务运行中: $BASE"
        api_json | "$PY" -c 'import json, sys
d = json.load(sys.stdin)
print("   PID        : %s" % d.get("pid"))
print("   已运行     : %s 秒" % d.get("uptime_seconds"))
print("   热重载     : %s" % ("已开启" if d.get("hot_reload") else "未开启"))
print("   进行中任务 : %s" % len(d.get("active_tasks") or []))
print("   会话总数   : %s" % d.get("session_count"))' 2>/dev/null
        echo "   命令行管理 : ./run.sh restart | ./run.sh stop"
        return 0
    fi

    echo "⛔ 服务未运行（或端口 ${PORT} 无响应）"
    pid="$(read_pid)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "   注意：PID 文件记录的进程 $pid 仍存活，但 HTTP 无响应（可能卡住）。"
        echo "   可执行 ./run.sh stop 强制终止。"
    fi
    return 1
}

do_stop() {
    asked_graceful=0

    if api_alive; then
        echo "⏻ 正在请求服务优雅退出..."
        curl -fsS -X POST -H "$ACTION_HEADER" "$BASE/api/system/shutdown" >/dev/null 2>&1
        asked_graceful=1
        i=0
        while [ "$i" -lt 40 ]; do
            sleep 0.25
            api_alive || break
            i=$((i + 1))
        done
    fi

    # 兜底：端口仍被监听，或 pid 文件里的进程还活着，就用信号结束
    if api_alive || [ -n "$(port_pids)" ]; then
        echo "⚠️  服务仍有响应，改用信号终止..."
        targets="$(port_pids)"
        if [ -z "$targets" ]; then
            targets="$(read_pid)"
        fi
        for p in $targets; do
            kill -TERM "$p" 2>/dev/null
        done
        i=0
        while [ "$i" -lt 20 ]; do
            sleep 0.25
            [ -z "$(port_pids)" ] && break
            i=$((i + 1))
        done
        for p in $(port_pids); do
            kill -KILL "$p" 2>/dev/null
        done
        sleep 0.3
    fi

    rm -f "$PID_FILE"
    if api_alive; then
        echo "❌ 停止失败：端口 ${PORT} 仍在监听，请检查终端输出"
        return 1
    fi

    if [ "$asked_graceful" -eq 0 ]; then
        echo "ℹ️  服务本来就未在运行"
    else
        echo "✅ 服务已停止"
        echo "   重新启动：./run.sh start"
    fi
    return 0
}

do_start() {
    if api_alive; then
        echo "⚠️  服务已在运行：$BASE"
        echo "   要重启请用 ./run.sh restart；要停止请用 ./run.sh stop"
        exit 1
    fi

    leftover="$(port_pids)"
    if [ -n "$leftover" ]; then
        echo "❌ 端口 ${PORT} 已被其它进程占用 (PID: ${leftover})"
        echo "   先执行 ./run.sh stop 清理，或换端口：PORT=5051 ./run.sh start"
        exit 1
    fi

    ensure_env

    # 记录 PID：exec 之后 shell 被替换成服务进程，PID 保持不变
    mkdir -p "$(dirname "$PID_FILE")"
    echo "$$" > "$PID_FILE"

    echo "=========================================================="
    echo "🚀 正在启动 AI Atelier（多 AI 协同任务工作台）..."
    echo "🌐 访问地址: ${BASE}"
    echo "🛠️  命令行管理: ./run.sh status | ./run.sh restart | ./run.sh stop"
    echo "按 Ctrl+C 停止服务"
    echo "=========================================================="

    exec "$PY" app.py
}

do_restart() {
    if ! api_alive; then
        echo "⛔ 服务当前未运行，无法重启。"
        echo "   用 ./run.sh start 启动。"
        return 3
    fi

    old_pid="$(api_json | json_field pid)"
    old_start="$(api_json | json_field started_at)"

    echo "🔄 正在重启服务（进行中的任务会标记为已取消）..."
    curl -fsS -X POST -H "$ACTION_HEADER" "$BASE/api/system/restart" >/dev/null 2>&1

    i=0
    while [ "$i" -lt 60 ]; do
        sleep 0.5
        if api_alive; then
            new_pid="$(api_json | json_field pid)"
            new_start="$(api_json | json_field started_at)"
            if [ -n "$new_start" ] && { [ "$new_start" != "$old_start" ] || [ "$new_pid" != "$old_pid" ]; }; then
                mkdir -p "$(dirname "$PID_FILE")"
                echo "$new_pid" > "$PID_FILE" 2>/dev/null
                echo "✅ 服务已重启：${BASE}（新 PID: ${new_pid}）"
                echo "   被打断的任务已标记为已取消，可在任务卡片里重试。"
                # 继任实例的输出不接终端（见 app.py _spawn_successor 的说明），只写日志文件
                echo "   启动日志：${SERVICE_LOG}（跟踪：./run.sh logs）"
                return 0
            fi
        fi
        i=$((i + 1))
    done

    echo "❌ 重启超时：30 秒内没等到新进程接管端口 ${PORT}"
    if [ -f "$SERVICE_LOG" ]; then
        echo "--- ${SERVICE_LOG} 最后 20 行 ---"
        tail -n 20 "$SERVICE_LOG" 2>/dev/null
        echo "-------------------------------"
    fi
    echo "   若端口已空闲，可直接 ./run.sh start 前台启动。"
    return 1
}

do_logs() {
    if [ ! -f "$SERVICE_LOG" ]; then
        echo "ℹ️  还没有日志文件（${SERVICE_LOG}）。"
        echo "   它只在 ./run.sh restart 拉起继任实例后产生；前台启动的输出直接打在终端。"
        return 1
    fi
    echo "📄 跟踪 ${SERVICE_LOG}（Ctrl+C 退出）"
    tail -n 80 -f "$SERVICE_LOG"
}

resolve_python

CMD="${1:-start}"
case "$CMD" in
    start)
        do_start
        ;;
    dev)
        # 仅开发时使用：正常启动默认 FLASK_DEBUG=0，改 .py 需要 ./run.sh restart
        export FLASK_DEBUG=1
        do_start
        ;;
    stop)
        do_stop
        ;;
    restart)
        do_restart
        ;;
    status)
        do_status
        ;;
    logs)
        do_logs
        ;;
    -h|--help|help)
        sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *)
        echo "未知命令: $CMD"
        echo "用法: ./run.sh [start|dev|stop|restart|status|logs]"
        exit 2
        ;;
esac
