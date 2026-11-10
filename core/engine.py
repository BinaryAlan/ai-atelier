import re
import time
from typing import Dict, Any, Optional, Tuple, List
from core.executor import Executor
from core.storage import load_settings, save_session

class MeetingEngine:
    @staticmethod
    def get_moderator(settings: Dict[str, Any]) -> Dict[str, Any]:
        return settings.get("moderator", {
            "id": "moderator",
            "name": "首席主持 (Antigravity)",
            "title": "会议主持人兼总架构师",
            "avatar": "👑",
            "avatar_color": "#d97706",
            "type": "cli",
            "cli_cmd": 'agy -p "{prompt}"',
            "system_prompt": "你是会议主持人兼全局首席架构师。注重工程规范、模块解耦与可靠性。负责综合立论生成方案草案、协调分歧并出具最终定稿。",
            "timeout": 180
        })

    @staticmethod
    def get_employee(settings: Dict[str, Any], emp_id: str) -> Optional[Dict[str, Any]]:
        # 优先匹配普通员工
        for e in settings.get("employees", []):
            if e["id"] == emp_id:
                return e
        # 匹配主持人
        moderator = MeetingEngine.get_moderator(settings)
        if moderator.get("id") == emp_id:
            return moderator
        return None

    @staticmethod
    def detect_mention(content: str, settings: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if "@所有人" in content or "@all" in content.lower() or "@All" in content:
            return True, None

        # 检查主持人
        mod = MeetingEngine.get_moderator(settings)
        if f"@{mod['name']}" in content or "@主持人" in content:
            return False, mod["id"]

        # 检查普通员工角色
        for emp in settings.get("employees", []):
            if f"@{emp['name']}" in content or f"@{emp['id']}" in content:
                return False, emp["id"]

        return False, None

    @staticmethod
    def user_inject(session: Dict[str, Any], content: str):
        now = int(time.time())
        settings = load_settings()

        msg = {
            "id": f"msg_{now}_{len(session['messages'])}",
            "sender": "user",
            "sender_name": "群主 (你)",
            "content": content,
            "phase": session.get("phase", 1),
            "verdict": "USER",
            "elapsed_time": 0,
            "created_at": now,
            "sent_prompt": content,
            "raw_response": content
        }
        session["messages"].append(msg)

        is_all, specific_emp_id = MeetingEngine.detect_mention(content, settings)

        if specific_emp_id:
            session["target_direct_mention"] = specific_emp_id
            session["status"] = "running"
        elif is_all:
            session["target_direct_mention"] = None
            session["phase"] = 1
            session["status"] = "running"
            session["passed_employees"] = []
            session["blocker_under_review"] = None
        else:
            session["target_direct_mention"] = None
            if session.get("status") in ["idle", "concluded"]:
                session["phase"] = 1
                session["status"] = "running"
                session["passed_employees"] = []
                session["blocker_under_review"] = None
            else:
                session["status"] = "running"

        save_session(session)

    @staticmethod
    def step(session: Dict[str, Any]) -> Tuple[bool, str]:
        if session.get("status") != "running":
            return False, f"会议处于 {session.get('status')} 状态，无需推进"

        settings = load_settings()
        moderator = MeetingEngine.get_moderator(settings)
        workers = session.get("employee_ids", [])
        if not workers:
            session["status"] = "concluded"
            save_session(session)
            return False, "没有选定参会成员"

        session_id = session.get("id", "default")
        workspace_dir = session.get("workspace_dir", "")

        # -------------------------------------------------------------
        # 1. 针对特定员工/主持人的点名直接应答 (单挑问答，完成后即停)
        # -------------------------------------------------------------
        direct_target = session.get("target_direct_mention")
        if direct_target:
            target_emp = MeetingEngine.get_employee(settings, direct_target)
            if target_emp:
                last_user_msg = ""
                for m in reversed(session["messages"]):
                    if m["sender"] == "user":
                        last_user_msg = m["content"]
                        break

                prompt = f"""【群内用户指令】: {last_user_msg}

你是【{target_emp['name']}】({target_emp['title']})。
人设定位: {target_emp['system_prompt']}
本地工作目录: {workspace_dir}

请直接回答群主的上述要求。言简意赅，发挥你的专业特长。"""

                session["current_speaker"] = target_emp["name"]
                save_session(session)

                reply, elapsed = Executor.run_employee(target_emp, prompt, session_id=session_id)
                session["current_speaker"] = None
                session["target_direct_mention"] = None
                session["status"] = "idle"

                is_timeout = "【超时未能及时回复" in reply
                is_error = "【CLI 执行报错" in reply or "【执行异常" in reply
                verdict = "TIMEOUT" if is_timeout else ("ERROR" if is_error else "DIRECT")

                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": target_emp["id"],
                    "sender_name": target_emp["name"],
                    "content": reply,
                    "phase": session.get("phase", 1),
                    "verdict": verdict,
                    "elapsed_time": elapsed,
                    "created_at": int(time.time()),
                    "sent_prompt": prompt,
                    "raw_response": reply
                })
                save_session(session)
                return True, f"[{target_emp['name']}] 已完成直接应答"

        phase = session.get("phase", 1)

        # -------------------------------------------------------------
        # 2. 阶段一：参会员工自由立论 (Brainstorm / Opening)
        # -------------------------------------------------------------
        if phase == 1:
            spoken_in_p1 = set()
            last_user_idx = -1
            for idx, m in enumerate(session["messages"]):
                if m["sender"] == "user":
                    last_user_idx = idx

            relevant_msgs = session["messages"][last_user_idx + 1:] if last_user_idx != -1 else session["messages"]
            for m in relevant_msgs:
                if m.get("phase") == 1 and m["sender"] in workers:
                    spoken_in_p1.add(m["sender"])

            next_emp_id = None
            for eid in workers:
                if eid not in spoken_in_p1:
                    next_emp_id = eid
                    break

            if next_emp_id:
                emp = MeetingEngine.get_employee(settings, next_emp_id)
                user_topic = session["messages"][last_user_idx]["content"] if last_user_idx != -1 else "技术架构设计"

                p1_context = [f"[{m['sender_name']}]: {m['content']}" for m in relevant_msgs if m.get("phase") == 1]
                context_str = "\n".join(p1_context) if p1_context else "(你是第一个发言人)"

                prompt = f"""【会议议题】: {user_topic}
本地工作目录: {workspace_dir}
你的身份是【{emp['name']}】({emp['title']})。
人设定位: {emp['system_prompt']}

【其他专家已发表的初步观点】:
{context_str}

【你的任务】:
请基于你的专业视角，直接给出你对该议题的初步架构思路、关键技术选型与核心考量。
要求：直奔核心，言简意赅，控制在 100~200 字。"""

                session["current_speaker"] = emp["name"]
                save_session(session)

                reply, elapsed = Executor.run_employee(emp, prompt, session_id=session_id)
                session["current_speaker"] = None

                is_timeout = "【超时未能及时回复" in reply
                is_error = "【CLI 执行报错" in reply or "【执行异常" in reply
                verdict = "TIMEOUT" if is_timeout else ("ERROR" if is_error else "OPINION")

                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": emp["id"],
                    "sender_name": emp["name"],
                    "content": reply,
                    "phase": 1,
                    "verdict": verdict,
                    "elapsed_time": elapsed,
                    "created_at": int(time.time()),
                    "sent_prompt": prompt,
                    "raw_response": reply
                })

                if is_timeout or is_error:
                    now = int(time.time())
                    session["messages"].append({
                        "id": f"msg_mod_fallback_{now}",
                        "sender": moderator["id"],
                        "sender_name": f"{moderator['name']} (主持人)",
                        "content": f"⚠️ 参会专家 [{emp['name']}] {'响应超时' if is_timeout else '执行报错'}。主持人已记录，暂不影响其他成员继续立论。",
                        "phase": 1,
                        "verdict": "FALLBACK",
                        "elapsed_time": 0,
                        "created_at": now,
                        "sent_prompt": "",
                        "raw_response": ""
                    })

                save_session(session)
                return True, f"[{emp['name']}] 阶段一立论完毕"

            else:
                # 阶段一全部打工仔立论完毕 ➡️ 主持人合成首版草案，切入阶段二！
                user_topic = session["messages"][last_user_idx]["content"] if last_user_idx != -1 else "技术架构设计"
                p1_context = [f"[{m['sender_name']}]: {m['content']}" for m in relevant_msgs if m.get("phase") == 1 and m.get("verdict") == "OPINION"]
                context_str = "\n".join(p1_context)

                synth_prompt = f"""【技术方案草案合成任务】
议题: "{user_topic}"
本地工作目录: {workspace_dir}
各专家初步立论汇总如下：
{context_str}

你是会议主持人兼总架构师【{moderator['name']}】。
请综合各方专家的共识与合理建议，输出一份标准 Markdown 格式的《技术方案架构设计草案 (v1)》。
要求：
- 结构清晰，包含：1. 总体设计原则; 2. 核心架构与技术选型; 3. 关键实现流程; 4. 潜在风险点。
- 输出纯 Markdown 内容，便于后续各专家进行严格审查。"""

                session["current_speaker"] = f"{moderator['name']} (主持人正在合成草案)"
                save_session(session)

                draft_doc, elapsed = Executor.run_employee(moderator, synth_prompt, session_id=session_id)
                session["current_speaker"] = None
                session["document"] = draft_doc
                session["phase"] = 2
                session["passed_employees"] = []

                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": moderator["id"],
                    "sender_name": f"{moderator['name']} (主持人)",
                    "content": f"📋 主持人已综合大家立论合成首版方案草案（已更新在右侧面板）。现正式进入【阶段二：严格审查与重大阻断质询】！",
                    "phase": 2,
                    "verdict": "SYNTHESIS",
                    "elapsed_time": elapsed,
                    "created_at": int(time.time()),
                    "sent_prompt": synth_prompt,
                    "raw_response": draft_doc
                })
                save_session(session)
                return True, "主持人已合成首版方案草案，进入阶段二"

        # -------------------------------------------------------------
        # 3. 阶段二：严格审查与重大阻断阶段 (Strict Review & Blocker Fix)
        # -------------------------------------------------------------
        elif phase == 2:
            blocker = session.get("blocker_under_review")

            # 子分支 A: 存在等待验收的阻断缺陷
            if blocker:
                raiser_id = blocker.get("raised_by")
                raiser_emp = MeetingEngine.get_employee(settings, raiser_id) or moderator
                issue_text = blocker.get("issue", "")

                fix_prompt = f"""【阻断缺陷修复任务】
当前方案草案如下：
{session['document']}

专家【{raiser_emp['name']}】指出了以下致命缺陷：
"{issue_text}"

请对当前方案进行针对性修订以消除该致命缺陷。
请直接输出修订后的完整 Markdown 方案文档。"""

                session["current_speaker"] = f"{moderator['name']} (主持人正在修复阻断)"
                save_session(session)

                updated_doc, elapsed_fix = Executor.run_employee(moderator, fix_prompt, session_id=session_id)
                session["document"] = updated_doc

                verify_prompt = f"""【阻断缺陷闭环验收】
你刚才指出的致命阻断缺陷为: "{issue_text}"
主持人已根据你的意见更新了方案文档（最新文档已更新在右侧共享面板）。

【封闭式验收要求】:
请仅针对修改是否消除了你指出的致命缺陷进行判定。
回答第一行必须严格为以下两者之一：
[ACCEPT] - 致命缺陷已消除，接受方案
[REJECT: 仍有致命缺陷简述] - 仍存在致命隐患
随后用 50~100 字阐述依据。严禁扩散引入不相关的新议题！"""

                session["current_speaker"] = f"{raiser_emp['name']} (正在验收方案修复)"
                save_session(session)

                verify_reply, elapsed_verify = Executor.run_employee(raiser_emp, verify_prompt, session_id=session_id)
                session["current_speaker"] = None

                is_timeout = "【超时未能及时回复" in verify_reply
                is_error = "【CLI 执行报错" in verify_reply or "【执行异常" in verify_reply

                if is_timeout or is_error:
                    session["messages"].append({
                        "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                        "sender": raiser_emp["id"],
                        "sender_name": raiser_emp["name"],
                        "content": verify_reply,
                        "phase": 2,
                        "verdict": "TIMEOUT" if is_timeout else "ERROR",
                        "elapsed_time": elapsed_verify,
                        "created_at": int(time.time()),
                        "sent_prompt": verify_prompt,
                        "raw_response": verify_reply
                    })
                    session["blocker_under_review"] = None
                    session["passed_employees"] = [raiser_id]
                    
                    now = int(time.time())
                    session["messages"].append({
                        "id": f"msg_mod_accept_fallback_{now}",
                        "sender": moderator["id"],
                        "sender_name": f"{moderator['name']} (主持人)",
                        "content": f"⚠️ 专家 [{raiser_emp['name']}] 验收回复超时。主持人复核该修改有效，默认予以裁定通过，继续推进后续审查。",
                        "phase": 2,
                        "verdict": "FALLBACK",
                        "elapsed_time": 0,
                        "created_at": now,
                        "sent_prompt": "",
                        "raw_response": ""
                    })
                    save_session(session)
                    return True, "验收超时，主持人介入兜底"

                is_accepted = "[ACCEPT]" in verify_reply or "ACCEPT" in verify_reply[:20]
                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": raiser_emp["id"],
                    "sender_name": raiser_emp["name"],
                    "content": verify_reply,
                    "phase": 2,
                    "verdict": "ACCEPT" if is_accepted else "REJECT",
                    "elapsed_time": elapsed_verify,
                    "created_at": int(time.time()),
                    "sent_prompt": verify_prompt,
                    "raw_response": verify_reply
                })

                if is_accepted:
                    session["blocker_under_review"] = None
                    session["passed_employees"] = [raiser_id]
                    save_session(session)
                    return True, f"[{raiser_emp['name']}] 验收通过了修订方案"
                else:
                    session["blocker_under_review"] = None
                    session["passed_employees"] = []
                    save_session(session)
                    return True, f"[{raiser_emp['name']}] 仍有异议已记录"

            # 子分支 B: 无未决阻断，让下一位打工仔专家审查
            passed = set(session.get("passed_employees", []))
            unpassed_ids = [eid for eid in workers if eid not in passed]

            if not unpassed_ids:
                session["status"] = "concluded"
                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": moderator["id"],
                    "sender_name": f"{moderator['name']} (主持人)",
                    "content": "🎉 全体参会专家均已审核通过（PASS），无任何重大阻断缺陷！最终方案已定稿，会议圆满闭幕！",
                    "phase": 2,
                    "verdict": "CONCLUDED",
                    "elapsed_time": 0,
                    "created_at": int(time.time()),
                    "sent_prompt": "",
                    "raw_response": ""
                })
                save_session(session)
                return True, "全体专家达成一致，会议圆满结束"

            reviewer_id = unpassed_ids[0]
            reviewer_emp = MeetingEngine.get_employee(settings, reviewer_id)

            review_prompt = f"""【方案严格审查质询】
你是【{reviewer_emp['name']}】({reviewer_emp['title']})。
人设定位: {reviewer_emp['system_prompt']}
本地工作目录: {workspace_dir}

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

            reply, elapsed = Executor.run_employee(reviewer_emp, review_prompt, session_id=session_id)
            session["current_speaker"] = None

            is_timeout = "【超时未能及时回复" in reply
            is_error = "【CLI 执行报错" in reply or "【执行异常" in reply

            if is_timeout or is_error:
                verdict = "TIMEOUT" if is_timeout else "ERROR"
                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": reviewer_emp["id"],
                    "sender_name": reviewer_emp["name"],
                    "content": reply,
                    "phase": 2,
                    "verdict": verdict,
                    "elapsed_time": elapsed,
                    "created_at": int(time.time()),
                    "sent_prompt": review_prompt,
                    "raw_response": reply
                })

                now = int(time.time())
                session["messages"].append({
                    "id": f"msg_mod_review_fallback_{now}",
                    "sender": moderator["id"],
                    "sender_name": f"{moderator['name']} (主持人)",
                    "content": f"⚠️ 参会专家 [{reviewer_emp['name']}] {'审查超时' if is_timeout else '执行报错'}。主持人已介入记录，暂时跳过该专家的本轮质询，会议继续推进。",
                    "phase": 2,
                    "verdict": "FALLBACK",
                    "elapsed_time": 0,
                    "created_at": now,
                    "sent_prompt": "",
                    "raw_response": ""
                })
                passed.add(reviewer_id)
                session["passed_employees"] = list(passed)

                if len(session["passed_employees"]) == len(workers):
                    session["status"] = "concluded"
                    session["messages"].append({
                        "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                        "sender": moderator["id"],
                        "sender_name": f"{moderator['name']} (主持人)",
                        "content": "🎉 全体参会专家均已审查完毕！最终方案已定稿，会议圆满闭幕！",
                        "phase": 2,
                        "verdict": "CONCLUDED",
                        "elapsed_time": 0,
                        "created_at": int(time.time()),
                        "sent_prompt": "",
                        "raw_response": ""
                    })

                save_session(session)
                return True, f"[{reviewer_emp['name']}] 审查超时，主持人已接管继续"

            first_line = reply.strip().split("\n")[0]
            if "[BLOCKER" in first_line or "BLOCKER:" in reply[:30]:
                match = re.search(r"\[?BLOCKER:?\s*([^\]\n]+)\]?", reply)
                issue_desc = match.group(1).strip() if match else "发现严重架构/性能隐患"
                session["blocker_under_review"] = {
                    "raised_by": reviewer_id,
                    "issue": issue_desc,
                    "raw": reply
                }
                session["passed_employees"] = []
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
                "created_at": int(time.time()),
                "sent_prompt": review_prompt,
                "raw_response": reply
            })

            if verdict == "PASS" and len(session["passed_employees"]) == len(workers):
                session["status"] = "concluded"
                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": moderator["id"],
                    "sender_name": f"{moderator['name']} (主持人)",
                    "content": "🎉 全体参会专家均已审核通过（PASS），无任何重大阻断缺陷！最终方案已定稿，会议圆满闭幕！",
                    "phase": 2,
                    "verdict": "CONCLUDED",
                    "elapsed_time": 0,
                    "created_at": int(time.time()),
                    "sent_prompt": "",
                    "raw_response": ""
                })

            save_session(session)
            return True, f"[{reviewer_emp['name']}] 完成审查，结论: {verdict}"

        return False, "无动作"
