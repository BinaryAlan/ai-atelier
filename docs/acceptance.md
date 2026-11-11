# AI-Meeting 任务系统 · 验收标准与核对表

对应规格：`docs/task-review-protocol.md`。验收范围是软件的最终形态：任务系统行为，以及旧圆桌代码、
旧接口、旧数据、旧设置字段和旧界面文案是否清除干净。规格文档本身不改动。

## 验证方式

```bash
# 1. 自动化测试（33 项，不调用真实 CLI、不写真实 data/）
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
| A27 | 10 | 设置只有成员管理 / 界面语言 / 服务三部分，没有模式选择 | CDP：`settingsTabs = [general, members, service]`、三项中文标签正确、`hasMeetingTab=false`；`settings` 只有 `ui_language` / `employees` 两个键 | 通过 |
| A28 | 2 / 6.1 | 长内容默认折叠；每条消息可查看完整输入与原始输出 | 原折叠/查看器逻辑保留；CDP 弹窗查看输入按钮 | 通过 |
| A29 | 11 | Flask + Vue3、会话 JSON 落盘、CLI 子进程执行、输入拼装与结构化输出解析模块齐备 | 语法编译 + Flask test_client 接口测试 + CDP 页面挂载 | 通过（端到端用注入的 FakeRunner，未调用真实 CLI 子进程，避免真实 token 消耗） |
| A30 | 全局 | 双语：新增界面文案有英文词条 | CDP：`stateLabel('running')='Running'`、`stateLabel('delivered')='Delivered'`；词典共 204 条，任务系统段 54 条；脚本核对 `t()` 的中文词条无遗漏、无重复键 | 通过 |
| A31 | 全局 | 旧代码与旧接口删除：`core/engine.py`、`core/streaming.py`、`core/i18n.py` 以及 `/stream`、`/pause`、`/resume`、`/interrupt`、`/skip`、`/task/stop`、旧消息 `/retry` 全部不存在 | `git status` 显示 3 个模块已删除；`app.url_map` 路由清单 21 条，逐条确认无旧路径；全仓 grep 无 `core.engine` / `core.streaming` / `core.i18n` 引用 | 通过 |
| A32 | 全局 | 旧数据清除：`settings.json` 只有 `ui_language` 与 `employees`（3 个可用成员）；旧会话文件清空；新会话不再有 `status` / `phase` / `mode` / `document` / `current_speaker` 等旧字段 | 文件内容核对；接口冒烟：带 `moderator` / `collab` / `default_mode` 的旧配置读取后只回两个键，POST 写入也只落两个键；带 `mode` / `phase` 创建会话，返回字段里无旧字段；`data/sessions/` 只剩 `.gitkeep` | 通过 |
| A33 | 10 | 主持人配置删除：界面无主持人卡片、设置无 `moderator`、`load_settings` 不再按旧字段做兼容 | CDP 设置面板文案无「主持人」；`settingsKeys` 无 `moderator`；`core/storage.py` 改为只按白名单字段构造设置 | 通过 |
| A34 | 全局 | 用户可见文案与规格一致：界面、词典、启动提示、重启/停止确认、run.sh、README 都改为「会话 / 成员 / 任务 / 已取消可重试」 | CDP 首屏 + 新建会话弹窗 + 设置面板 + 服务页：旧词命中数 0；词典 204 条旧词命中 0；重启/停止确认含「已取消」「任务卡片」且不含「恢复讨论」；全仓中文旧词 grep 无业务残留 | 通过 |
| A35 | 全局 | 中英 README 与 `run.sh` 同步任务系统；旧圆桌演示 GIF 移除 | README 两份按规格 11 章结构重写，目录树与真实文件一致；`run.sh status` 读 `active_tasks`、启动/重启提示改为任务表述；`git rm docs/demo.gif docs/demo-en.gif`（已备份到 `/tmp/aim-legacy-backup-20261111-184514/`） | 通过 |

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
  新建会话弹窗、设置面板的旧词命中数均为 0；词典 204 条旧词命中 0；服务页显示「进行中任务 / 重启服务 / 停止服务」；
  重启与停止确认文案含「已取消」「任务卡片」、不含「恢复讨论」。截图 `/tmp/aim-verify-cleanup.png`。
