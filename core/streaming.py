"""
执行中的流式输出暂存区（进程内，不写会话文件）。

为什么存在这里而不直接写 task/session：
- 引擎侧是「一轮跑完才写结果」的语义，把还没说完的半句话写进 messages/card
  会污染正文，还可能被并发保存覆盖；
- 界面只需要知道「这一刻那位成员正在写什么」，按需拉取即可；
- 一条回答结束时由调用方 clear，正式结果照常作为一条记录落库，历史里不留半截文本。

key 用 `{tid}:{stage}:{member_id}`（会话由 sid 单独记着，便于一次取整场），
和 core/tasks.py 的 execution_key 同源。

单进程 dev server 的取舍同 _ACTIVE_SESSIONS：多进程部署要换成共享存储（Redis 等），
否则不同 worker 之间看不到彼此的输出。
"""

import threading
import time
from typing import Any, Dict, Optional

_LOCK = threading.Lock()
_STREAMS: Dict[str, Dict[str, Any]] = {}

# 超过这个时间没有更新就视为陈旧（进程被杀 / 异常退出时不会有人来 clear）
_STALE_SECONDS = 600

# 单条流最多留这么多字符：长回答只在界面上看尾部，全量结果另有 raw_output 落库
MAX_LIVE_CHARS = 200_000


def stream_key(tid: str, stage: str, member_id: str = "") -> str:
    return f"{tid}:{stage}:{member_id}"


def publish(
    session_id: str,
    key: str,
    tid: str,
    stage: str,
    member_id: str = "",
    text: str = "",
    phase: str = "",
    activity: str = "",
) -> None:
    """更新某位成员「正在生成」的文本。

    text 是累计全文（不是增量），界面直接取尾部渲染；
    phase 说明当前在干什么（thinking / writing / tool），activity 是工具活动的短描述。
    """
    if not key:
        return
    body = text or ""
    if len(body) > MAX_LIVE_CHARS:
        body = body[-MAX_LIVE_CHARS:]
    with _LOCK:
        _STREAMS[key] = {
            "sid": session_id or "",
            "tid": tid or "",
            "stage": stage or "",
            "member_id": member_id or "",
            "text": body,
            "chars": len(body),
            "phase": phase or "",
            "activity": activity or "",
            "updated_at": time.time(),
        }


def clear(key: str) -> None:
    with _LOCK:
        _STREAMS.pop(key, None)


def clear_session(session_id: str) -> None:
    """删除会话时一并清掉（否则已删会话的流会挂到 10 分钟过期）"""
    if not session_id:
        return
    with _LOCK:
        for key in [k for k, v in _STREAMS.items() if v.get("sid") == session_id]:
            _STREAMS.pop(key, None)


def get(key: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        item = _STREAMS.get(key)
        if not item:
            return None
        if time.time() - item["updated_at"] > _STALE_SECONDS:
            _STREAMS.pop(key, None)
            return None
        return dict(item)


def for_session(session_id: str) -> Dict[str, Dict[str, Any]]:
    """取某场会话所有进行中的流：`{tid:stage:member}` → 内容。

    顺手把过期的清掉——这是唯一会被前端周期性调用的入口。
    """
    now = time.time()
    out: Dict[str, Dict[str, Any]] = {}
    with _LOCK:
        stale = []
        for key, item in _STREAMS.items():
            if now - item["updated_at"] > _STALE_SECONDS:
                stale.append(key)
                continue
            if item.get("sid") != session_id:
                continue
            out[key] = {
                "tid": item.get("tid", ""),
                "stage": item.get("stage", ""),
                "member_id": item.get("member_id", ""),
                "text": item.get("text", ""),
                "chars": item.get("chars", 0),
                "phase": item.get("phase", ""),
                "activity": item.get("activity", ""),
                "updated_at": item.get("updated_at", 0),
            }
        for key in stale:
            _STREAMS.pop(key, None)
    return out


def clear_all() -> None:
    """测试用：清空全部暂存。"""
    with _LOCK:
        _STREAMS.clear()
