# AI-Meeting 任务系统 · 验收标准与核对表

对应规格：`docs/task-review-protocol.md`。验收范围是软件的最终形态：任务系统行为，以及旧圆桌代码、
旧接口、旧数据、旧设置字段和旧界面文案是否清除干净。规格文档本身不改动。

## 验证方式

```bash
# 1. 自动化测试（54 项，不调用真实 CLI、不写真实 data/）
.venv/bin/python -m unittest discover -s tests -v

# 2. 语法检查
.venv/bin/python -m py_compile app.py core/*.py
node --check static/js/app.js && node --check static/js/i18n.js

# 3. 路由与接口冒烟（Flask test_client，数据目录指向临时目录）
#    检查：路由清单无旧接口、设置只落两个字段、新会话无旧字段、状态接口是 active_tasks

# 4. 界面验证（无头 Chrome + CDP，虚构会话数据注入，不碰真实会话文件）
PORT=5097 FLASK_DEBUG=0 .venv/bin/python app.py
node /tmp/verify_ui.mjs        # 任务系统行为与布局
node /tmp/verify_cleanup.mjs   # 旧文案 / 旧字段 / 设置三部分 / 确认弹窗文案
```

界面验证使用 `static/` 的真实页面与真实 Vue 实例，通过 CDP 向组件注入一份虚构会话
（含任务卡片、执行中/失败的单任务结果消息、`in_chat:false` 的执行记录），检查 DOM 与交互。
截图：`/tmp/aim-verify-card.png`、`/tmp/aim-verify-cleanup.png`。

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
| A11 | 4.3 / 8 | 审核环节按只读执行：规程约束只读，服务端不向其注入写权限（成员模板若自带写开关，仍以规程为准） | `test_review_is_read_only`（runner 收到 `write_access:false`）+ `prompts/roles/reviewer.md` 的只读规程 | 通过 |
| A12 | 3 / 4.4 | 每位成员一个 FIFO 队列，同一成员串行：占用粒度是「单个执行单元」（执行 / 审核 / 交付各自占用），交叉协作也不得互等；等不到成员时标失败可重试，不并发起进程 | `test_same_member_fifo_serial`、`test_review_and_queue_task_do_not_overlap`、`test_crossed_collab_tasks_do_not_deadlock`（A 主创 agy/审 DS4 × B 主创 DS4/审 agy，每成员并发峰值 1）、`test_member_wait_timeout_fails_instead_of_running_concurrently` | 通过 |
| A13 | 3 / 4.4 | 不同成员之间的任务互不阻塞、可并行 | `test_different_members_run_in_parallel`（Barrier(2)） | 通过 |
| A14 | 4.4 | 审核不占审核成员队列位置；该成员忙时等当前任务结束后串行执行 | 代码 `_wait_member_idle` + `test_queued_task_waits_for_member_current_task` | 通过 |
| A15 | 4.4 / 6.2 | 同一成员多任务排队时界面显示该成员队列的排队状态 | `test_queue_exposes_pending_order`（`task_queues` 顺序 + `tasks.status`）+ CDP：卡片外壳与结果消息显示「该成员队列：第 n / m 项」 | 通过 |
| A16 | 5.2 / 8 | 取消：立即向执行进程发终止信号，已写文件不回滚，静默转「已取消」，输入框即刻就绪 | `test_cancel_is_silent_and_keeps_files`（killer 被调用、无系统播报、文件保留）+ CDP `inputReady=true` | 通过（信号路径由 `Executor.kill_session_process` 提供，测试用注入 killer 验证；未对真实 CLI 进程做信号实测） |
| A17 | 5.2 | 「取消」只在正在输出 / 尚未结束的环节上，已结束的任务或环节一律拒绝（409 `task_finished` / `stage_not_active`）；「重试」在失败、已取消、超时、无意见、交付完成后 | CDP：执行中显示取消、失败显示重试、进行中卡片无交付重试、已交付后出现「重新生成交付内容」、排队中的卡片也能取消；`test_cancel_is_rejected_on_finished_task`、`test_cancel_failed_task_keeps_failure`、`test_cancel_queued_task_syncs_message` | 通过 |
| A18 | 4.3 / 5.2 | 重试只重跑被点环节：执行重跑后审核与交付重新推进；审核重试不自动重做交付；单任务重试只重跑单任务本身；交付重试要求执行已成功 | `test_retry_exec_reruns_reviews_and_delivery`、`test_retry_review_only_updates_that_member`（断言重试真的执行了第 2 次）、`test_retry_delivery_regenerates_final`、`test_retry_single_reruns_single_stage`、`test_deliver_retry_requires_successful_exec` | 通过 |
| A19 | 4.3 / 5.2 | 重试沿用原输入与同一工作目录，不新增任务、不增加流程轮次 | 重试类测试断言 `tasks` 长度不变、执行次数符合预期（单任务重试为 `exec, exec`，不额外跑交付） | 通过 |
| A20 | 6.2 | 任务创建时卡片即出现，默认折叠；外壳显示摘要、整体状态、环节进度、实时耗时流水、成员状态点，并在审核过程中实时更新 | `test_collab_task_creates_card`（创建即 queued）+ CDP（外壳、环节、耗时、状态点）+ 轮询签名含 `part_a.status` / `part_b[].status`，环节状态变化即刷新 | 通过 |
| A21 | 6.2 / 7 | 点击卡片弹窗分两部分，按记录可查完整输入与原始输出；取消/重试产生的历次记录保留可查 | CDP 弹窗两部分 /「历次执行记录」列表 / 点记录打开审查弹窗显示完整 prompt 与 raw；`test_three_stages_and_card`（4 条 `in_chat:false` 记录）、`test_history_records_preserved_after_retry`（重跑交付后 4 条记录，含 2 条交付记录） | 通过 |
| A22 | 6.3 | 任务状态与成员状态集合齐全：任务含「超时」（终态、可重试），成员含「超时」「无意见」 | `test_empty_review_marked_no_opinion` + 状态映射 `stateLabel`（含英文） | 通过 |
| A23 | 7 | 会话保存 `tasks` / `task_queues` / `active_task_id`；消息带 `kind` / `task_id` / `in_chat` / `sent_prompt` / `raw_response` | `test_single_task_structure`、`test_old_sessions_get_task_fields`（老会话自动补字段） | 通过 |
| A24 | 4.4 | 刷新页面不改变正在执行的任务；服务重启后进行中任务标记已取消，三个环节的「进行中」一并转已取消，可从被中断的环节重试；已结束的任务不被恢复逻辑改动 | `test_recover_marks_running_as_cancelled`、`test_recover_cancels_inflight_stages`、`test_single_task_closes_part_a_and_recover_ignores_finished` | 通过 |
| A25 | 8 | 工作目录按会话配置；写权限由成员设置决定；取消后文件保留、卡片如实列出 | `test_review_is_read_only`、产物清单解析 `scan_artifacts`、CDP 产物清单渲染 | 通过 |
| A26 | 9 | 运行规程 4 份文本随软件提供，服务端拼进成员输入；结构化输出解析不到按纯文本展示 | `test_prompt_includes_protocol_files`、`test_plain_text_output_fallback`；文件：`prompts/protocol/task-flow.md`、`prompts/roles/{single,owner,reviewer}.md` | 通过 |
| A27 | 10 | 设置只有成员管理 / 界面语言 / 服务三部分，没有模式选择 | CDP：`settingsTabs = [general, members, service]`、三项中文标签正确、`hasMeetingTab=false`；`settings` 只有 `ui_language` / `employees` 两个键 | 通过 |
| A28 | 2 / 6.1 | 长内容默认折叠；每条消息可查看完整输入与原始输出 | 原折叠/查看器逻辑保留；CDP 弹窗查看输入按钮 | 通过 |
| A29 | 11 | Flask + Vue3、会话 JSON 落盘、CLI 子进程执行、输入拼装与结构化输出解析模块齐备 | 语法编译 + Flask test_client 接口测试 + CDP 页面挂载 | 通过（端到端用注入的 FakeRunner，未调用真实 CLI 子进程，避免真实 token 消耗） |
| A30 | 全局 | 双语：新增界面文案有英文词条 | CDP：`stateLabel('running')='Running'`、`stateLabel('delivered')='Delivered'`；词典共 206 条（本轮新增「主创 / 协助成员已停用」两条中英对照）；脚本核对 `t()` 的中文词条无遗漏、无重复键 | 通过 |
| A31 | 全局 | 旧代码与旧接口删除：`core/engine.py`、`core/streaming.py`、`core/i18n.py` 以及 `/stream`、`/pause`、`/resume`、`/interrupt`、`/skip`、`/task/stop`、旧消息 `/retry` 全部不存在 | `git status` 显示 3 个模块已删除；`app.url_map` 路由清单 21 条，逐条确认无旧路径；全仓 grep 无 `core.engine` / `core.streaming` / `core.i18n` 引用 | 通过 |
| A32 | 全局 | 旧数据清除：`settings.json` 只有 `ui_language` 与 `employees`（3 个可用成员）；旧会话文件清空；新会话不再有 `status` / `phase` / `mode` / `document` / `current_speaker` 等旧字段 | 文件内容核对；接口冒烟：带 `moderator` / `collab` / `default_mode` 的旧配置读取后只回两个键，POST 写入也只落两个键；带 `mode` / `phase` 创建会话，返回字段里无旧字段；`data/sessions/` 只剩 `.gitkeep` | 通过 |
| A33 | 10 | 主持人配置删除：界面无主持人卡片、设置无 `moderator`、`load_settings` 不再按旧字段做兼容 | CDP 设置面板文案无「主持人」；`settingsKeys` 无 `moderator`；`core/storage.py` 改为只按白名单字段构造设置 | 通过 |
| A34 | 全局 | 用户可见文案与规格一致：界面、词典、启动提示、重启/停止确认、run.sh、README 都改为「会话 / 成员 / 任务 / 已取消可重试」 | CDP 首屏 + 新建会话弹窗 + 设置面板 + 服务页：旧词命中数 0；词典 206 条旧词命中 0；重启/停止确认含「已取消」「任务卡片」且不含「恢复讨论」；全仓中文旧词 grep 无业务残留 | 通过 |
| A35 | 全局 | 中英 README 与 `run.sh` 同步任务系统；旧圆桌演示 GIF 移除 | README 两份按规格 11 章结构重写，目录树与真实文件一致；`run.sh status` 读 `active_tasks`、启动/重启提示改为任务表述；`git rm docs/demo.gif docs/demo-en.gif`（已备份到 `/tmp/aim-legacy-backup-20261111-184514/`） | 通过 |

| A36 | 4.2 / 5.1 | 成员输入包含「会话背景」：最近 8 条消息（单条 600 字、合计 3000 字上限），被截断的更早部分压成一段进展摘要；系统播报与各环节执行记录不进背景；没点名成员的消息因此真正作为上下文生效；重试时原话之后补充的约束一并带上 | `test_recent_messages_fill_context`、`test_context_skips_system_and_records`、`test_current_message_is_not_duplicated`、`test_prompt_contains_context_section`、`test_retry_carries_later_user_constraints` | 通过 |

---

## 不在验收范围

1. **真实 CLI 端到端未跑**：全部流程用注入执行器验证；真实 `agy` / `codebuddy` 子进程只由现有
   `Executor.run_employee` 路径承载，取消信号由 `Executor.kill_session_process`（SIGTERM → 0.3s → SIGKILL）实现。
2. **取消信号未对真实进程实测**：killer 注入验证调用链与状态流转，未在本轮消耗真实 CLI token。
3. **演示 GIF 未重录**：旧 GIF 展示的是已删除的圆桌界面，先移除；当前界面无演示动图。

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

## 旧圆桌清理复核记录（2026-11-11）

- 备份先行：旧会话、`settings.json`、`scanned_models.json`、两份演示 GIF 都先复制到
  `/tmp/aim-legacy-backup-20261111-184514/`，然后再删除/覆盖。
- 删除：`core/engine.py`、`core/streaming.py`、`core/i18n.py`（`git rm`）；`data/sessions/` 下 3 个旧会话文件清空。
- 回归：`Ran 33 tests in 7.429s — OK`；`py_compile app.py core/*.py`、`node --check` 两个 JS 全部通过。
- 路由清单 21 条，无 `/stream` `/pause` `/resume` `/interrupt` `/skip` `/task/stop` 及旧消息 `/retry`。
- 接口冒烟（临时数据目录）：`GET /api/settings` → `['employees','ui_language']`；带旧字段 POST 后落盘仍是两个键；
  `POST /api/sessions` 返回 `active_task_id` / `tasks` / `task_queues` 等任务字段且无任何旧字段；
  `/api/system/status` 有 `active_tasks`、无 `active_sessions`。
- 界面（CDP `/tmp/verify_ui.mjs`）：卡片外壳 / 环节 / 耗时 / 状态点、消息上的取消与重试、`in_chat:false` 过滤、
  右栏只工作目录、无全局控制条、无模式徽章、`/collab` 输入校验、弹窗两部分与历次记录、
  已交付后的交付重试、输入框始终可用、设置三部分、英文词条，全部符合预期。
- 界面（CDP `/tmp/verify_cleanup.mjs`）：页面标题为「AI-Meeting · 多 AI 协同任务工作台」；首屏、
  新建会话弹窗、设置面板的旧词命中数均为 0；词典 206 条旧词命中 0；服务页显示「进行中任务 / 重启服务 / 停止服务」；
  重启与停止确认文案含「已取消」「任务卡片」、不含「恢复讨论」。截图 `/tmp/aim-verify-cleanup.png`。

## 独立复核与修复记录（2026-11-11 晚）

按规格逐条复读实现（不是只跑测试），发现并修掉 7 项，另有 2 项为规格缺口补齐：

1. **单任务重试走错流水线**：`_retry_worker` 不区分任务类型，单任务的重试被当成协助任务跑，
   结果消息永不更新（仍显示「失败」）、多跑一次交付环节、给单任务写入了本不该有的 `part_a.final`。
   → 按 `type` 分流到 `_run_single`。证据：修复前 `keys=['exec','exec','deliver']`、消息 `status=failed`。
2. **审核重试自锁**：`_retry_worker` 先占用成员，`_run_review_one` 内部又等该成员空闲 → 同一线程等自己，
   线程永久挂起（点「重试」返回 ok，卡片永远停在「失败」）。旧测试只断言「交付没被重跑」，掩盖了这一点。
   → 占用逻辑改为可重入的 `_acquire_member/_release_member`，重试不再重复占用。
3. **服务重启只改任务状态**：`recover_on_startup` 不动 `part_a.status/final_status/part_b`，
   中断在审核环节时成员永久 `in_progress`，既无取消（进程已没了）也无重试（`in_progress` 不可重试）。
   → 启动时统一收掉三个环节的进行中标记。
4. **取消排队任务后消息卡死**：`tasks` 转 cancelled，但结果消息永久停在「排队中」且留着点不动的按钮，
   `task_queues` 里还留死条目。→ 取消时同步消息状态并从队列移除。
5. **同一成员可并发**：审核进行中给该成员派队列任务会立刻开跑（实测 `max_active=3`，审核与执行同时进行）。
   → 队列任务与审核、重试共用同一占用标记，双向串行；等待有上限（30 分钟），避免两个任务互相等成死锁。
6. **删会话不杀子进程**：执行进程注册在 `{session_id}:{task_id}:{stage}:{member_id}`，
   删除时只按 `session_id` 精确匹配 → 子进程跑完、白烧 token。→ 新增 `Executor.kill_session_processes` 前缀匹配。
7. **前端轮询签名缺环节状态**：`sessionSignature` 只看卡片整体状态与流水长度，审核进入「进行中」不改这两项，
   卡片在整个审核期间显示「等待」。→ 签名纳入 `part_a.status` / `part_b[].status`，
   轮询开关也认「环节进行中」，重试后关掉弹窗仍能继续刷新。
8. **补齐规格 4.2 的「会话背景」**（A36）：`build_prompt` 原本完全不拼历史，未点名的消息「作为上下文保留」
   只是落盘、从不生效。→ 最近 8 条 + 未进窗口部分的进展摘要 + 产物指针（只给路径不给正文）。
9. **A11 措辞**：只读的强制力在运行规程（`prompts/roles/reviewer.md`）与服务端不注入写权限，
   不是硬隔离；验收表按此改写，不再写「一律只读」。

回归：`Ran 41 tests in 1.8s — OK`（新增 8 项：单任务重试、重启中断恢复、排队取消、同成员互斥、
审核重试真的执行、会话背景 4 项）；`py_compile app.py core/*.py tests/*.py`、`node --check static/js/app.js` 通过。
探针复核（FakeStore/FakeRunner，不碰真实 `data/`）：单任务重试调用序列 `exec, exec`、消息状态 delivered；
审核中断重启后 `part_b` 全 cancelled 且重试可用；同成员并发峰值由 3 降到 2（仅两位审核成员并行）。
未改动的部分：`docs/task-review-protocol.md` 规格正文、真实 CLI 端到端与真实进程 SIGTERM（仍不在验收范围）。

## 业务不变量复核与修复（2026-11-11 夜，第二轮）

这一轮不看「代码是否按预期跑」，而是先建模再按业务不变量审：同一成员串行（I2）、终态不可逆（I3）、
取消后不再产生调用（I4）、不存在无法脱身的停留态（I5）、任务/消息/卡片口径一致（I6）、
交付只在执行成功之后（I7）。全部用 FakeStore/FakeRunner 实测复现，再修再验。

1. **交叉协作死锁**：占用粒度原先是从「任务开始」到「交付结束」的整段，而审核又内联在该段里，
   `A(主创 agy / 审 DS4)` 与 `B(主创 DS4 / 审 agy)` 会互等对方手里的成员——实测两任务都停在 `step2`、
   `part_b=waiting`、零次模型调用，要等 `MEMBER_WAIT_SECONDS=1800` 才「破窗」并发起两个进程同写一个目录。
   → 占用粒度改为**单个执行单元**（执行 / 审核 / 交付各自 `_execute` 内占用），队列 worker 与重试线程都不再外层占用；
   等待超时改为「不执行、标失败可重试」，不再用破坏串行换取不死锁。修复后同一场景 1 秒内两个任务都 delivered，
   每成员并发峰值为 1。
2. **取消没有状态守卫**：`cancel` 不看状态直接写 `cancelled`，已交付任务能被「取消」回已取消（产物还在），
   已失败任务能被改成已取消而消息仍是失败。→ 只受理未结束的任务 + 未结束的环节，其余返回 409
   （`code=task_finished|stage_not_active`）；取消落在「进程已返回、结果未落库」的窗口时以取消为准，终态不回退。
3. **交付可跳过执行**：执行失败（`task.status=failed`）时 `retry(deliver)` 被放行，会产出没有产物、
   没有审核意见却显示「已交付」的任务。→ 追加前置条件 `part_a.status == done`。
4. **单任务环节状态不闭环**：`_run_single` 从不写 `part_a.status`，交付后仍停在 `in_progress`，
   服务重启时被恢复逻辑改成 `cancelled` 并追加「进行中的环节已取消」的假流水。→ 收尾写实际终态；
   `recover_on_startup` 只处理未结束的任务。
5. **重试丢补充说明**：会话背景以原始消息为切点，用户在失败后补充的约束永远进不了重试的 prompt。
   → `build_context(..., include_after=True)` 用于重试：保留原话之后的消息、仍排除原话本身（不重复）。
6. **取消标记跨轮污染**：`CANCELLED_KEYS` 是进程级集合且按 `{sid}:{tid}:{stage}:{member}` 复用，
   同一环节下一轮执行会被无条件判成「已取消」——真的调了模型却丢弃结果。→ 标记只对一次执行有效（执行结束即清理），
   并在起进程前再查一次，排队期间被取消的单元不再白烧 token。

回归：`Ran 51 tests in 3.0s — OK`（新增 10 项：交叉协作不死锁、取消拒绝终态、失败任务取消不篡改、
交付需先执行成功、单任务 part_a 闭环、重试带补充约束、取消标记单次有效、等待超时改判失败、超时属终态、重跑失败不残留上一轮内容）。
`py_compile app.py core/*.py tests/*.py`、`node --check static/js/app.js` 通过；
CDP 界面验证 `verify_ui.mjs` / `verify_cleanup.mjs` 全项通过（词典 206 条、旧词 0 命中）。

7. **遗留项收尾（第三轮）**：
   - 「超时」原先只存在于成员状态，单任务超时会写出第 6 种任务状态却不在 `FINAL_TASK_STATES` 里，
     文档 6.3 也只列 5 种。→ 任务状态补为 6 种（含超时），超时计入终态：取消会被拒绝（`task_finished`），
     重试照常可用（`test_timeout_is_terminal_and_blocks_cancel`）。
   - 重跑执行失败后，`part_a` 仍留着上一轮的简报、产物、`sent_prompt`/`raw_output`，卡片在失败态下把旧内容
     当本轮结果展示。→ 每个执行单元启动时先清空本轮要产出的字段；主创重跑时同时把 `part_b` 复位为等待并清掉旧意见
     （`test_rerun_exec_failure_does_not_keep_previous_round`）。
   - `part_a.final.changed_files` 装的是全部产物路径，与字段名和 6.2 的「终版改动说明」都不符，且无人读取。
     → 从代码与设计文档 7 的数据结构里一并移除，产物清单以 `part_a.artifacts` 为准。

8. **边缘项收尾（第四轮，agy 复审提出）**：
   - 排队中被取消的任务原本也会写 `CANCELLED_KEYS`，但它已从队列摘除、不会再进入执行单元，
     标记无人消费，只会在进程级集合里长期留一个字符串。→ 只有真正起过执行的任务才写标记
     （`test_cancel_queued_task_syncs_message` 增加断言）。
   - `MEMBER_WAIT_SECONDS` 上方还留着「超时则照常执行」的旧注释，与实现（超时不执行、标失败可重试）相反，
     已改为与实现一致的说明。
   - 协助成员已停用或缺 CLI 时，原先只在审核环节落一个「成员不可用」，主创仍白跑一轮。
     → `parse_collab` 在解析阶段就拒绝（主创与协助成员一视同仁），`api_validate_collab` 与提交走同一套判定；
     前端输入提示同步检查 `enabled` 与 `cli_cmd`，输入时就给「协助成员 HY4 已停用或缺少 CLI 配置」，
     不再显示「格式正确」后再失败（`test_disabled_reviewer_is_rejected_before_submit`、
     `test_reviewer_without_cli_is_rejected`、`test_disabled_owner_is_rejected`；CDP 实测提示文案正确，词典 206 条）。

回归：`Ran 54 tests in 3.0s — OK`。
