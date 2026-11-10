import re
import time
from typing import Dict, Any, Optional, Tuple
from core.executor import Executor
from core.storage import load_settings, save_session

class MeetingEngine:
    @staticmethod
    def get_employee(settings: Dict[str, Any], emp_id: str) -> Optional[Dict[str, Any]]:
        for e in settings.get("employees", []):
            if e["id"] == emp_id:
                return e
        return None

    @staticmethod
    def user_inject(session: Dict[str, Any], content: str, is_new_direction: bool = False):
        """
        用户发言/插话。
        如果是新指令或新方向，重置为阶段一（立论）。
        """
        now = int(time.time())
        msg = {
            "id": f"msg_{now}_{len(session['messages'])}",
            "sender": "user",
            "sender_name": "群主 (你)",
            "content": content,
            "phase": session.get("phase", 1),
            "verdict": "USER",
            "elapsed_time": 0,
            "created_at": now
        }
        session["messages"].append(msg)

        # 判断是否重新开始新一轮立论 (新方向 / 重启)
        if is_new_direction or session.get("status") in ["idle", "concluded"]:
            session["phase"] = 1
            session["status"] = "running"
            session["passed_employees"] = []
            session["blocker_under_review"] = None
            session["title"] = content[:20] if len(content) > 3 else session.get("title", "新会议")
        else:
            # 中途纠偏
            session["status"] = "running"

        save_session(session)

    @staticmethod
    def step(session: Dict[str, Any]) -> Tuple[bool, str]:
        """
        推动会议向前走一步。
        返回: (是否发生了动作, 状态描述信息)
        """
        if session.get("status") != "running":
            return False, f"会议处于 {session.get('status')} 状态，无需推进"

        settings = load_settings()
        employee_ids = session.get("employee_ids", [])
        if not employee_ids:
            session["status"] = "concluded"
            save_session(session)
            return False, "没有选定参会成员"

        moderator_id = session.get("moderator_id") or settings.get("moderator_id", employee_ids[0])
        moderator = MeetingEngine.get_employee(settings, moderator_id) or MeetingEngine.get_employee(settings, employee_ids[0])

        phase = session.get("phase", 1)

        # -------------------------------------------------------------
        # 阶段一：自由立论与出谋划策 (Brainstorm / Opening)
        # -------------------------------------------------------------
        if phase == 1:
            # 找到在阶段一中还未发言的成员
            spoken_in_p1 = set()
            # 获取最后一次用户发言之后的消息
            last_user_idx = -1
            for idx, m in enumerate(session["messages"]):
                if m["sender"] == "user":
                    last_user_idx = idx

            relevant_msgs = session["messages"][last_user_idx + 1:] if last_user_idx != -1 else session["messages"]
            for m in relevant_msgs:
                if m.get("phase") == 1 and m["sender"] in employee_ids:
                    spoken_in_p1.add(m["sender"])

            # 寻找下一位未发言者
            next_emp_id = None
            for eid in employee_ids:
                if eid not in spoken_in_p1:
                    next_emp_id = eid
                    break

            if next_emp_id:
                # 还有人没立论，呼叫该成员发言
                emp = MeetingEngine.get_employee(settings, next_emp_id)
                user_topic = session["messages"][last_user_idx]["content"] if last_user_idx != -1 else "技术架构设计"

                # 收集其他人已有的立论
                p1_context = []
                for m in relevant_msgs:
                    if m.get("phase") == 1:
                        p1_context.append(f"[{m['sender_name']}]: {m['content']}")
                context_str = "\n".join(p1_context) if p1_context else "(你是第一个发言人)"

                prompt = f"""【会议议题】: {user_topic}
你的身份是【{emp['name']}】({emp['title']})。
人设定位: {emp['system_prompt']}

【其他专家已发表的初步观点】:
{context_str}

【你的任务】:
请基于你的专业视角，直接给出你对该议题的初步架构思路、关键技术选型与核心考量。
要求：
1. 直奔核心，言简意赅，控制在 100~200 字；
2. 保持独立思考，提出符合你专长定位的独特视角。"""

                session["current_speaker"] = emp["name"]
                save_session(session)

                reply, elapsed = Executor.run_employee(emp, prompt)
                session["current_speaker"] = None

                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": emp["id"],
                    "sender_name": emp["name"],
                    "content": reply,
                    "phase": 1,
                    "verdict": "OPINION",
                    "elapsed_time": elapsed,
                    "created_at": int(time.time())
                })
                save_session(session)
                return True, f"[{emp['name']}] 完成了初步立论 (用时 {elapsed}s)"

            else:
                # 阶段一所有人立论完成 ➡️ 调度器合成首版方案草案，切入阶段二！
                user_topic = session["messages"][last_user_idx]["content"] if last_user_idx != -1 else "技术架构设计"
                p1_context = [f"[{m['sender_name']}]: {m['content']}" for m in relevant_msgs if m.get("phase") == 1]
                context_str = "\n".join(p1_context)

                synth_prompt = f"""【技术方案草案合成任务】
议题: "{user_topic}"
各专家初步立论汇总如下：
{context_str}

你是会议主持人兼总架构师【{moderator['name']}】。
请综合各方专家的共识与合理建议，输出一份标准 Markdown 格式的《技术方案架构设计草案 (v1)》。
要求：
- 结构清晰，包含：1. 总体设计原则; 2. 核心架构与技术选型; 3. 关键实现流程; 4. 潜在风险点。
- 输出纯 Markdown 内容，便于后续各专家进行代码级与架构级审查。"""

                session["current_speaker"] = f"{moderator['name']} (合成草案)"
                save_session(session)

                draft_doc, elapsed = Executor.run_employee(moderator, synth_prompt)
                session["current_speaker"] = None
                session["document"] = draft_doc
                session["phase"] = 2
                session["passed_employees"] = []

                # 系统广播通知
                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": "system",
                    "sender_name": "系统广播",
                    "content": "📋 调度器已根据各位专家的立论合成首版《架构方案草案》，现已进入【阶段二：严格审查与重大阻断质询】！",
                    "phase": 2,
                    "verdict": "SYSTEM",
                    "elapsed_time": elapsed,
                    "created_at": int(time.time())
                })
                save_session(session)
                return True, "已合成首版方案草案，进入阶段二"

        # -------------------------------------------------------------
        # 阶段二：严格审查与重大阻断阶段 (Strict Review & Blocker Fix)
        # -------------------------------------------------------------
        elif phase == 2:
            blocker = session.get("blocker_under_review")

            # 子分支 A: 存在等待验收的阻断问题
            if blocker:
                raiser_id = blocker.get("raised_by")
                raiser_emp = MeetingEngine.get_employee(settings, raiser_id) or moderator
                issue_text = blocker.get("issue", "")

                # 调度器针对该缺陷进行文档修改
                fix_prompt = f"""【阻断缺陷修复任务】
当前方案草案如下：
{session['document']}

专家【{raiser_emp['name']}】指出了以下致命缺陷：
"{issue_text}"

请对当前方案进行针对性修订以消除该致命缺陷。
请直接输出修订后的完整 Markdown 方案文档，保持专业严谨。"""

                session["current_speaker"] = f"{moderator['name']} (正在修复阻断缺陷)"
                save_session(session)

                updated_doc, elapsed_fix = Executor.run_employee(moderator, fix_prompt)
                session["document"] = updated_doc

                # 接着让提意见的专家做封闭式验收
                verify_prompt = f"""【阻断缺陷闭环验收】
你刚才指出的致命阻断缺陷为: "{issue_text}"
调度器已根据你的意见更新了方案文档（最新文档已更新在共享面板）。

【封闭式验收要求】:
请仅针对修改是否消除了你指出的致命缺陷进行判定。
回答第一行必须严格为以下两者之一：
[ACCEPT] - 致命缺陷已消除，接受方案
[REJECT: 仍有致命缺陷简述] - 仍存在致命隐患
随后用 50~100 字阐述依据。严禁扩散引入不相关的新议题！"""

                session["current_speaker"] = f"{raiser_emp['name']} (验收方案修复)"
                save_session(session)

                verify_reply, elapsed_verify = Executor.run_employee(raiser_emp, verify_prompt)
                session["current_speaker"] = None

                is_accepted = "[ACCEPT]" in verify_reply or "ACCEPT" in verify_reply[:20]

                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": raiser_emp["id"],
                    "sender_name": raiser_emp["name"],
                    "content": verify_reply,
                    "phase": 2,
                    "verdict": "ACCEPT" if is_accepted else "REJECT",
                    "elapsed_time": elapsed_verify,
                    "created_at": int(time.time())
                })

                if is_accepted:
                    # 消除阻断，清空 blocker，继续其他成员审核
                    session["blocker_under_review"] = None
                    session["passed_employees"] = [raiser_id]  # 该专家已通过
                    save_session(session)
                    return True, f"[{raiser_emp['name']}] 验收通过了修订方案"
                else:
                    # 仍有微调要求，可再次更新或标记争议
                    session["blocker_under_review"] = None
                    session["passed_employees"] = []
                    save_session(session)
                    return True, f"[{raiser_emp['name']}] 仍有异议，已记录"

            # 子分支 B: 无未决阻断，让下一位未 passed 的专家审查
            passed = set(session.get("passed_employees", []))
            unpassed_ids = [eid for eid in employee_ids if eid not in passed]

            if not unpassed_ids:
                # 所有参会 AI 全部通过！
                session["status"] = "concluded"
                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": "system",
                    "sender_name": "系统广播",
                    "content": "🎉 全体专家均已审核通过（PASS），无任何重大阻断缺陷！最终方案已定稿，会议圆满闭幕！",
                    "phase": 2,
                    "verdict": "CONCLUDED",
                    "elapsed_time": 0,
                    "created_at": int(time.time())
                })
                save_session(session)
                return True, "全体专家达成一致，会议圆满结束"

            # 挑选下一位专家审查
            reviewer_id = unpassed_ids[0]
            reviewer_emp = MeetingEngine.get_employee(settings, reviewer_id)

            review_prompt = f"""【方案严格审查质询】
你是【{reviewer_emp['name']}】({reviewer_emp['title']})。
人设定位: {reviewer_emp['system_prompt']}

请仔细审阅最新版技术方案：
{session['document']}

【审查铁律】:
1. 严禁无病呻吟与琐碎纠缠！代码注释、命名风格等非致命小瑕疵一律禁止阻断流程；
2. 只有导致系统崩溃、并发死锁、严重安全漏洞或不可逆架构冲突的致命缺陷，才允许标注为 BLOCKER；
3. 你的回答首行必须严格为以下二者之一：
   [PASS] - 方案在你的专业领域无致命漏洞，同意通过
   [BLOCKER: 致命缺陷简述] - 发现不可接受的严重缺陷
4. 随后用 100 字左右阐述关键技术依据。"""

            session["current_speaker"] = reviewer_emp["name"]
            save_session(session)

            reply, elapsed = Executor.run_employee(reviewer_emp, review_prompt)
            session["current_speaker"] = None

            # 解析判定结果
            first_line = reply.strip().split("\n")[0]
            if "[BLOCKER" in first_line or "BLOCKER:" in reply[:30]:
                match = re.search(r"\[?BLOCKER:?\s*([^\]\n]+)\]?", reply)
                issue_desc = match.group(1).strip() if match else "发现严重架构/性能隐患"
                session["blocker_under_review"] = {
                    "raised_by": reviewer_id,
                    "issue": issue_desc,
                    "raw": reply
                }
                session["passed_employees"] = []  # 发生阻断，需重置已通过列表
                verdict = "BLOCKER"
            else:
                passed.add(reviewer_id)
                session["passed_employees"] = list(passed)
                verdict = "PASS"

            session["messages"].append({
                "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                "sender": reviewer_emp["id"],
                "sender_name": reviewer_emp["name"],
                "content": reply,
                "phase": 2,
                "verdict": verdict,
                "elapsed_time": elapsed,
                "created_at": int(time.time())
            })

            # 如果这刚才是最后一个通过的人，则直接宣告结束
            if verdict == "PASS" and len(session["passed_employees"]) == len(employee_ids):
                session["status"] = "concluded"
                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": "system",
                    "sender_name": "系统广播",
                    "content": "🎉 全体专家均已审核通过（PASS），无任何重大阻断缺陷！最终方案已定稿，会议圆满闭幕！",
                    "phase": 2,
                    "verdict": "CONCLUDED",
                    "elapsed_time": 0,
                    "created_at": int(time.time())
                })

            save_session(session)
            return True, f"[{reviewer_emp['name']}] 完成审查，结论: {verdict}"

        return False, "无动作"
