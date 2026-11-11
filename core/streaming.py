"""
流式输出的进程内暂存区。

为什么不直接写会话文件：引擎线程用的是 save_session(merge=True) 语义，
把「还没说完的半句话」高频写进 messages 会污染正文、也可能被磁盘基线覆盖。
这里只把正在生成的文本放在内存里，前端按需拉取；一条回答结束时清空，
正式结果照常以一条消息落库，历史里不会留下半截文本。

单进程 dev server 的取舍同 _ACTIVE_SESSIONS / _SKIP_REQUESTS：多进程部署需要换成
共享存储（Redis 等），否则不同 worker 之间看不到彼此的输出。
"""

import threading
import time
from typing import Any, Dict, Optional

_LOCK = threading.Lock()
_STREAMS: Dict[str, Dict[str, Any]] = {}

# 超过这个时间没有更新就视为陈旧（进程被杀/异常退出时不会有人来 clear）
_STALE_SECONDS = 600


def publish_stream(session_id: str, speaker: str, text: str) -> None:
    """更新某场会议「正在生成」的文本。"""
    if not session_id:
        return
    body = text or ""
    with _LOCK:
        _STREAMS[session_id] = {
            "speaker": speaker or "",
            "text": body,
            "chars": len(body),
            "updated_at": time.time(),
        }


def clear_stream(session_id: str) -> None:
    with _LOCK:
        _STREAMS.pop(session_id, None)


def get_stream(session_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        item = _STREAMS.get(session_id)
        if not item:
            return None
        if time.time() - item["updated_at"] > _STALE_SECONDS:
            _STREAMS.pop(session_id, None)
            return None
        return dict(item)
