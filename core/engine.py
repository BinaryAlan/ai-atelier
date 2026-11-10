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
            "name": "会议主持人",
            "title": "会议主持人",
            "avatar": "👑",
            "avatar_color": "#d97706",
            "type": "cli",
            "cli_cmd": 'agy -p "{prompt}"',
            "system_prompt": "你是会议主持人。负责主持会议进程、客观梳理各方意见、汇总生成方案草案，并在全员达成共识时出具终稿文档。",
            "timeout": 180
        })

    @staticmethod
    def get_employee(settings: Dict[str, Any], emp_id: str) -> Optional[Dict[str, Any]]:
        for e in settings.get("employees", []):
            if e["id"] == emp_id:
                return e
        moderator = MeetingEngine.get_moderator(settings)
        if moderator.get("id") == emp_id:
            return moderator
        return None

    @staticmethod
    def detect_mention(content: str, settings: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if "@所有人" in content or "@all" in content.lower() or "@All" in content:
            return True, None

        mod = MeetingEngine.get_moderator(settings)
        if f"@{mod['name']}" in content or "@主持人" in content:
            return False, mod["id"]

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
            session["status"] = "idle"
            save_session(session)
            return False, "没有选定参会成员"

        session_id = session.get("id", "default")
        workspace_dir = session.get("workspace_dir", "")

        # -------------------------------------------------------------
        # 1. 针对特定成员的点名直接应答 (定向呼叫，完成后转为等待)
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

                prompt = f"""【会议用户指令】: {last_user_msg}

你是【{target_emp['name']}】({target_emp.get('title', '参会专家')})。
人设背景: {target_emp.get('system_prompt', '')}
当前工作目录: {workspace_dir}

请直接回应群主的上述要求。请保持专业、客观、务实，直奔核心。"""

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
        # 2. 阶段一：参会专家自由发表意见 (Brainstorm / Opening)
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
                user_topic = session["messages"][last_user_idx]["content"] if last_user_idx != -1 else "方案讨论"

                p1_context = [f"[{m['sender_name']}]: {m['content']}" for m in relevant_msgs if m.get("phase") == 1]
                context_str = "\n".join(p1_context) if p1_context else "(你是第一个发言人)"

                prompt = f"""【会议议题】: {user_topic}
本地工作目录: {workspace_dir}
你的身份是【{emp['name']}】({emp.get('title', '参会专家')})。
人设定位: {emp.get('system_prompt', '')}

【其他参会人已发表的初步观点】:
{context_str}

【你的任务】:
请基于你的专业视角，直接给出你对该议题的核心见解、关键思路与落地建议。
要求：
1. 语言精炼，务实客观，控制在 100~200 字左右；
2. 提出有建设性的方案要点，不讲套话。"""

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
                    # 如果超时或报错，挂起会议等待群主介入
                    session["status"] = "paused"
                    session["messages"].append({
                        "id": f"msg_mod_fallback_{int(time.time())}",
                        "sender": moderator["id"],
                        "sender_name": f"{moderator['name']} (主持人)",
                        "content": f"⚠️ 参会人 [{emp['name']}] {'响应超时未回复' if is_timeout else '执行报错'}。会议已自动挂起，等待群主介入处理。",
                        "phase": 1,
                        "verdict": "PAUSE",
                        "elapsed_time": 0,
                        "created_at": int(time.time()),
                        "sent_prompt": "",
                        "raw_response": ""
                    })
                    save_session(session)
                    return True, f"[{emp['name']}] 阶段一立论超时，已挂起"

                save_session(session)
                return True, f"[{emp['name']}] 阶段一发言完毕"

            else:
                # 阶段一全部人发表完毕 ➡️ 主持人合成首版草案文档
                user_topic = session["messages"][last_user_idx]["content"] if last_user_idx != -1 else "方案讨论"
                p1_context = [f"[{m['sender_name']}]: {m['content']}" for m in relevant_msgs if m.get("phase") == 1 and m.get("verdict") == "OPINION"]
                context_str = "\n".join(p1_context)

                synth_prompt = f"""【会议纪要与方案草案整理任务】
议题: "{user_topic}"
工作目录: {workspace_dir}
各专家初步发言汇总如下：
{context_str}

你是【{moderator['name']}】。
请综合各方建议与共识，输出一份标准的《方案草案与工作纪要 (初稿)》。
要求：
- 结构清晰、条理规整，涵盖核心结论、执行要点与待办细节；
- 输出纯 Markdown 格式。"""

                session["current_speaker"] = f"{moderator['name']} (主持人正在整理草案)"
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
                    "content": f"📋 主持人已综合整理首版方案纪要（已更新在右侧面板）。现正式进入【阶段二：审阅与隐患质询】！",
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
        # 3. 阶段二：方案审阅与重大隐患质询
        # -------------------------------------------------------------
        elif phase == 2:
            blocker = session.get("blocker_under_review")

            # 子分支 A: 存在等待验收的阻断问题
            if blocker:
                raiser_id = blocker.get("raised_by")
                raiser_emp = MeetingEngine.get_employee(settings, raiser_id) or moderator
                issue_text = blocker.get("issue", "")

                fix_prompt = f"""【方案修订任务】
当前方案草案如下：
{session['document']}

参会人【{raiser_emp['name']}】指出了以下重大问题：
"{issue_text}"

请对方案草案进行针对性修订以解决该问题。
请直接输出修订后的完整 Markdown 方案文档。"""

                session["current_speaker"] = f"{moderator['name']} (主持人正在修改草案)"
                save_session(session)

                updated_doc, elapsed_fix = Executor.run_employee(moderator, fix_prompt, session_id=session_id)
                session["document"] = updated_doc

                verify_prompt = f"""【方案修订闭环验收】
你刚才指出的重大问题为: "{issue_text}"
主持人已根据你的建议更新了方案草案（最新文档见右侧面板）。

【封闭式验收要求】:
请仅针对修改是否解决了你指出的核心问题进行判定。
回答第一行必须严格为以下两者之一：
[ACCEPT] - 核心问题已解决，接受修改
[REJECT: 仍有问题简述] - 仍存在严重漏洞
随后用 50~100 字阐述依据。严禁扩散引入不相关的新议题！"""

                session["current_speaker"] = f"{raiser_emp['name']} (正在验收方案修改)"
                save_session(session)

                verify_reply, elapsed_verify = Executor.run_employee(raiser_emp, verify_prompt, session_id=session_id)
                session["current_speaker"] = None

                is_timeout = "【超时未能及时回复" in verify_reply
                is_error = "【CLI 执行报错" in verify_reply or "【执行异常" in verify_reply

                if is_timeout or is_error:
                    # 关键修复：超时/报错绝不能判定为通过！
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
                    session["status"] = "paused"
                    session["messages"].append({
                        "id": f"msg_mod_pause_{int(time.time())}",
                        "sender": moderator["id"],
                        "sender_name": f"{moderator['name']} (主持人)",
                        "content": f"⚠️ 参会人 [{raiser_emp['name']}] 验收回复超时。流程已自动暂停，等待群主决策。",
                        "phase": 2,
                        "verdict": "PAUSE",
                        "elapsed_time": 0,
                        "created_at": int(time.time()),
                        "sent_prompt": "",
                        "raw_response": ""
                    })
                    save_session(session)
                    return True, "验收回复超时，已挂起流程"

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

            # 子分支 B: 无未决问题，让下一位参会人审阅
            passed = set(session.get("passed_employees", []))
            unpassed_ids = [eid for eid in workers if eid not in passed]

            if not unpassed_ids:
                # 真正全员通过！平稳结案，留出后续交流空间
                session["status"] = "idle"
                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": moderator["id"],
                    "sender_name": f"{moderator['name']} (主持人)",
                    "content": "📋 本轮讨论方案全员已审阅完成，最新共识已整理在右侧面板。群主如有新想法或要调整方向，可随时输入新消息继续讨论。",
                    "phase": 2,
                    "verdict": "CONCLUDED",
                    "elapsed_time": 0,
                    "created_at": int(time.time()),
                    "sent_prompt": "",
                    "raw_response": ""
                })
                save_session(session)
                return True, "全体审阅完成，转为空闲等待"

            reviewer_id = unpassed_ids[0]
            reviewer_emp = MeetingEngine.get_employee(settings, reviewer_id)

            review_prompt = f"""【方案客观审阅】
你是【{reviewer_emp['name']}】({reviewer_emp.get('title', '参会专家')})。
人设背景: {reviewer_emp.get('system_prompt', '')}
当前工作目录: {workspace_dir}

请仔细审阅最新版方案草案：
{session['document']}

【审阅原则】:
1. 保持客观理性与专业严谨。细微措辞等非原则性小建议不作为阻断项；
2. 只有发现存在严重逻辑漏洞、重大事实硬伤或不可行隐患，才允许标注为 BLOCKER；
3. 你的回答首行必须严格为以下二者之一：
   [PASS] - 方案整体可行、无严重漏洞，同意通过
   [BLOCKER: 问题简述] - 发现不可接受的严重问题
4. 随后用 100 字左右客观阐述理由与改进建议。"""

            session["current_speaker"] = reviewer_emp["name"]
            save_session(session)

            reply, elapsed = Executor.run_employee(reviewer_emp, review_prompt, session_id=session_id)
            session["current_speaker"] = None

            is_timeout = "【超时未能及时回复" in reply
            is_error = "【CLI 执行报错" in reply or "【执行异常" in reply

            if is_timeout or is_error:
                # 关键修复：绝对不将未回复者加入 passed！而是暂停并提醒群主
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

                session["status"] = "paused"
                now = int(time.time())
                session["messages"].append({
                    "id": f"msg_mod_pause_{now}",
                    "sender": moderator["id"],
                    "sender_name": f"{moderator['name']} (主持人)",
                    "content": f"⚠️ 参会人 [{reviewer_emp['name']}] {'审阅超时未回复' if is_timeout else '执行报错'}。会议已自动挂起，等待群主决策（可点击右上角恢复重试，或直接输入指令纠偏）。",
                    "phase": 2,
                    "verdict": "PAUSE",
                    "elapsed_time": 0,
                    "created_at": now,
                    "sent_prompt": "",
                    "raw_response": ""
                })
                save_session(session)
                return True, f"[{reviewer_emp['name']}] 审阅超时，流程已暂停"

            # 正常回复的判断
            first_line = reply.strip().split("\n")[0]
            if "[BLOCKER" in first_line or "BLOCKER:" in reply[:30]:
                match = re.search(r"\[?BLOCKER:?\s*([^\]\n]+)\]?", reply)
                issue_desc = match.group(1).strip() if match else "发现严重方案隐患"
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
                session["status"] = "idle"
                session["messages"].append({
                    "id": f"msg_{int(time.time())}_{len(session['messages'])}",
                    "sender": moderator["id"],
                    "sender_name": f"{moderator['name']} (主持人)",
                    "content": "📋 本轮讨论方案全员已审阅完成，最新共识已整理在右侧面板。群主如有新想法或要调整方向，可随时输入新消息继续讨论。",
                    "phase": 2,
                    "verdict": "CONCLUDED",
                    "elapsed_time": 0,
                    "created_at": int(time.time()),
                    "sent_prompt": "",
                    "raw_response": ""
                })

            save_session(session)
            return True, f"[{reviewer_emp['name']}] 完成审阅，结论: {verdict}"

        return False, "无动作"
