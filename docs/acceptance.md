# AI-Meeting 任务系统改造 · 验收标准与核对表

对应规格：`docs/task-review-protocol.md`。本次只改代码与测试，规格文档未改动。

## 验证方式

```bash
# 1. 自动化测试（33 项，不调用真实 CLI、不写真实 data/）
.venv/bin/python -m unittest discover -s tests -v

# 2. 语法检查
.venv/bin/python -m py_compile app.py core/tasks.py core/storage.py
node --check static/js/app.js && node --check static/js/i18n.js

# 3. 界面验证（无头 Chrome + CDP，虚构会话数据注入，不碰真实会话文件）
PORT=5097 FLASK_DEBUG=0 .venv/bin/python app.py
node /tmp/verify_ui.mjs
```

界面验证使用 `static/` 的真实页面与真实 Vue 实例，通过 CDP 向组件注入一份虚构会话
（含任务卡片、执行中/失败的单任务结果消息、`in_chat:false` 的执行记录），检查 DOM 与交互。
截图：`/tmp/aim-verify-card.png`。

---

## 逐条核对

| # | 规格 | 验收标准 | 验证方式 | 结果 |
| :-- | :--- | :--- | :--- | :--- |
| A1 | 1 / 5.3 | 右侧面板只保留工作目录文件树与浏览；界面不设全局控制条 | CDP：`工作目录预览` 存在、`会议方案 / 纪要` 不存在、无暂停/恢复/结束任务/跳过此人 | 通过 |
| A2 | 2 / 4.2 | `@` 一位成员 → 单任务入该成员队列，独立完成并输出一条结果消息 | `test_single_task_structure`、`test_no_mention_is_context_only` | 通过 |
| A3 | 2 | 没点名成员的消息只作为上下文保留，不产生任务 | `test_no_mention_is_context_only` | 通过 |
| A4 | 2 / 5.1 | 引用回复把被引用的原话交给被回复的成员 | `test_reply_targets_quoted_member`（HTTP 层 `forced_owner_id`） | 通过 |
| A5 | 4.1 | `/collab @主创 @协助成员... 任务内容` 发起协助任务，去掉指令前缀作为上下文 | `test_valid_collab_strips_prefix_and_dedupes`、`test_collab_validation_endpoint` | 通过 |
| A6 | 4.1 | 严格验证：缺主创、缺协助成员、缺任务内容一律拦截提示，不创建任务 | 6 项解析测试 + 前端输入时提示 + 后端 400 `validation_error` + CDP 校验 | 通过 |
| A7 | 4.3 | 协助任务固定一轮：主创执行 → 成员审核 → 主创交付 | `test_three_stages_and_card`（执行+2 审核+交付共 4 条执行记录） | 通过 |
| A8 | 4.3 | 不同协助成员独立并行审核 | `test_review_members_run_in_parallel`（Barrier(2) 同时进入，`max_active>=2`） | 通过 |
| A9 | 4.3 | 个别审核成员失败 / 超时 / 已取消 / 无意见，主创仍交付 | `test_failed_reviewer_still_delivers`、`test_empty_review_marked_no_opinion`、`test_cancel_one_reviewer_does_not_stop_delivery` | 通过 |
| A10 | 4.3 | 只取消某位成员的审核时，任务继续走到交付 | `test_cancel_one_reviewer_does_not_stop_delivery` | 通过 |
| A11 | 4.3 / 8 | 审核环节一律只读，不改主创产物 | `test_review_is_read_only`（runner 收到 `write_access:false`） | 通过 |
| A12 | 3 / 4.4 | 每位成员一个 FIFO 队列，同一成员串行 | `test_same_member_fifo_serial`（第一条阻塞时第二条未启动，`max_active=1`） | 通过 |
| A13 | 3 / 4.4 | 不同成员之间的任务互不阻塞、可并行 | `test_different_members_run_in_parallel`（Barrier(2)） | 通过 |
| A14 | 4.4 | 审核不占审核成员队列位置；该成员忙时等当前任务结束后串行执行 | 代码 `_wait_member_idle` + `test_queued_task_waits_for_member_current_task` | 通过 |
| A15 | 4.4 / 6.2 | 同一成员多任务排队时界面显示该成员队列的排队状态 | `test_queue_exposes_pending_order`（`task_queues` 顺序 + `tasks.status`）+ CDP：卡片外壳与结果消息显示「该成员队列：第 n / m 项」 | 通过 |
| A16 | 5.2 / 8 | 取消：立即向执行进程发终止信号，已写文件不回滚，静默转「已取消」，输入框即刻就绪 | `test_cancel_is_silent_and_keeps_files`（killer 被调用、无系统播报、文件保留）+ CDP `inputReady=true` | 通过（信号路径由 `Executor.kill_session_process` 提供，测试用注入 killer 验证；未对真实 CLI 进程做信号实测） |
| A17 | 5.2 | 「取消」只在正在输出的消息 / 正在进行的环节上；「重试」在失败、已取消、超时、无意见、交付完成后 | CDP：执行中显示取消、失败显示重试、进行中卡片无交付重试、已交付后出现「重新生成交付内容」 | 通过 |
| A18 | 4.3 / 5.2 | 重试只重跑被点环节：执行重跑后审核与交付重新推进；审核重试不自动重做交付 | `test_retry_exec_reruns_reviews_and_delivery`、`test_retry_review_only_updates_that_member`、`test_retry_delivery_regenerates_final` | 通过 |
| A19 | 4.3 / 5.2 | 重试沿用原输入与同一工作目录，不新增任务、不增加流程轮次 | 重试类测试断言 `tasks` 长度不变、执行次数符合预期 | 通过 |
| A20 | 6.2 | 任务创建时卡片即出现，默认折叠；外壳显示摘要、整体状态、环节进度、实时耗时流水、成员状态点 | `test_collab_task_creates_card`（创建即 queued）+ CDP（外壳、环节、耗时、状态点） | 通过 |
| A21 | 6.2 / 7 | 点击卡片弹窗分两部分，按记录可查完整输入与原始输出；取消/重试产生的历次记录保留可查 | CDP 弹窗两部分 /「历次执行记录」列表 / 点记录打开审查弹窗显示完整 prompt 与 raw；`test_three_stages_and_card`（4 条 `in_chat:false` 记录）、`test_history_records_preserved_after_retry`（重跑交付后 4 条记录，含 2 条交付记录） | 通过 |
| A22 | 6.3 | 任务状态与成员状态集合齐全，成员含「超时」「无意见」 | `test_empty_review_marked_no_opinion` + 状态映射 `stateLabel`（含英文） | 通过 |
| A23 | 7 | 会话保存 `tasks` / `task_queues` / `active_task_id`；消息带 `kind` / `task_id` / `in_chat` / `sent_prompt` / `raw_response` | `test_single_task_structure`、`test_old_sessions_get_task_fields`（老会话自动补字段） | 通过 |
| A24 | 4.4 | 刷新页面不改变正在执行的任务；服务重启后进行中任务标记已取消，可从被中断环节重试 | `test_recover_marks_running_as_cancelled` + 服务启动时 `recover_on_startup()`；刷新不改变会话状态（前端只轮询） | 通过 |
| A25 | 8 | 工作目录按会话配置；写权限由成员设置决定；取消后文件保留、卡片如实列出 | `test_review_is_read_only`、产物清单解析 `scan_artifacts`、CDP 产物清单渲染 | 通过 |
| A26 | 9 | 运行规程 4 份文本随软件提供，服务端拼进成员输入；结构化输出解析不到按纯文本展示 | `test_prompt_includes_protocol_files`、`test_plain_text_output_fallback`；文件：`prompts/protocol/task-flow.md`、`prompts/roles/{single,owner,reviewer}.md` | 通过 |
| A27 | 10 | 设置只有成员管理 / 界面语言 / 服务三部分，没有模式选择 | CDP：`settingsTabs = [general, members, service]`；模板删除会议行为页与新建/编辑会话的模式选择 | 通过（成员页仍保留旧「主持人」配置卡片，任务流程不读取它，属旧数据兼容） |
| A28 | 2 / 6.1 | 长内容默认折叠；每条消息可查看完整输入与原始输出 | 原折叠/查看器逻辑保留；CDP 弹窗查看输入按钮 | 通过 |
| A29 | 11 | Flask + Vue3、会话 JSON 落盘、CLI 子进程执行、输入拼装与结构化输出解析模块齐备 | 语法编译 + Flask test_client 接口测试 + CDP 页面挂载 | 通过（端到端用注入的 FakeRunner，未调用真实 CLI 子进程，避免真实 token 消耗） |
| A30 | 全局 | 双语：新增界面文案有英文词条 | CDP：`stateLabel('running')='Running'`、`stageLabel='Members reviewing in parallel'`；i18n 词典「AI 团队任务系统」段共 46 条，`t()` 中文词条无遗漏（正则误匹配的 CSS 选择器除外） | 通过 |

---

## 明确不在本次改造范围 / 保留兼容项

1. **旧圆桌流程代码与接口保留**：`core/engine.py`、`/pause`、`/resume`、`/interrupt`、`/skip`、旧消息 `/retry` 仍在，
   但 `api_send_message` 已改走任务系统，界面不再暴露任何全局控制条。旧会话可只读浏览。
2. **设置里的主持人配置卡片保留**：`settings.moderator` 是旧数据字段，任务系统不读取、不调用主持人。
3. **流式预览通道保留但未被任务系统触发**：任务执行不写 `current_speaker`，因此流式气泡不会出现。
4. **README 与演示 GIF 仍描述旧圆桌界面**：本次按用户要求只改代码与测试，未重写 README。
5. **真实 CLI 端到端未跑**：全部流程用注入执行器验证；真实 `agy` / `codebuddy` 子进程只由既有
   `Executor.run_employee` 路径承载，取消信号由 `Executor.kill_session_process`（SIGTERM → 0.3s → SIGKILL）实现。

---

## 本次核对记录（2026-11-11）

- 自动化：`Ran 33 tests in 7.6s — OK`（解析 6、创建 4、队列 5、协助流程 5、取消重试 7、恢复与规程 4、HTTP 2）。
- 语法：Python 3 文件、2 个 JS 文件全部通过。
- 接口（运行中的服务）：`/collab/validate` 有效、缺主创、缺协助、缺任务内容四种情形；
  取消不存在任务返回 404；`GET /` 返回 200；验证过程未改动 `data/sessions/` 里的真实会话文件。
- 界面（CDP）：Vue 正常挂载；卡片外壳/环节/耗时/状态点、消息上的取消与重试、`in_chat:false` 过滤、
  右栏只工作目录、无全局控制条、无模式徽章、`/collab` 输入校验、弹窗两部分、已交付后的交付重试、
  输入框始终可用、设置分类无 meeting 页、英文词条，全部符合预期。
- 复核后补两处：成员队列位置显示（A15）、卡片弹窗「历次执行记录」按记录查看完整输入/原始输出（A21）；
  同时把成功执行记录的状态由「已交付」修正为「已完成」，避免环节记录与任务终态混淆。
  新增 `test_queue_exposes_pending_order`、`test_history_records_preserved_after_retry`，并扩充 CDP 校验项后重跑，全部通过。
