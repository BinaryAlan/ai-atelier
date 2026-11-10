import re
import time
import uuid
from typing import Dict, Any, Optional, Tuple, List
from core.executor import Executor, ExecutionResult
from core.storage import load_settings, save_session, get_session

def generate_msg_id() -> str:
    """生成防碰撞的唯一消息 ID"""
    return f"msg_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

class MeetingEngine:
    @staticmethod
    def get_moderator(settings: Dict[str, Any]) -> Dict[str, Any]:
        mod = settings.get("moderator")
        if isinstance(mod, dict) and mod.get("id"):
            return mod
        return {
            "id": "moderator",
            "name": "Antigravity",
            "avatar": "👑",
            "avatar_color": "#d97706",
            "type": "cli",
            "cli_cmd": 'agy -p "{prompt}"',
            "system_prompt": "你是会议主持人。负责中立客观地主持会议进程、梳理记录各方发言要点、综合整理方案纪要，并在全员达成共识时出具定稿报告。",
            "timeout": 180
        }

    @staticmethod
    def get_employee(settings: Dict[str, Any], emp_id: str) -> Dict[str, Any]:
        """
        根据 ID 获取员工配置。若员工被删除或 ID 历史变更，返回安全兜底对象，绝不抛出异常。
        """
        moderator = MeetingEngine.get_moderator(settings)
        if emp_id in ["moderator", moderator.get("id"), "antigravity_moderator"]:
            return moderator

        for e in settings.get("employees", []):
            if e.get("id") == emp_id:
                return e

        # 历史别名兼容
        if emp_id == "antigravity":
            for e in settings.get("employees", []):
                if e.get("id") == "emp_agy":
                    return e
            return moderator

        # 兜底对象（防止历史会话因角色删除而崩溃）
        return {
            "id": emp_id,
            "name": emp_id,
            "avatar": "🤖",
            "avatar_color": "#64748b",
            "type": "cli",
            "cli_cmd": "",
            "system_prompt": "",
            "enabled": False,
            "timeout": 180
        }

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
            "id": generate_msg_id(),
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
            session["pre_mention_status"] = session.get("status", "idle")
            session["status"] = "running"
        elif is_all:
            session["target_direct_mention"] = None
            session["phase"] = 1
            session["status"] = "running"
            session["passed_employees"] = []
            session["blocker_under_review"] = None
            session["blocker_retry_count"] = 0
        else:
            session["target_direct_mention"] = None
            if session.get("status") in ["idle", "concluded"]:
                session["phase"] = 1
                session["status"] = "running"
                session["passed_employees"] = []
                session["blocker_under_review"] = None
                session["blocker_retry_count"] = 0
            else:
                session["status"] = "running"

        save_session(session)

    @staticmethod
    def _create_pause_message(moderator_id: str, moderator_name: str, phase: int, text: str) -> Dict[str, Any]:
        return {
            "id": generate_msg_id(),
            "sender": moderator_id,
            "sender_name": f"{moderator_name} (主持人)",
            "content": text,
            "phase": phase,
            "verdict": "PAUSE",
            "elapsed_time": 0,
            "created_at": int(time.time()),
            "sent_prompt": "",
            "raw_response": ""
        }

    @staticmethod
    def _create_concluded_message(moderator_id: str, moderator_name: str) -> Dict[str, Any]:
        return {
            "id": generate_msg_id(),
            "sender": moderator_id,
            "sender_name": f"{moderator_name} (主持人)",
            "content": "📋 本轮讨论方案全员已审阅完成，最新共识已整理在右侧面板。群主如有新想法或要调整方向，可随时输入新消息继续讨论。",
            "phase": 2,
            "verdict": "CONCLUDED",
            "elapsed_time": 0,
            "created_at": int(time.time()),
            "sent_prompt": "",
            "raw_response": ""
        }

    @staticmethod
    def step(session: Dict[str, Any]) -> Tuple[bool, str]:
        session_id = session.get("id", "default")

        # 检查最新持久化状态，若已暂停则立即终止执行
        latest_on_disk = get_session(session_id)
        if latest_on_disk and latest_on_disk.get("status") == "paused":
            session["status"] = "paused"
            session["current_speaker"] = None
            return False, "会议已被暂停"

        if session.get("status") != "running":
            return False, f"会议处于 {session.get('status')} 状态，无需推进"

        settings = load_settings()
        moderator = MeetingEngine.get_moderator(settings)
        workers = session.get("employee_ids", [])
        if not workers:
            session["status"] = "idle"
            save_session(session)
            return False, "没有选定参会成员"

        workspace_dir = session.get("workspace_dir", "")

        # -------------------------------------------------------------
        # 1. 针对特定成员的点名直接应答 (定向呼叫，保留原会议状态)
        # -------------------------------------------------------------
        direct_target = session.get("target_direct_mention")
        if direct_target:
            target_emp = MeetingEngine.get_employee(settings, direct_target)
            last_user_msg = ""
            for m in reversed(session["messages"]):
                if m["sender"] == "user":
                    last_user_msg = m["content"]
                    break

            prompt = f"""【会议群主直接提问/指令】: {last_user_msg}

你是【{target_emp['name']}】。
人设背景: {target_emp.get('system_prompt', '')}
当前工作目录: {workspace_dir}

请直接回应群主的上述要求。请保持客观、专业、务实，直奔核心。"""

            session["current_speaker"] = target_emp["name"]
            save_session(session)

            res = Executor.run_employee(
                target_emp,
                prompt,
                session_id=session_id,
                workspace_dir=workspace_dir
            )

            # 检查执行过程中是否被用户手动暂停
            latest_check = get_session(session_id)
            if res.status == "interrupted" or (latest_check and latest_check.get("status") == "paused"):
                session["status"] = "paused"
                session["current_speaker"] = None
                save_session(session)
                return False, "执行已被用户手动暂停中断"

            session["current_speaker"] = None
            session["target_direct_mention"] = None

            # 恢复点名之前的会议状态
            prev_status = session.pop("pre_mention_status", "idle")
            session["status"] = prev_status if prev_status in ["running", "paused"] else "idle"

            if res.status == "timeout":
                verdict = "TIMEOUT"
            elif res.status == "error":
                verdict = "ERROR"
            else:
                verdict = "DIRECT"

            session["messages"].append({
                "id": generate_msg_id(),
                "sender": target_emp["id"],
                "sender_name": target_emp["name"],
                "content": res.output,
                "phase": session.get("phase", 1),
                "verdict": verdict,
                "elapsed_time": res.elapsed,
                "created_at": int(time.time()),
                "sent_prompt": prompt,
                "raw_response": res.raw_output
            })
            save_session(session)
            return True, f"[{target_emp['name']}] 已完成直接应答"

        phase = session.get("phase", 1)

        # -------------------------------------------------------------
        # 2. 阶段一：参会成员自由发表意见 (Brainstorm / Opening)
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
                user_topic = session["messages"][last_user_idx]["content"] if last_user_idx != -1 else "方案研讨"

                p1_context = [f"[{m['sender_name']}]: {m['content']}" for m in relevant_msgs if m.get("phase") == 1]
                context_str = "\n".join(p1_context) if p1_context else "(你是第一个发言人)"

                prompt = f"""【会议议题】: {user_topic}
本地工作目录: {workspace_dir}
你的身份是【{emp['name']}】。
人设定位: {emp.get('system_prompt', '')}

【其他参会人已发表的初步观点】:
{context_str}

【你的任务】:
请基于你的专业视角，直接给出你对该议题的核心见解、关键思路与落地建议。
要求：
1. 语言精炼，务实客观，控制在 100~200 字左右；
2. 提出有建设性的方案要点，不讲客套话。"""

                session["current_speaker"] = emp["name"]
                save_session(session)

                res = Executor.run_employee(emp, prompt, session_id=session_id, workspace_dir=workspace_dir)

                # 检查暂停中断
                latest_check = get_session(session_id)
                if res.status == "interrupted" or (latest_check and latest_check.get("status") == "paused"):
                    session["status"] = "paused"
                    session["current_speaker"] = None
                    save_session(session)
                    return False, "执行已被用户手动暂停中断"

                session["current_speaker"] = None

                if res.status == "timeout":
                    verdict = "TIMEOUT"
                elif res.status == "error":
                    verdict = "ERROR"
                else:
                    verdict = "OPINION"

                session["messages"].append({
                    "id": generate_msg_id(),
                    "sender": emp["id"],
                    "sender_name": emp["name"],
                    "content": res.output,
                    "phase": 1,
                    "verdict": verdict,
                    "elapsed_time": res.elapsed,
                    "created_at": int(time.time()),
                    "sent_prompt": prompt,
                    "raw_response": res.raw_output
                })

                if res.status in ["timeout", "error"]:
                    session["status"] = "paused"
                    err_desc = "响应超时未回复" if res.status == "timeout" else "执行报错"
                    session["messages"].append(
                        MeetingEngine._create_pause_message(
                            moderator["id"], moderator["name"], 1,
                            f"⚠️ 参会人 [{emp['name']}] {err_desc}。会议已自动挂起，等待群主介入处理。"
                        )
                    )
                    save_session(session)
                    return True, f"[{emp['name']}] 阶段一立论异常，已挂起"

                save_session(session)
                return True, f"[{emp['name']}] 阶段一发言完毕"

            else:
                # 阶段一全部人发表完毕 ➡️ 主持人合成首版草案文档
                user_topic = session["messages"][last_user_idx]["content"] if last_user_idx != -1 else "方案研讨"
                p1_context = [f"[{m['sender_name']}]: {m['content']}" for m in relevant_msgs if m.get("phase") == 1 and m.get("verdict") == "OPINION"]
                context_str = "\n".join(p1_context)

                synth_prompt = f"""【会议纪要与方案草案整理任务】
议题: "{user_topic}"
工作目录: {workspace_dir}
各参会成员初步发言汇总如下：
{context_str}

你是【{moderator['name']}】。
请综合各方建议与共识，输出一份标准的《方案草案与工作纪要 (初稿)》。
要求：
- 结构清晰、条理规整，涵盖核心结论、执行要点与待办细节；
- 输出纯 Markdown 格式。"""

                session["current_speaker"] = f"{moderator['name']} (主持人正在整理草案)"
                save_session(session)

                res = Executor.run_employee(moderator, synth_prompt, session_id=session_id, workspace_dir=workspace_dir)

                latest_check = get_session(session_id)
                if res.status == "interrupted" or (latest_check and latest_check.get("status") == "paused"):
                    session["status"] = "paused"
                    session["current_speaker"] = None
                    save_session(session)
                    return False, "执行已被用户手动暂停中断"

                session["current_speaker"] = None

                if res.status != "success" or len(res.output.strip()) < 20:
                    session["status"] = "paused"
                    session["messages"].append(
                        MeetingEngine._create_pause_message(
                            moderator["id"], moderator["name"], 1,
                            f"⚠️ 主持人整理初稿方案失败 ({res.status})。草案文档保留未覆盖，会议暂停等待人工处理。"
                        )
                    )
                    save_session(session)
                    return True, "主持人整理初稿失败"

                session["document"] = res.output
                session["phase"] = 2
                session["passed_employees"] = []
                session["blocker_retry_count"] = 0

                session["messages"].append({
                    "id": generate_msg_id(),
                    "sender": moderator["id"],
                    "sender_name": f"{moderator['name']} (主持人)",
                    "content": "📋 主持人已综合整理首版方案纪要（已更新在右侧面板）。现正式进入【阶段二：审阅与隐患质询】！",
                    "phase": 2,
                    "verdict": "SYNTHESIS",
                    "elapsed_time": res.elapsed,
                    "created_at": int(time.time()),
                    "sent_prompt": synth_prompt,
                    "raw_response": res.raw_output
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
                raiser_emp = MeetingEngine.get_employee(settings, raiser_id)
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

                res_fix = Executor.run_employee(moderator, fix_prompt, session_id=session_id, workspace_dir=workspace_dir)

                latest_check = get_session(session_id)
                if res_fix.status == "interrupted" or (latest_check and latest_check.get("status") == "paused"):
                    session["status"] = "paused"
                    session["current_speaker"] = None
                    save_session(session)
                    return False, "执行已被用户手动暂停中断"

                if res_fix.status == "success" and len(res_fix.output.strip()) > 20:
                    session["document"] = res_fix.output
                else:
                    session["status"] = "paused"
                    session["current_speaker"] = None
                    session["messages"].append(
                        MeetingEngine._create_pause_message(
                            moderator["id"], moderator["name"], 2,
                            f"⚠️ 主持人修订方案草案失败 ({res_fix.status})。草案未被破坏，流程暂停等待处理。"
                        )
                    )
                    save_session(session)
                    return True, "主持人修订方案失败，暂停流程"

                verify_prompt = f"""【方案修订闭环验收】
你刚才指出的重大问题为: "{issue_text}"
主持人已根据你的建议更新了方案草案（最新文档见右侧面板）。

【封闭式验收要求】:
请仅针对修改是否解决了你指出的核心问题进行判定。
回答第一行必须严格为以下两者之一：
[ACCEPT] - 核心问题已解决，接受修改
[REJECT: 仍有问题简述] - 仍存在严重漏洞
随后用 50~100 字阐述依据。严禁发散引入不相关的新议题！"""

                session["current_speaker"] = f"{raiser_emp['name']} (正在验收方案修改)"
                save_session(session)

                res_verify = Executor.run_employee(raiser_emp, verify_prompt, session_id=session_id, workspace_dir=workspace_dir)

                latest_check = get_session(session_id)
                if res_verify.status == "interrupted" or (latest_check and latest_check.get("status") == "paused"):
                    session["status"] = "paused"
                    session["current_speaker"] = None
                    save_session(session)
                    return False, "执行已被用户手动暂停中断"

                session["current_speaker"] = None

                if res_verify.status in ["timeout", "error"]:
                    verdict = "TIMEOUT" if res_verify.status == "timeout" else "ERROR"
                    session["messages"].append({
                        "id": generate_msg_id(),
                        "sender": raiser_emp["id"],
                        "sender_name": raiser_emp["name"],
                        "content": res_verify.output,
                        "phase": 2,
                        "verdict": verdict,
                        "elapsed_time": res_verify.elapsed,
                        "created_at": int(time.time()),
                        "sent_prompt": verify_prompt,
                        "raw_response": res_verify.raw_output
                    })
                    session["status"] = "paused"
                    session["messages"].append(
                        MeetingEngine._create_pause_message(
                            moderator["id"], moderator["name"], 2,
                            f"⚠️ 参会人 [{raiser_emp['name']}] 验收回复超时或报错。流程已自动暂停，等待群主决策。"
                        )
                    )
                    save_session(session)
                    return True, "验收回复异常，已挂起流程"

                # 严格判定验收结果：检查首行
                first_line = res_verify.output.strip().split("\n")[0].upper()
                is_accepted = "[ACCEPT]" in first_line or first_line.startswith("ACCEPT")

                verdict = "ACCEPT" if is_accepted else "REJECT"

                session["messages"].append({
                    "id": generate_msg_id(),
                    "sender": raiser_emp["id"],
                    "sender_name": raiser_emp["name"],
                    "content": res_verify.output,
                    "phase": 2,
                    "verdict": verdict,
                    "elapsed_time": res_verify.elapsed,
                    "created_at": int(time.time()),
                    "sent_prompt": verify_prompt,
                    "raw_response": res_verify.raw_output
                })

                if is_accepted:
                    session["blocker_under_review"] = None
                    session["passed_employees"] = [raiser_id]
                    session["blocker_retry_count"] = 0
                    save_session(session)
                    return True, f"[{raiser_emp['name']}] 验收通过了修订方案"
                else:
                    # 循环防死锁与收敛保护：单次阻断最多允许 3 次修订尝试
                    retry_count = session.get("blocker_retry_count", 0) + 1
                    session["blocker_retry_count"] = retry_count

                    if retry_count >= 3:
                        session["status"] = "paused"
                        session["messages"].append(
                            MeetingEngine._create_pause_message(
                                moderator["id"], moderator["name"], 2,
                                f"⚠️ 参会人 [{raiser_emp['name']}] 提出的问题已连续修订 {retry_count} 轮仍未达成一致。为防死锁，系统已自动暂停会议，请群主介入裁决或微调方案方向。"
                            )
                        )
                        save_session(session)
                        return True, "达到最大自动修订轮次，暂停等待人工介入"

                    session["blocker_under_review"] = {
                        "raised_by": raiser_id,
                        "issue": f"{issue_text} (第{retry_count}轮反馈: {res_verify.output[:100]})",
                        "raw": res_verify.output
                    }
                    save_session(session)
                    return True, f"[{raiser_emp['name']}] 仍有异议，进入第 {retry_count + 1} 轮修订"

            # 子分支 B: 无未决问题，让下一位参会人审阅
            passed = set(session.get("passed_employees", []))
            unpassed_ids = [eid for eid in workers if eid not in passed]

            if not unpassed_ids:
                session["status"] = "idle"
                session["messages"].append(
                    MeetingEngine._create_concluded_message(moderator["id"], moderator["name"])
                )
                save_session(session)
                return True, "全体审阅完成，转为空闲等待"

            reviewer_id = unpassed_ids[0]
            reviewer_emp = MeetingEngine.get_employee(settings, reviewer_id)

            review_prompt = f"""【方案客观审阅】
你是【{reviewer_emp['name']}】。
人设背景: {reviewer_emp.get('system_prompt', '')}
当前工作目录: {workspace_dir}

请仔细审阅最新版方案草案：
{session['document']}

【审阅原则】:
1. 保持客观理性与专业严谨。细微修辞或非原则性小建议不作为阻断项；
2. 只有发现存在严重逻辑漏洞、重大硬伤或不可行隐患，才允许标注为 BLOCKER；
3. 你的回答首行必须严格为以下二者之一：
   [PASS] - 方案整体可行、无严重漏洞，同意通过
   [BLOCKER: 问题简述] - 发现不可接受的严重问题
4. 随后用 100 字左右客观阐述理由与改进建议。"""

            session["current_speaker"] = reviewer_emp["name"]
            save_session(session)

            res = Executor.run_employee(reviewer_emp, review_prompt, session_id=session_id, workspace_dir=workspace_dir)

            latest_check = get_session(session_id)
            if res.status == "interrupted" or (latest_check and latest_check.get("status") == "paused"):
                session["status"] = "paused"
                session["current_speaker"] = None
                save_session(session)
                return False, "执行已被用户手动暂停中断"

            session["current_speaker"] = None

            if res.status in ["timeout", "error"]:
                verdict = "TIMEOUT" if res.status == "timeout" else "ERROR"
                session["messages"].append({
                    "id": generate_msg_id(),
                    "sender": reviewer_emp["id"],
                    "sender_name": reviewer_emp["name"],
                    "content": res.output,
                    "phase": 2,
                    "verdict": verdict,
                    "elapsed_time": res.elapsed,
                    "created_at": int(time.time()),
                    "sent_prompt": review_prompt,
                    "raw_response": res.raw_output
                })

                session["status"] = "paused"
                err_desc = "审阅超时未回复" if res.status == "timeout" else "执行报错"
                session["messages"].append(
                    MeetingEngine._create_pause_message(
                        moderator["id"], moderator["name"], 2,
                        f"⚠️ 参会人 [{reviewer_emp['name']}] {err_desc}。会议已自动挂起，等待群主决策。"
                    )
                )
                save_session(session)
                return True, f"[{reviewer_emp['name']}] 审阅超时/报错，流程已暂停"

            # 正常回复的判断（严格匹配第一行，杜绝宽泛误判为 PASS）
            first_line = res.output.strip().split("\n")[0]
            if "[BLOCKER" in first_line or "BLOCKER:" in res.output[:40]:
                match = re.search(r"\[?BLOCKER:?\s*([^\]\n]+)\]?", res.output)
                issue_desc = match.group(1).strip() if match else "发现方案严重隐患"
                session["blocker_under_review"] = {
                    "raised_by": reviewer_id,
                    "issue": issue_desc,
                    "raw": res.output
                }
                session["passed_employees"] = []
                verdict = "BLOCKER"
            elif "[PASS]" in first_line or "PASS" in res.output[:20] or "通过" in first_line:
                passed.add(reviewer_id)
                session["passed_employees"] = list(passed)
                verdict = "PASS"
            else:
                # 既不是明确的 BLOCKER 也不是明确的 PASS（答非所问/讨论意见）
                # 决不能放行 PASS，作为建议意见记录，并不加入 passed_employees
                verdict = "COMMENT"

            session["messages"].append({
                "id": generate_msg_id(),
                "sender": reviewer_emp["id"],
                "sender_name": reviewer_emp["name"],
                "content": res.output,
                "phase": 2,
                "verdict": verdict,
                "elapsed_time": res.elapsed,
                "created_at": int(time.time()),
                "sent_prompt": review_prompt,
                "raw_response": res.raw_output
            })

            if verdict == "PASS" and len(session["passed_employees"]) == len(workers):
                session["status"] = "idle"
                session["messages"].append(
                    MeetingEngine._create_concluded_message(moderator["id"], moderator["name"])
                )

            save_session(session)
            return True, f"[{reviewer_emp['name']}] 完成审阅，结论: {verdict}"

        return False, "无动作"
