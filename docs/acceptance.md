# AI Atelier 任务系统 · 验收标准与核对表

> 项目原名 AI-Meeting。对应规格：`docs/task-review-protocol.md`。验收范围是软件的最终形态：任务系统行为，以及旧圆桌代码、
旧接口、旧数据、旧设置字段和旧界面文案是否清除干净；A81 为项目重命名验收项。规格文档本身不改动。

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
| A3 | 2 | 没点名成员的消息：会话里已有 AI 回过话时默认交给最近那一位（A71.4）；一条 AI 消息都没有时只作为上下文保留，不产生任务 | `test_no_mention_is_context_only`、`TestDefaultRouteToLastAi` 4 项 | 通过 |
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
| A30 | 2 / 5.1 | 输入框打 `/` 弹出可用指令、打 `@` 弹出本会话成员；↑↓ 选择、Enter 补全、Esc 关闭；候选打开时 Enter 不发送消息，输入法组词时 Enter 不误选；选完 `/collab` 直接接着选成员 | CDP `verify_suggest.mjs`：16 项全通过（命令列表、成员过滤、方向键、Esc 保留输入、正文里的 `/` 不误弹、中文后紧跟 `@` 也弹、邮箱不误弹、Enter 不发送、IME 不误选） | 通过 |
| A31 | 4.1 | `/collab` 的提及按出现顺序判定、可散落在句子里；`@//agy/` 路径写法、大小写、按目录名写的 `@deepseek-v4/(DS4)` 都能认出；邮箱等对不上的 `@` 不算提及；前端校验提示与后端同一套解析（`/api/collab/validate`） | `test_mentions_may_be_spread_across_the_sentence`、`test_file_reference_style_mentions_are_accepted`、`test_dashed_full_name_with_parenthesis_is_accepted`、`test_mentions_are_case_insensitive`、`test_email_and_path_are_not_mentions`、`test_first_mention_wins_for_single_task` + CDP `verify_collab.mjs`：6 项全通过 | 通过 |
| A30 | 全局 | 双语：新增界面文案有英文词条 | CDP：`stateLabel('running')='Running'`、`stateLabel('delivered')='Delivered'`；词典共 206 条（本轮新增「主创 / 协助成员已停用」两条中英对照）；脚本核对 `t()` 的中文词条无遗漏、无重复键 | 通过 |
| A31 | 全局 | 旧代码与旧接口删除：`core/engine.py`、`core/streaming.py`、`core/i18n.py` 以及 `/stream`、`/pause`、`/resume`、`/interrupt`、`/skip`、`/task/stop`、旧消息 `/retry` 全部不存在 | `git status` 显示 3 个模块已删除；`app.url_map` 路由清单 21 条，逐条确认无旧路径；全仓 grep 无 `core.engine` / `core.streaming` / `core.i18n` 引用 | 通过 |
| A32 | 全局 | 旧数据清除：`settings.json` 只有 `ui_language` 与 `employees`（3 个可用成员）；旧会话文件清空；新会话不再有 `status` / `phase` / `mode` / `document` / `current_speaker` 等旧字段 | 文件内容核对；接口冒烟：带 `moderator` / `collab` / `default_mode` 的旧配置读取后只回两个键，POST 写入也只落两个键；带 `mode` / `phase` 创建会话，返回字段里无旧字段；`data/sessions/` 只剩 `.gitkeep` | 通过 |
| A33 | 10 | 主持人配置删除：界面无主持人卡片、设置无 `moderator`、`load_settings` 不再按旧字段做兼容 | CDP 设置面板文案无「主持人」；`settingsKeys` 无 `moderator`；`core/storage.py` 改为只按白名单字段构造设置 | 通过 |
| A34 | 全局 | 用户可见文案与规格一致：界面、词典、启动提示、重启/停止确认、run.sh、README 都改为「会话 / 成员 / 任务 / 已取消可重试」 | CDP 首屏 + 新建会话弹窗 + 设置面板 + 服务页：旧词命中数 0；词典 206 条旧词命中 0；重启/停止确认含「已取消」「任务卡片」且不含「恢复讨论」；全仓中文旧词 grep 无业务残留 | 通过 |
| A35 | 全局 | 中英 README 与 `run.sh` 同步任务系统；旧圆桌演示 GIF 移除 | README 两份按规格 11 章结构重写，目录树与真实文件一致；`run.sh status` 读 `active_tasks`、启动/重启提示改为任务表述；`git rm docs/demo.gif docs/demo-en.gif`（已备份到 `/tmp/aim-legacy-backup-20261111-184514/`） | 通过 |
| A36 | 2 / 6.1 | 从协助卡片里点「查看输入 / 原始输出」，详情弹窗必须盖在卡片之上（原先同为 `z-50`、DOM 在前，被卡片压住） | CDP `verify_inspect2.mjs`：inspect `z=90` / 卡片 `z=50`，`elementFromPoint(视口中心)` 命中的是 inspect 弹窗，7 项全通过 | 通过 |
| A37 | 6.2 | 环节发起时就把本轮 Prompt 写进 `part_a.sent_prompt` / `part_b[].sent_prompt` / `part_a.final_sent_prompt`，进行中点开即可查看；三者都为空时按环节状态区分说法（未开始 / 进行中 / 老记录） | `test_prompt_is_readable_while_stage_is_running`（用闸门把主创与审核卡在执行中，断言此时 Prompt 已可读且 `msg_id` 指向本轮记录）、`test_rerun_exec_failure_does_not_keep_previous_round`（失败保留本轮 Prompt、清掉简报与产物）；CDP：第一部分 1234 字符 / 2 段、waiting 显示「还没开始」、done 且为空仍显示老文案，7 项全通过 | 通过 |
| A38 | 6.2 | 协助卡片按三个环节分块：顶部三步进度条（执行 → 审核 → 交付，完成打勾、失败标「!」、进行中高亮），下面依次是「1 · 主创执行（简报 + 产物清单）」「2 · 协助审核（每位成员一个位置，标题旁 n/m 已完成）」「3 · 主创交付（交付摘要 + 对审核意见的处理 + 自己的取消/重试/查看入口）」；流水里的内部成员 id 显示为成员名 | CDP `verify_card_steps.mjs`：12 项全通过（三步存在且顺序正确、三个分块标题与内容各就位、旧两段式标题已消失、审核进度 2/2、四个「查看输入」入口）；聊天流里的折叠卡片同样显示 `✓执行 → !审核 → ✓交付` 徽标；流水实测 `agy 开始执行`（早期记录里的 `emp_agy` 也在显示层换成名字） | 通过 |
| A39 | 5.2 | CLI 启动阶段的端口抢占（`EADDRINUSE`）自动重试一次；已有产出的失败不重试，避免重复工作；重试仍失败时给出中文说明 | `tests/test_executor.py`：5 项全通过（冲突识别、零产出时重试、有产出不重试、重试耗尽提示、普通失败不重试） | 通过 |
| A40 | 5.2 | 起 CLI 子进程时只放行白名单环境变量，宿主注入的 `SERVER__PORT` / `CLIENT_INFO_*` / `CLAUDE_*` / `CODEBUDDY_*` 与环回代理一律不传；用户自配的 `CODEBUDDY_API_KEY` 等凭据与外部代理保留 | `tests/test_executor.py`：`test_host_identity_vars_are_excluded`、`test_unknown_vars_are_not_forwarded`（未来新增的宿主变量默认不传）、`test_user_credentials_and_external_proxy_are_kept`、`test_host_sandbox_proxy_is_stripped`、`test_spawn_passes_clean_env`；实测环境变量 233 → 10 | 通过 |
| A41 | 6.2 | 聊天流里的协助卡片直接给出「谁做了什么」：主创执行 / 每位协助审核 / 主创交付各一段简报，带各自状态与**该环节自己的耗时**；简报默认三行截断，可原地展开；点某一段直接打开该环节的 Prompt 与原始输出；取消与报错不把内部提示（`【任务已被用户取消】`、`【CLI 执行报错…EADDRINUSE】`）原样铺在卡片上；产物清单、实时流水、历次记录留在「完整任务详情」弹窗 | CDP `verify_chat_card.mjs`：23 项全通过（四段顺序 执行/审核/审核/交付、执行 1m14s / 审核 32s / 29s / 交付 43s、长简报默认三行且样式生效、展开后出现「收起」、点段打开对应 Prompt 且弹窗 `z=90` 覆盖卡片、运行中显示「正在执行 / 排队等待中」占位、无意见写「未提出意见」、取消写「已取消，未给出意见」、EADDRINUSE 报错折叠成「执行失败，点击查看报错详情」而模型自己写的失败说明照常展示、底部入口进大弹窗）；后端 `part_a.elapsed` / `part_a.final_elapsed` / `part_b[].elapsed` 由实测任务落库（执行 39.08s、审核取消 293.98s、交付 42.18s） | 通过 |
| A42 | 5.2 | 消息与卡片都能直接重跑：失败的显示「重试」，已成功的单任务消息显示「重新生成」（按同一份输入重写这条回答）；协助任务的每一段右下角都有自己的入口，点哪一段只重跑哪一段——主创执行段「重新执行」、每位审核段各自「重新审核」、交付段「重新生成交付」，运行中的那一段变「取消」，失败态是「重试…」，主创执行没成功时交付段不给入口；已交付后重跑执行等于整轮重来，重跑某位审核时该位重审、其余审核都有结论后自动补一次交付 | CDP `verify_row_actions.mjs`：17 项全通过（已交付卡片四段依次为 重新执行 / 重新审核 / 重新审核 / 重新生成交付、执行中卡片执行段变「取消」且排队中的审核与交付没有入口、执行失败的卡片只有「重试执行」且不能重跑交付、三次点击分别打到 `stage=exec`、`stage=review&member_id=codebuddy-ds4`、`stage=deliver`，点按钮不会误开输入输出弹窗）；后端 `test_redo_exec_on_delivered_collab_restarts_round`（已交付后重跑执行＝执行/审核/交付各重跑一次、流水写「重新生成执行」、不新增任务）、`test_redo_review_on_delivered_collab_refreshes_delivery`（重审后终稿自动更新、`deliver_stale` 落库再清除）、`test_message_and_cancel_retry_endpoints`（已交付任务拒绝取消 409、允许重新生成 200） | 通过 |
| A43 | 6.1 | 聊天里展示的始终是正文，不是 JSON：协议要求「正文正常写 + 产物清单用末尾 JSON 代码块登记」，解析时把登记块摘掉；整段 JSON 的旧写法取 `summary`；连 `summary` 都没有时把字段拼成可读文字，不把 `{...}` 原样丢给用户 | `tests/test_tasks.py`：`test_body_plus_artifact_block_keeps_body_only`、`test_whole_json_without_summary_is_still_readable`、`test_markdown_fence_with_prose_inside_is_parsed`、`test_plain_text_output_fallback` | 通过 |
| A44 | 8 | 右侧工作目录点文件在弹窗里预览：Markdown 按文档渲染（标题/列表/表格/代码块），图片直接显示，其余文本走等宽 `<pre>`，二进制只给说明并可复制路径；文本 >500KB、图片 >20MB 时提示不支持；Esc / 点遮罩关闭；弹窗内外部链接在新标签打开 | CDP `verify_file_preview.mjs` 12 项（marked/DOMPurify 已加载、点 md 行打开弹窗且 `.markdown-body` 有标题与表格/列表、不再出现原始 `## ` 与表格分隔线、弹窗 `z=92` 在最上层、Esc 关闭、`/api/workspace/raw` 拒绝非图片）+ `verify_preview_kinds.mjs` 6 项（py→text+`<pre>`、zip→binary 提示、png→img+raw 地址、点目录只展开不弹窗）；`tests/test_workspace_preview.py` 6 项（大文件返回 `too_large` 且 content 为空、raw 只服务图片、`/etc/hosts` 被拒、缺文件 404） | 通过 |

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

---

## 本轮（卡片三段化 + CLI 端口冲突）

协助卡片原先只有「第一部分 / 第二部分」两块，交付摘要塞在第一部分里，看不出「执行→审核→交付」这条线。现按三个环节分块，并加一条三步进度条（A38）。

另外修了并行审核时的 `EADDRINUSE`：`codebuddy` 启动要自己挑本地端口起服务，探测与 listen 之间有窗口期，并发起两个实例时会撞上同一端口而直接退出。执行层现在识别这类错误、零产出时自动重试一次（A39）。

回归：`Ran 68 tests in 3.7s — OK`；CDP `verify_card_steps.mjs` 12/12、`verify_inspect2.mjs` 7/7、`verify_suggest.mjs` 16/16、`verify_collab.mjs` 6/6。

---

## 补充（EADDRINUSE 的真正根因）

上一节把 `EADDRINUSE` 归因为「并发抢端口 + 探测窗口期」，**这个结论错了**。

真实原因是环境变量继承：宿主（WorkBuddy / IDE）把自己的本地服务端口以 `SERVER__PORT` 注入它拉起的每个子进程，而 `codebuddy` 启动时是 `parseInt(process.env.SERVER__PORT) || 配置里的端口`——环境里有就直接用，不再挑空闲端口。于是每个 CLI 子进程都去 `listen` 宿主自己的端口（实测 `127.0.0.1:62587`，宿主 Electron 正在监听），2 秒即退出。**不是偶发碰撞，是 100% 必现**；两个审核同时失败只是因为它们读的是同一个常量。

清理过程中还发现第二个坑：宿主注入的 `HTTP_PROXY=127.0.0.1:62616`（沙箱代理）和 `CLIENT_INFO_*` + `CLAUDE_*` 组合，会让 CLI 判定「我在 IDE 里运行」，改走 IDE 凭据通道，报 `Authentication required`。按前缀删除（黑名单）防不住——宿主注入的变量实测有 81 个且随版本变化，删了这批还有下一批。

最终改为**白名单**：只放行运行必需（`PATH`/`HOME`/`TERM`/`SHELL`/`TMPDIR`/`SSH_AUTH_SOCK` 等）+ 用户自配凭据（`CODEBUDDY_API_KEY`/`CODEBUDDY_BASE_URL`/`CODEBUDDY_CONFIG_DIR`/`ANTHROPIC_API_KEY` 等）+ 非环回代理。实测 233 个变量收敛到 10 个。

验证：单跑 4.6s 成功；并发 2 个 × 3 轮共 6 次全部成功；端到端 `/collab`（agy 主创 + DS4/HY4 并行审核）走到 `delivered`，两位审核均 `done`，无端口错误、无鉴权失败。

**部署前提**：CLI 需在本机自行登录过（`codebuddy /login`）。若看到 `Authentication required`，说明该 CLI 没有独立登录态，不是本服务的 bug。

---

## 补充（旧记录会一直显示旧的端口报错）

端口冲突修好后仍会被问到「为什么还报」。原因是**历史记录的原文不会变**：`data/sessions/*.json` 里存着修复前那次执行的 `raw_output`（`Exit code: 1 / Error: listen EADDRINUSE … 127.0.0.1:62587`），重启服务、升级代码都不会改写它，点开「输入输出」看到的仍是当时那段英文。

两件事要分开看：

- **旧记录**：11-11 22:46 那两位审核的失败原文，任务本身最终是 `delivered` 的。
- **新执行**：白名单环境实测 `codebuddy --model deepseek-v4-pro -p …` 6.4s 正常返回；宿主 233 个变量收敛到 10 个，`SERVER__PORT` 不再传给子进程。

为避免下次再误判，A45 在「输入输出」弹窗的原始输出上方加了一条解释（原文一个字不改，可复制、可排查）：

- `EADDRINUSE` / `address already in use` → 说明这是 CLI 启动端口被占、当前版本已改为干净环境启动且会重试，并提示旧记录点「重试」即可确认；
- `Authentication required` / `not logged in` → 说明是 CLI 自己的登录态失效，与本服务无关；
- 正常回复不显示任何提示条。

A45 验收（`/tmp/verify_inspect_note.mjs`，6/6）：提示条出现、渲染在弹窗内、原文未被改写、登录失效提示、正常回复不打扰、关闭按钮可点。回归：`Ran 86 tests in 3.9s — OK`；词典 259 条、界面中文 key 228 条、未收录 0。

---

## A46 · 简报与输入输出改按 markdown 渲染

模型输出的是 markdown（`## 审核结论`、`**边界条件**`、`- 列表`），交付前只做了结构化解析、没做渲染，所以卡片简报、任务详情、输入输出里看到的都是带记号的源码。三处一起改：

- **聊天卡片简报**：`briefText(row)` 交给 `renderMdText` 渲染。收起时 `-webkit-line-clamp: 3` 要求内容是行内流，所以 `.brief-clamp` 下的块级子元素统一压成 `inline`（列表项之间补 `·` 分隔、标题保留 600 字重），展开后恢复正常的段落/列表/引用块。
- **任务详情**：执行简报、每位成员的审核意见、交付摘要与「对审核意见的处理」都改 `v-html="renderMdText(...)"`，用 `.md-brief` 压掉标题与段落的大间距（不然一小段字被 margin 撑出一大块空白）。
- **输入输出**：这里存的是 CLI 完整 stdout，正文后面常挂着 ```json {"summary":…,"artifacts":[…]}``` 的产物登记。整段渲染会把登记铺成一个大代码块、正文被挤到下面——正是「还是 md 源码」的来源。新增 `splitRegistry()` 按后端 `parse_output` 的同一规则把登记块摘出来：正文用 `.markdown-body .md-compact-doc` 渲染，登记放进 `<details>` 折叠；整段就是 JSON 的老记录退回到 `summary` 字段显示。右上角加「查看原文 / 美化显示」开关，原文一个字不改。

另外 `marked` 设成 `gfm + breaks`：模型习惯用单换行分行，不开 `breaks` 会和成一个空格，简报与审核意见会挤成一坨。

验收：

- `verify_md_render.mjs` 11/11：卡片有 HTML 结构、无 `**` 漏出、收起仍 3 行；详情简报是结构化；输入输出默认渲染、可切原文。
- `verify_md_registry.mjs` 8/8：正文与登记分离、默认不铺花括号、登记折叠、原文可见、普通回复正常。
- `verify_card_md.mjs` 6/6：收起时块级压成行内且约 3 行、展开后列表恢复 `list-item`、标题 600 字重。
- 回归：`Ran 86 tests — OK`；i18n 263 条、未收录 0。

---

## A47 · 文件预览里的 mermaid 流程图

预览弹窗把 md 交给 marked 渲染，```` ```mermaid ```` 只会变成 `<pre><code class="language-mermaid">`，看到的仍是代码。现在在 DOM 落地后把这类代码块换成 SVG。

- 库放 `static/vendor/mermaid.min.js`（11.4.1，2.4MB，跟 marked / DOMPurify 一样离线可用）。**懒加载**：页面上真出现 `code.language-mermaid` 才插 script，不拖首屏。
- 用 `MutationObserver` 观察 body：聊天流、预览弹窗、任务详情都会插入新节点，不必每处各调一次；`pre.dataset.mermaidDone` 防重复处理。
- 渲染失败时保留原代码并给一行 `.mermaid-error` 说明（「不让用户看到一块空白」）；顺手清掉 mermaid 留在 body 里的临时节点。
- 初始化 `securityLevel: 'strict'`，图里的 html 标签不会被当 HTML 执行。
- 图放在 `.mermaid-box`（浅色边框、横向可滚、`svg` 居中限宽）。

验收 `verify_preview_mermaid.mjs` 6/6：用真实 md（标题 + mermaid + 表格）走 `/api/workspace/file` 打开预览 → 出图、节点文字正确、不再残留代码块、表格照常渲染、无报错提示。

## A48 · 删除单条聊天消息

聊天里每条消息右上角加了删除入口：平时不显示，鼠标移到这一条上才浮出圆形 ✕（初版 `.msg-row:hover > .msg-del`，触发范围为整行；**A53 已改为只有右上角热区才触发**）。点了先 `confirm` 再删。

- 接口 `DELETE /api/sessions/<sid>/messages/<mid>`：在 `session_lock` 里把该条从 `messages` 物理移除，返回 `{success, remaining}`；消息不存在 404、会话 id 非法 400、并发删两次第二次 404。
- **删除即不进上下文**：`build_context()` 直接读 `session["messages"]`，物理删掉后下次任务拼的 Prompt 里就不会再有这段，不需要额外的「忽略标记」。
- 前端乐观隐藏：本地用 `deletedMsgIds` 先屏蔽，避免 1.5s 轮询刚好返回旧数据把消息「闪回来」；失败时撤销标记并提示。
- 删除不影响正在跑的任务：进程继续、产物照旧落盘，只是这条消息不再出现在对话里。

验收：`tests/test_message_delete.py` 5/5（只删目标、上下文不再包含、未知消息 404、非法 id 400、重复删 404）；`verify_mermaid_delete.mjs` 16/16 中的删除部分 10 项（默认 opacity=0 且 pointer-events:none、hover 后 opacity=1 可点、点击调用接口并消失、其他消息保留、确认框取消不删）。回归 `Ran 91 tests — OK`。

## A49 · 删除消息与历史任务 Prompt 的边界

删除一条消息只影响**以后**拼出来的 Prompt，不改写**已经跑完**的任务记录。

- 历史任务里 `part_a.sent_prompt`、`part_b[].sent_prompt`、`final_sent_prompt`（以及历次执行记录的 `sent_prompt`）都是发送当时拼好的字符串快照。点开「输入输出 → Prompt」仍能看到那段被删的消息——它记录的是那次执行实际喂了什么，改了就等于篡改执行记录，事后无法复盘。
- 时间线：这条消息只对**删除之后新发起的任务**（以及点了重试/重新生成后重新拼的 Prompt）不再出现，因为 `build_context()` 每次都是现读 `session["messages"]`。
- 已经产生的输出、工作目录里的产物文件同样不会回退：模型当时确实读过。
- 固化在 `tests/test_message_delete.py::test_completed_task_prompt_snapshot_keeps_deleted_text`（删后：聊天与后续上下文均不含、历史快照仍含）。

## A50 · 卡片横栏可折叠、每段带执行时间

用户反馈：卡片信息太散、收起时文字被挤成一坨；点正文/头部都该是折叠，只有「输入输出」才弹窗。

- **头部横栏**：`@click="toggleCardClosed(msg)"` 切换整卡内容（`v-show="!isCardClosed(msg)"`），头部与底部流水保留，右侧 ▸/▾ 指示。不再打开「完整任务详情」弹窗。
- **简报正文**：`@click="toggleBrief"` 展开/收起这一段；只有右下角「输入输出 ›」按钮调 `openInspectMsg`。重跑按钮带 `@click.stop`，不会误触折叠。
- **收起排版**：弃用 `-webkit-line-clamp`（它要求行内流，之前把 `<p>/<ul>/<li>` 压成 inline 才裁得准，结果文字堆成一坨）。改成 `.brief-body.is-clamped { max-height: 156px; overflow: hidden }` + 底部渐隐——收起时看到的就是展开排版的前约 8 行，块级 `<h3>/<ul>/<li>` 全部保留。短内容不裁切，不强行撑高。
- **每段执行时间**：后端 `part_a` / `part_b[]` / `part_a.final_*` 各存 `started_at` / `finished_at`（`_execute` 返回时落库，`retry` 时清零），`build_card` 输出；前端 `briefWhen(row)` 显示「开始时刻 · 用时」，`briefWhenTitle` 悬停显示「开始 X · 返回 Y」。老任务没有这两个字段时退回只显示耗时，不出现 Invalid Date。

验收 `verify_card_fold_time.mjs` 14/14：每段标题带时间且跟耗时、收起保留块级排版（`display=block`）、收起 156px≈8 行 / 展开 182px、点正文展开再收回、点头部收起不弹窗且出 ▸、再点恢复、点「输入输出」弹窗顶部有时间。

## A51 · 输入输出弹窗显示开始/返回时间

弹窗顶部新增一行「开始 X · 返回 Y · 用时 Z」，数据来自 `inspectingMsg`。

- `openInspectMsg()` 现在把 `started_at / finished_at / created_at / elapsed / stage_label / sender_name` 一并透传。上一版只传了内容/Prompt/状态，时间字段在组装时被丢掉，导致这一行永远为空。
- 卡片每段的 `inspect`、详情弹窗的 `inspectForCard()`、历次执行记录三处入口都走同一个 `openInspectMsg`，共用这一个弹窗。
- 普通消息没有独立的 `finished_at`，用消息落库的 `created_at` 兜底当作「返回」时刻；运行中且没有返回时刻时显示「执行中」。
- 词典新增「开始 {a}」「返回 {b}」「用时 {d}」等带占位符条目，由 `t(text, params)` 替换。

## A52 · 重审不再自动生成交付（已被 A54 撤销）

> 后续用户改口，认可「所有审核都有结论就自动补一次交付」，只要求标出来源；现在的实际行为见 **A54**。

用户反馈：只重新审核了两条失败消息，最后阶段的交付却自己跑了。

- 原因：`_retry_worker` 里有一段「所有审核都有结论就自动补一次交付」，本意是避免卡片停在「执行中」，但没点过的执行不该自己发生。
- 改为：重审成功时只 `task["deliver_stale"] = True`，终稿内容原样保留可见，卡片交付段显示「意见已更新」，流水加「审核意见已更新，终稿待重新生成」。是否重出终稿由用户点「重新生成交付」决定。
- 任务状态在该轮重审结束后回到 `delivered`（终稿还在）或 `done`（没出过终稿），不再停在 `running`。
- 测试改写：`test_redo_review_on_delivered_collab_marks_delivery_stale`、`test_retry_review_only_updates_that_member` 断言交付次数不再增加、`deliver_stale` 为真。

## A53 · 删除按钮只在右上角热区 hover 时显示（用户消息的位置见 A55）

> **A55 补充**：用户自己的消息改为左上角，其余消息仍按本节保持右上角。

用户反馈：鼠标放在气泡任意位置都会冒出 ✕，应该只有放到右上角才显示。

- 旧实现 `.msg-row:hover > .msg-del` —— 整行都是触发区。改为在气泡内部放一块透明的 `.msg-del-zone`（26×26，贴气泡右上角、大半在气泡外），只有 `.msg-del-zone:hover .msg-del` 才让 ✕ 浮出并变为可点。默认仍是 `opacity:0; pointer-events:none`，防误触。
- **顺带修正定位**：旧按钮 `right:0` 相对 `.msg-row`（整行宽），对靠左的助手气泡会跑到屏幕最右边。现在 `.msg-del-zone` 放在各自气泡容器内（气泡加 `relative`），贴的是「这个气泡」自己的右上角；卡片外层补了一层 `relative w-full`，保持卡片占满整行。
- 感应区位置避开了气泡头部右侧的「回复 / 取消 / ℹ️」等按钮。

验收：`verify_mermaid_delete.mjs` 20/20（新增「hover 正文中间不显示 ✕」「hover 正文时点不到」「感应区贴气泡右上角」等）；`verify_del_zone_position.mjs` 7/7（用户气泡与靠左的助手气泡各自贴自己的右上角、hover 一条不会让另一条跟着显示）；`verify_del_realclick_card.mjs` 真实鼠标按下/抬起能命中并调删除接口；`verify_del_card_only.mjs` 6/6（卡片仍有感应区、贴右上角、卡片宽度不缩、hover 可显示可点）。回归 `Ran 92 tests — OK`；i18n 278 条、未收录 0。资源版本 `?v=20261112n`。

## A54 · 审核结论齐全后自动补一次交付，并标注来源

用户改口：「所有审核都有结论自动补一次交付挺好的，在交付那里标一下这是审核补全以后重新生成的交付」。A52 的「不自动补交付」就此撤销。

- 后端 `_retry_worker` 的审核分支恢复自动补交付，但加了去重与标注：`retry()` 点一次「重新审核」就把 `review_generation + 1`；所有审核都落在 `done / no_opinion`（有结论）时，若 `review_generation > auto_deliver_generation` 就在 `session_lock` 里认领一次（同时置 `auto_deliver_inflight`），调用 `_run_deliver(..., source="review_auto")`。两位审核并发重跑也只补一次；还有审核失败 / 超时 / 被取消时不补，只留 `deliver_stale` +「终稿待重新生成」。
- `_run_deliver` 成功后把 `deliver_source="review_auto"`、`deliver_source_at`、`part_a.final_source` 落库，`build_card` 透传；用户手点「重新生成交付」写 `manual`，正常首轮交付为空，重跑执行整轮重来时清掉标签。失败 / 取消不挂标签，并解开 `auto_deliver_inflight`；服务重启也会解锁遗留占用。
- 流水两条：「全部审核已出结论，自动补一次交付」+「审核结论已齐，系统自动补一次交付（终稿重新生成）」，紧跟原来的「主创交付完成」。
- 卡片交付段挂「↻ 审核补全后重新生成」靛蓝标签，悬停显示原因与重出时刻；「完整任务详情」的交付块同样挂标签并加一行说明。手动重出后标签消失。
- 测试：改写 `test_retry_review_only_updates_that_member`、`test_redo_review_on_delivered_collab_auto_regenerates_delivery`（断言补一次且来源正确），新增 `test_two_review_retries_supplement_delivery_only_once`、`test_failed_retry_review_does_not_supplement_delivery`、`test_manual_regeneration_replaces_auto_badge`。

## A55 · 我的消息删除按钮移到左上方

- `.msg-del-zone.is-left { right: auto; left: -10px }`，气泡模板按 `msg.sender === 'user'` 加 `is-left`；用户气泡（靠右、贴屏幕边）✕ 贴自己左上角，助手 / 系统 / 任务卡片仍是右上角。
- 触发范围不变：只有这一块 26×26 热区 hover 才浮出 ✕，hover 正文或另一个角都不会出现。

## A56 · 任务卡片显示创建时间

- `build_card` 透传 `task.created_at`（任务创建时已落库）；卡片头部与「完整任务详情」弹窗头部显示 `🕒 创建 YYYY-MM-DD HH:MM:SS`，`cardCreatedAt(msg)` 带日期，老会话没有该字段时退回卡片消息 `created_at`。
- 测试 `test_new_task_card_exposes_creation_time`。

验收：`verify_auto_badge_left_created.mjs` 14/14（用户气泡 ✕ 贴左上角且只有该热区可唤出、助手仍右上、卡片标签与悬停说明、卡片/弹窗创建时间、手动重出后标签消失）；CDP 定点截图 `/tmp/shot_left_del_zone.png`、`/tmp/shot_badge_focus.png` 目视无重叠。回归 `.venv/bin/python -m unittest discover -s tests` → `Ran 96 tests — OK`；i18n 词典 284 条、`t()` 未收录 0。资源版本 `?v=20261112o`。

## A57 · README 重写 + 演示动图重制

用户要求：重写 README、重做 GIF，示例同时包含单任务与协助任务，切换/滚动放慢让人看清。

- `README.md` / `README_zh.md` 全量重写：顶部嵌入新动图；补齐卡片四段简报、逐段重跑、审核补全自动交付标签与创建时间、消息删除位置与上下文边界、文件预览（Markdown 表格 / Mermaid）、输入输出时间、CLI 环境白名单、96 项测试与 A1–A56 验收等与旧版不符的描述。目录 11 个锚点、相对链接与图片路径均已校验存在。
- 动图 `docs/demo.gif`（中文）/ `docs/demo-en.gif`（英文）：真实界面逐帧截屏（1200×750，CDP 注入**虚构**会话），6 fps、306 帧、51 秒；每场景静止约 2.7 秒，滚动 10 步 × 2 帧约 3.3 秒，比旧版明显放慢；带编号字幕条；场景间用浅白场过渡。
- 场景覆盖：片头 → 单任务结果消息 + `/collab` 指令 + 卡片（同类画面里两类任务同框）→ 单任务输入输出弹窗（开始/返回/用时）→ 卡片头部与创建时间 → 四段简报慢速滚动 → 完整任务详情弹窗 → 「↻ 审核补全后重新生成」标签 → 文件预览弹窗（Markdown 表格 + Mermaid 流程图）→ 收尾 → 片尾要点卡。
- 生成链路：`/tmp/make_demo.mjs`（Node + CDP 截帧）→ `ffmpeg` palettegen/paletteuse（160 色、`diff_mode=rectangle`）合成；成片 4.1 MB / 3.9 MB。程序滚动会触发「回到最新」浮动按钮，已在演示样式里隐藏；过渡白场由 0.9 降到 0.55，避免出现近乎空白的帧。
- 校验：成片抽帧目视（第 60 / 225 / 299 帧）文字清晰、Mermaid 已出图、片尾完整；两版 ffprobe 均为 1200×750 / 306 帧 / 51.0 秒。

## A58 · 协助审核的四条标准与 [通过] 判定

用户要求给审核 AI 定一套标准，避免发散随机：① 审视隐患与漏洞（逻辑自洽性 / 边界条件 / 实操可用性）② 鼓励新意与巧思 ③ 精简实用、言之有物（篇幅不设上限，杜绝套话空泛理论）④ 达标即过（`[通过]` + 简述亮点，严禁硬挤琐碎细节）。讨论中用户追问并修正两处：删掉会禁掉巧思的「不要求用户没提过的新功能 / 不对未落盘设想发表意见」；边界条件从程序清单扩展成适用于方案、产品设计等产物的通用内核。

- `prompts/roles/reviewer.md` 全量重写：四条要求 + 判定基准优先级（用户原话 > 产物自述 > 通用底线，软件仍不设置任务级验收标准）+ 首行 `[通过]` / `[意见]` 契约 + 每条「现状 / 影响 / 怎么改」三要素 + 末尾 `review` 登记块。**不限条数、不限篇幅、不给重大改进设门槛**，肯定项也按用户第 4 条收成 `[通过]` + 一句亮点。
- `prompts/protocol/task-flow.md` 同步把「不对未落盘的设想发表意见」改成「审核对象限于已落盘产物；尚未落盘的替代思路可作为巧思提出，但不能当成评价现有产物的依据」，避免两份规程互相打架。
- `prompts/roles/owner.md` 交付段补三类意见处理：`[通过]` 按无需改动、隐患逐条说明修没修及理由、巧思可采纳可不采纳但要说明取舍依据。
- 后端：`parse_output` 的登记块识别集合加 `review`；`_clean_review()` 只认 `verdict=pass/issues` 与 issues/ideas 条数，漏写登记块时按正文首行 `[通过]` / `[意见]` 兜底判定；`_execute` 回传 review；`_run_review_one` 落 `review_verdict` / `review_counts`，pass 时流水写「XX 审核通过」；`build_prompt` 给主创的【审核意见】标注「[通过]，无需改动」；重跑审核 / 执行时清掉上一轮判定，避免挂旧标签。
- 前端：`cardBriefRows` 带 `reviewVerdict`，审核段状态标签旁挂绿色「通过」标签；i18n 加 2 条；资源版本 `?v=20261112p`。
- 测试 `TestReviewStandard` 7 项：规程四条标准与两条禁用文案不回归、登记块剥离与 verdict 解析、首行兜底、非法 verdict 不采信、主创 prompt 标注与老会话兼容、完整一轮 pass 链路落到卡片与交付 prompt、issues 不误挂通过。全量 `Ran 103 tests — OK`。
- 文档：协议 §4.3 新增「审核标准」小节、§9 表格同步；README 中英文补审核标准、通过标签、103 项测试与 A1–A58。

## A59 · 重审是否用最新规程 + 输入快照清理

用户问「现在点重新审核会不会使用最新的规则？如果不会，把输入快照里面的内容处理一下，我要测试效果」。

**结论：会用。** `_read_protocol()` 每次从磁盘读 `prompts/protocol/task-flow.md` 与 `prompts/roles/reviewer.md`，无缓存；`retry(STAGE_REVIEW) → _run_review_one → _execute → _execute_unit → build_prompt()` 现场重拼整份输入，并把 `part_b[].sent_prompt` 与新 `task_record.sent_prompt` 覆盖成新输入。历史快照只留档，从不作为模型输入复用。现场构造的审核输入 3088 字，含四条标准与 `review` 登记要求，不含旧规程措辞。

但输入快照里有 3 处会干扰测试观感，已清理：

- `task-flow.md` 流程概述第 2 步仍写「独立给出审核意见或优化建议」、第 3 步没提 `[通过]` 分类 → 改成「按审核规程的四条标准独立给出意见」与三类意见处理；「输出」段补审核环节必附 `review` 登记块。
- `build_prompt` 的【输出要求】原本对四个环节统一写「**如果产出了文件**才附登记块」，只读审核不产文件，模型会以为不必写登记块 → 审核环节改成专用输出要求：首行 `[通过]` / `[意见]`、分节、末尾务必附 `{"artifacts":…, "review": {"verdict":…, "issues":…, "ideas":…}}`；执行 / 交付段保持原文案。
- `retry()` 的 REVIEW 分支原先只清 `summary / raw_output / 耗时`，不清 `sent_prompt / msg_id / review_verdict / review_counts` → 点击瞬间到新 Prompt 落库之间仍挂旧规程输入，成员占用失败时还会残留。EXEC 分支同步补齐。

**存量数据的处理**：不删不改原文（保留审计、可复制排查），也不在界面加规程版本说明——这类研发过程信息不该出现在用户界面（A60）。旧快照打开就是原文；重审后新输入落库，内容里出现四条标准即可判断新规程生效。端口冲突 / 登录失效的排障解释保留，仍在两个页签之上。

- 测试新增 2 项（`TestReviewStandard`）：当前审核输入含四条标准 + `review` 登记要求、不含旧措辞且执行 / 交付段输出要求不变；存量会话挂旧快照后点重审，新 `sent_prompt` 与三次发给 CLI 的输入都是当前规程、旧判定不残留。全量 `Ran 105 tests — OK`。
- 初版曾在弹窗加旧规程提示条（`verify_review_prompt_snapshot.mjs` 4/4）；该提示已按 A60 移除。
- i18n 286 条、`t()` 未收录 0；资源版本 `?v=20261112r`；`touch app.py` 触发热重载，服务已加载新代码。README 中英、协议 §4.3 同步。

## A60 · 界面不展示研发过程提示

用户指出「这是旧版审核规程留下的输入快照……提示条也会自动消失」属于研发过程说明，不该出现在界面上。A59 的旧规程提示条就此撤掉。

- `static/js/app.js` 的 `inspectNote` 只保留 EADDRINUSE / 登录失效两类面向用户的排障解释；按旧 `reviewer.md` 文案识别的分支整段删除，`i18n.js` 对应词条一并移除（287 → 286 条）。
- 弹窗顶部提示条容器保留：端口冲突与登录失效说明仍在两个页签之上可见；普通记录、旧规程快照都不再出现任何解释条，也不显示「旧版规程」「提示条会消失」这类实现细节。
- 资源版本 `?v=20261112r`。
- 验证 CDP `verify_no_dev_note.mjs` 5/5：旧规程文本进弹窗无解释条、页面无「旧版审核规程」字样、真实卡片重审输入已是当前规程、端口冲突解释仍在、普通记录无解释条。回归 `Ran 105 tests — OK`；i18n 286 条、未收录 0。
- 实证：用户此前已自行点过重审，DS4 返回 `[意见]`（issues=2 / ideas=1），输入 3216 字含四条标准、不含旧措辞；HY4 那轮被取消但输入同样是新规程 —— 重审确实用当前规程。

## A61 · 单任务结果消息在执行中即展示 Prompt（交互详情审查）

用户给 agy 发消息后，点 ℹ️「交互详情审查」看不到发送给 AI 的 Prompt。根因：单任务的 `task_result` 消息只在**执行完成后**才被写回 `sent_prompt`，执行中点开是空的。

- `core/tasks.py` `_execute` 的 EXEC 分支：环节一开始（拼好 Prompt、进程未起前）就把 `sent_prompt` 与 `started_at` 同步写进 `task_result` 结果消息，并把状态置 `running`；`_run_single` 收尾补 `started_at` / `finished_at`。
- `openInspectModal` 直接以消息对象为 `inspectingMsg`，结果消息带上 `sent_prompt` 后，执行中点 ℹ️ 即能看到「发送给 AI 的 Prompt」，不用等跑完。
- 新增回归 `test_single_result_msg_shows_prompt_while_running`：用 gate 把 worker 卡在「执行中」，断言结果消息 `status=running` 且 `sent_prompt` / `started_at` 已就位，放行后 `finished_at` 补齐。

## A62 · 消息头部显示发送时间

用户消息此前不显示发送时刻。 `static/index.html` 的消息头部（非 system）追加 `{{ formatFlowTime(msg.created_at) }}`（墙钟时间），用户 / AI / 单任务结果消息统一带时间戳；`task_card` 自身已有「🕒 创建」不重复。

## A63 · 单任务结果消息显示「正在思考中」与实时计时

协作卡片顶部本就有计时与步骤进度；单任务的 `task_result` 消息此前运行中没有「思考中」提示、也没有顶部计时。

- 结果消息在 `running` / `queued` 且无正文时，气泡内显示 `🤔 正在思考中…`（排队时显示 `⏳ 排队中`）。
- 头部在 `running` 且已有 `started_at` 时显示 `⏱ {taskElapsed(msg)}` 实时计时（由 `nowTick` 每秒心跳驱动，与协作卡片同一套机制），完成后切回 `elapsed_time`。
- i18n 增补「正在思考中…」英文；资源版本 `?v=20261112r3`。
- 验证：`node --check` 通过；`Ran 82 tests — OK`（test_tasks）；服务热重载，刷新即可见。

## A64 · 单任务结果消息耗时统一进阶成分钟

agy（单任务）结果消息头部的耗时此前两种显示不一致：运行中用 `taskElapsed`（会进阶分钟），完成后却用原始 `elapsed_time + "s"`（超过 60 秒只显示 `120s`，不进阶分钟）。

- 新增 `msgElapsed(msg)`：`started_at` 存在时按 started/finished（或 nowTick）算实时/真实耗时并统一进阶成 `Nm Ns`；老消息无 `started_at` 则退回 `elapsed_time`。
- 头部计时改为单一 `<span v-if="msgElapsed(msg) !== ''">⏱ {{ msgElapsed(msg) }}</span>`，运行中点 ℹ️ 实时跳动、完成后统一显示为分钟，且老会话（只有 `elapsed_time`）仍可显示。
- 资源版本 `?v=20261112r4`。

## A65 · 文件预览改为右侧面板内就地展开（覆盖整个工作目录预览）

此前点文件是 `fixed inset-0` 居中弹窗，离开右侧面板。用户要求：在当前窗口（右侧面板）内就地预览、不要弹出；预览顶部带全屏与关闭；打开的预览要盖住整个工作目录预览，而不是挤在目录下面。

- 删除 `fixed inset-0` 弹窗；改为右侧面板（`relative`）内的 `absolute inset-0 z-20` 覆盖层：打开后盖住「📁 工作目录预览」标题、面包屑与文件列表；关闭（`closeFilePreview` / Esc）后回到原来的目录列表。
- 预览顶部新增「复制路径 / 全屏 / 关闭」三枚按钮：`togglePreviewFullscreen()` 用 Fullscreen API 把预览面板本身请求全屏（非整页）；`×` 关闭；`copyFilePath` 保留。
- 提示语改为「点击文件夹进入，点击文件在此面板内预览；面包屑可返回上级」。
- i18n 增补「全屏」「全屏查看」；资源版本 `?v=20261112r4`。

## A66 · 审核「只回开场白」的语义层重试与结论有效性（回归修复）

会话 `6373f3ce` 任务 `t_60f4c7b0`：审核成员 hy4-std 整段输出只有一句 `I'll read the actual artifact and surrounding context before judging.`（70 字节，耗时 24.66s），却被记成 `done`「完成」，并作为一条意见 `- hy4-std: I'll read…` 混进主创交付 prompt。

- 根因：`17d6e8a` 旧圆桌系统用 `run_with_retry()`（空/开场白/只剩工具标记 → 强指令重试一次 → 仍失败报错并提示 `--tools`）修过同类问题；`913232d` 删旧引擎、`6a68034` 重建任务系统时只保留了 `sanitize_model_output`，重试逻辑丢失（回归）。且 `hy4-std` 配的是 `--tools ""`，审核 prompt 却要读产物。
- `core/executor.py`：补回 `looks_like_incomplete()`（中英文开场白 + 空 + 极短；带 `[通过]/[意见]` 标记不算）、`employee_has_tools()`、`capability_block()`。
- `core/tasks.py`：`_execute_unit` 在**审核环节**（只读、可安全重跑）做语义层重试一次（`_review_needs_retry` + `REVIEW_RETRY_HINT`），复用同一执行单元与记录，不额外产生 `task_record`；只读环节把 `capability_block` 写进 prompt，如实说明有没有工具。`_run_review_one` 对「无 verdict 且像开场白」的回答记 `no_opinion`，并把气泡正文换成「本轮未给出有效结论…」；原始输出仍留在 `raw_output` 供「交互详情」查看。无效结论不进 `opinions` 筛选，不再污染主创交付。
- 配置：`hy4-std` / `hy3` 的 `--tools ""` 改为 `--tools "Read,Glob,Grep"`（只读；写权限仍为 False）。`data/settings.json` 已备份为 `.bak`。
- 与既有网络/系统层重试（`CLI_PORT_CONFLICT_RETRIES` 处理 EADDRINUSE）并列：这是**语义层**重试，针对「进程正常退出但没给出实质结论」。
- 验证：`tests/test_tasks.py::TestReviewSemanticRetry` 4 项（识别英文/中文开场白、只回开场白→重试后无意见且不进交付 prompt、开场白后重试成功采用后答、内容充实无标记不重试）；全量 `Ran 110 tests — OK`。


## A67 · 成员权限拆成「读 / 写」两个开关，新建成员默认全开

新建的成员即便什么都没勾，也不该连产物都读不到——这是 `--tools ""` 的直接后果。

- 根因（两处）：
  1. `data/scanned_models.json` 是**旧列表格式**缓存，里面每条 `cli_cmd` 都是 `codebuddy --model X --tools "" ...`，而 `_load_cache()` 对列表格式不做 TTL 判断、也不修模板 → 「添加成员」下拉里选出来的就是死模板，与写权限勾选框无关。
  2. UI 只有一个勾选框，文案在「只读 / 可写」之间翻转，默认不勾 = 只读；而只读成员的模板若带 `--tools ""`，运行时也没有任何补读工具的逻辑 → 成员读不到文件，只会回一句「我先读取一下」。
- 权限模型：`read_access`（新字段，默认 True）与 `write_access` 并存。
  - 读：`Read,Glob,Grep`；写：再加 `Write,Edit,Bash` 与 CLI 自动放行参数（agy `--dangerously-skip-permissions`，codebuddy `-y`）。
  - 两个都关：`--tools ""`，即完全不借助工具（`capability_block` 会告知模型不要回「我先去读」）。
- `core/executor.py`：新增 `compose_tools_value()` / `normalize_cli_tools()`（模板级修补）与 `apply_access_mode(args, write, read)`（运行时补参数）；只读角色也会补 `Read/Glob/Grep`，`--tools default/all` 视为用户显式放开、不动。
- `core/scanner.py`：读缓存时统一按只读权限修补模板，历史缓存自愈（旧列表格式里的 `--tools ""` → `Read,Glob,Grep`）。
- `core/storage.py`：默认模板改为带读工具；`load_settings` 为老配置补 `read_access=True`（缺失即视为有读权限，避免升级后成员突然读不到产物）。
- `app.py`：`_sanitize_agent` 持久化 `read_access`，并在落库前按权限规范化模板 —— 界面上看到的模板 = 实际执行用的模板。
- `static/index.html` + `app.js`：成员列表行改为「👁 读 / ✍️ 写」两个勾选框；添加/编辑成员弹窗拆成两个带说明的勾选框，**新建默认两个都勾**；勾选变化时 `syncEditingEmpCli()` 立刻把 `--tools` 同步到模板输入框。资源版本 `?v=20261112r5`。
- 验证：`TestAccessMode` 7 项（只读补读工具、写全开、全关、写开读关、agy 只补开关、模板自愈、`default` 不动）+ `TestScannedModelsCacheHeal`（旧缓存自愈）；全量 `Ran 118 tests — OK`。CDP 实测：列表两勾选框均为勾选态，弹窗两个勾选框默认勾选，模板随勾选联动为 `Read,Glob,Grep,Write,Edit,Bash` → `Write,Edit,Bash` → `""`。

## A68 · 进行中的卡片一律显示开始时刻 + 实时计时；设置默认进「成员管理」

用户反馈：点「重新生成」后 hy4-std 那一段既没有开始时间也没有计时；顺带设置打开时默认停在「界面语言」而不是常改的「成员管理」。

- 根因（计时）：
  1. `part_b` 的审核项与 `part_a.final_started_at` 都只在**跑完**才写 `started_at`，进行中这段时间字段是空的 → `briefWhen()` 无内容、无法计时。
  2. 「重新审核」只把 `task.status` 置 running，没刷新 `task.started_at`，头部 `taskElapsed` 会接着整条任务最早的开始时刻算（看起来像跑了半小时）。
  3. 进行中的占位文案是静态句号串，没有「在跑」的动感。
- 后端 `core/tasks.py`：
  - `_execute_unit` 在 EXEC / REVIEW / DELIVER 起点分别写入 `started_at` / `final_started_at`（并把对应 `finished_at` 清空），进行中即可读。
  - `retry()` 三个环节都重置 `task["started_at"]`，本轮计时从点下去那一刻算。
- 前端：
  - 新增 `cardIsBusy()` / `cardClockStart()`：优先取「正在跑的那一段」的开始时刻；`taskElapsed()` 改为按它算，运行中由 `nowTick` 心跳驱动。
  - 卡片头部在 `cardIsBusy` 时多一行 `▶ HH:MM:SS`（本轮开始时间），计时文字转蓝色；每段标题后的 `briefWhen()` 改为实时计算（`briefElapsedSec()`）。
  - 新增 `.thinking-dots`（三点依次亮起 + 轻微上浮，`prefers-reduced-motion` 下静止）；「正在执行，简报生成后会显示在这里」「排队等待中」「正在思考中」三处统一去掉静态省略号、改挂动画点。
  - `openSettings()` 与 `settingsTab` 默认值改为 `members`。
  - 资源版本 `?v=20261112r6`。
- 验证：新增 `TestRunningCardClock` 2 项（审核进行中即有 `started_at` 且 `finished_at` 为空；重新审核会重置卡片计时）；全量 `Ran 120 tests — OK`；i18n 297 条未收录 0。CDP 注入一张运行中卡片实测：头部 `▶ 20:42:04` + `⏱ 1m 16s`，hy4-std 段 `20:42:04 · 1m16s` + 进行中 + 动画占位；设置默认页签为「成员管理」；`.thinking-dots` 动画 `dotBlink 1.2s` 生效。

## A69 · 聊天里的文件名可直接点开预览；卡片每位成员独立成块

- 需求：① 聊天气泡里的文件名点一下就能在工作目录预览面板里读到，不用先去目录树里找；② 协作卡片里几位成员的内容挨在一起，看起来像一整段密密麻麻的文字，要能一眼分出「谁说了什么」。
- 文件名可点（`static/js/app.js`）：
  - `renderMdText()` 渲染后统一过一遍 `linkifyFilePaths()`：行内代码 `` `A01.md` `` 与**裸文本**两种写法都认（模型实际输出几乎都不写反引号，还会把名字夹在《》里、后面跟 `:65-83` 行号）。裸文本走 `findFileTokens()`：以「行首/空白/中英标点」为左右边界，只包住文件名本身，行号留在可点范围之外；URL（`https://…` 的尾部、`//` 开头）、`Node.js` 这类框架名、`write_access` 这类标识符都不包。
  - 点击用 `document` **捕获阶段**的委托（`onDocFileClick`）接管并 `stopPropagation()`，否则点文件名会连带触发父级的「展开/收起」。
  - `openWorkspacePath()`：相对路径按 `currentSession.workspace_dir` 解析，`normalizeFsPath()` 处理 `./ ../`，绝对路径必须仍在工作目录内，否则提示「这个文件不在当前工作目录里」。
  - 裸文本里常粘着「参考」「见」这类前缀（`参考亲子教育手册.md`）：先按整串找不到时，用 `matchFileBySuffix()` 到同级目录里按「目录项是它的后缀」再匹配一次，取最长命中。找不到才提示「找不到这个文件」。
  - 打开前 `focusWorkspaceDir()` 把右侧列表切到该文件所在目录（面包屑同步），关掉预览看到的就是文件所在的地方。
  - 单任务结果消息新增产物 chips（`msg.artifacts`），卡片详情弹窗的「产物清单」也从纯文本改成可点。`openArtifact()` 会先关掉盖在上面的弹窗，避免「点了没反应」。
  - `.file-link` 用淡蓝文字 + 虚线下划线（不是整块底色），长中文文件名不会糊成一片蓝。
- 卡片分块（`static/index.html`）：三段简报的容器改为 `bg-slate-50/80 p-2 space-y-2`，每位成员一个 `rounded-lg border bg-white` 的小块，左侧 3px 状态色条（`rowBarClass()`：进行中蓝 / 失败红 / 取消灰 / 无意见黄 / 完成绿），段头加头像圆点（`rowAvatar()`）+ 角色 chip，操作行（展开全文 / 输入输出 / 重跑）用虚线分隔。`cardBriefRows()` 给主创的两段补上 `memberId` 以便取头像。
- 验证：全量 `Ran 120 tests — OK`；i18n 301 条未收录 0；资源版本 `?v=20261112r9`。CDP 实测（注入真实格式的回复）：`《…计划.md:65-83》`、`产物 …A01….md（主要产物）`、`docs/README.md` 均成为可点，`https://example.com/a.md` 保持普通链接，`Node.js` 不误判；点 `参考亲子教育硬核手册_八个内容组完整目录.md` 经后缀兜底打开真实文件（8325 字），面包屑停在 `social skill`；产物 chips 点击打开 3827 字；卡片截图确认 4 段各自成块、色条与角色 chip 正常。

## A70 · 恢复流式输出：执行中就能看到「正在写什么」

用户反馈：之前有流式输出，重构后没了，任务跑几十秒只有「正在思考中」，感觉很慢。

- 根因：`913232d` 清旧圆桌系统时把 `core/streaming.py` 与 `Executor._stream_process` 一起删掉了，
  新的任务系统只走 `p.communicate()`（进程结束才拿到整段 stdout），所以界面在整个等待期只能显示占位。
- 还有个更深的原因：两边 CLI 的 `-p` 默认输出格式都是 `text`，本来就「跑完才吐」。
  只恢复增量读取没用，必须同时把输出格式换成流式：
  - `codebuddy`：`--output-format stream-json --include-partial-messages --verbose`
  - `agy`：`--output-format stream-json`（它没有 `--include-partial-messages`，多给会报错）
  - 只在模板自己没写 `--output-format`、且是 `-p/--print` 模式时注入，模板本身不改。
- 后端：
  - 新增 `core/streaming.py`：进程内暂存区（`{tid}:{stage}:{member_id}` → 文本 / 阶段 / 工具活动），
    `for_session()` 取整场并顺手清过期项，单条上限 20 万字符；会话删除时 `clear_session()`。
  - `core/executor.py`：恢复 `_stream_process()`（双线程抽干 stdout/stderr、`read1` 增量解码、回调限流 0.33s）
    并新增 `StreamJsonParser`：兼容两家的形状 —— codebuddy 的 `stream_event/thinking_delta/text_delta`、
    `assistant.tool_use`（把「读哪个文件、跑什么命令」显示成活动）与 `result`；
    agy 的 `step_update.text_delta` 与 `result.response`。最终文本一律取 `result` 事件，
    与界面看到的完全一致；非 NDJSON（模板另有打算 / 版本差异）自动退回纯文本。
  - `run_employee` / `_run_cli` 透传 `on_chunk`；端口冲突重试时换新解析器，不把上一轮的半截文本带过来。
  - `core/tasks.py`：`_execute_unit` 按执行 key 发布流、`finally` 里清空；审核语义重试前先清一次；
    执行器不支持 `on_chunk`（假执行器）时自动关闭流式，老测试夹具不受影响。
  - `app.py`：新增 `GET /api/sessions/<id>/live`（204/空对象都很轻），只回正在生成的文本。
- 前端：
  - `app.js`：`liveStreams` / `liveLast` 状态，500ms 轮询（只在有活动任务时发请求）；`liveKeyOf()` 把
    卡片每一段映射回 `{tid}:{stage}:{member}`，`liveTail()` 只留尾部 900 字，`liveForRow()` / `liveForMsg()`
    分别服务协作卡片与单任务气泡；`liveLast` 兜住「刚跑完、正式结果还没轮询回来」的一瞬，不闪回占位。
  - `index.html`：卡片每段与单任务气泡新增实时块 —— 阶段 chip（正在思考 / 正在输出 / 正在读取 · 工具名）+
    字数 + 纯文本尾部 + 闪烁光标（`.stream-live` 顶部淡出、`.typing-caret`）；没有实时文本时才显示原来的动画省略号。
    流式内容按纯文本渲染：半成品 Markdown（未闭合代码块、只写一半的表格）会让整块反复变形。
  - 资源版本 `?v=20261112r10`。
- 验证：
  - 新增 `tests/test_streaming.py` 16 项（暂存区隔离/过期/上限、两种事件形状、半行重组、注入规则、
    透传与清理、假执行器兼容）；全量 `Ran 136 tests — OK`。
  - 真实 CLI：codebuddy 首段文本 5.0s 到达、13.1s 完成，`/live` 轮询看到 19 次增长（thinking → writing），
    结束后 `streams` 立刻为空；agy 8.3s 起逐段到达、最终文本与 `result.response` 一致。
  - CDP 实测：单任务气泡在 16s 显示「正在输出 53 字」+ 正文 + 光标，结束后只剩正式回答；
    协助任务卡片在交付段显示「正在思考 2618 字」+ 实时文本，三段状态与计时正常。
  - 顺带修掉一个偶发失败：`test_retry_review_only_updates_that_member` 在 `wait_idle()` 后立刻断言终态，
    而「审核齐了自动补一次交付」是异步线程，机器负载高时会读到 `running`。改为等终态（`_wait`），
    连跑 8 轮全绿。

## A71 · 预览实时刷新 / 就地编辑、单任务气泡折叠、默认接话成员、刷新不影响执行

用户一次提了五件事：

1. **预览中的文件被改写要能实时刷新，并且能直接改、直接存**
   - 后端 `/api/workspace/file` 增加 `meta=1`：只回 `mtime / size`，不读正文。
     预览面板每 1.2s 探一次指纹（一本几百 KB 的文档不会每秒读一遍盘），
     变了才重新取正文；图片则用 `&t=<mtime>` 换 URL，绕过浏览器缓存。
   - 新增 `POST /api/workspace/file` 保存：只允许纯文本白名单后缀（`EDITABLE_EXTS`），
     写「同目录临时文件 + `os.replace`」保证原子性，保留原文件权限；
     带 `base_mtime` 做冲突检测，期间被 AI / 编辑器改过返回 409 + `code=conflict`，
     前端二次确认后带 `force=1` 覆盖。
   - 前端：面板顶部「✏️ 编辑 / 全屏 / 复制路径」（A73 去掉了「刷新」与「实时刷新」两个按钮，
     自动刷新保持常开）；编辑态是等宽 textarea，⌘/Ctrl+S 保存、Esc 取消（不会顺手关掉整个面板），
     未保存时标题旁标 `● 未保存`；自动刷新时右上角显示「刚刚已更新 / N 秒前已更新」。
     编辑期间暂停自动刷新，避免把草稿冲掉。

2. **拖右栏改变宽度后，长气泡被切掉却看不到后面的内容**
   - 根因：折叠限高 `.msg-clamped` 一直生效，但「有没有被截断」只在消息**条数增加**时测量。
     单任务回答是先建一条空消息、正文随后填进去（条数没变）→ 从不测量 →
     内容被切掉、`展开全文` 按钮也不出现，看起来就是「挤下去了又翻不到」。
   - 修法：`pollActiveSession` 只要会话签名变化就重新测量；`ResizeObserver` 监听聊天区
     （拖右栏、缩放窗口都会触发），合并 120ms 内多次触发；拖拽结束同样补一次。
   - 顺带把中间列补上 `min-h-0`、聊天区加 `overscroll-contain`；
     拖拽改用 blur 兜底清理（鼠标在窗口外松开时 `mouseup` 收不到，页面会卡在
     `col-resize` + 不能选字的状态继续跟着鼠标改宽度）。

3. **单任务回答也要能展开 / 收起，高度约 20 行**
   - 新增 `.msg-clamped-task`：`max-height: min(calc(20 * 1.7em), 64vh)`；
     `msgClampClass(msg)` 按消息类型选档，卡片里的长文仍是原来的一屏；
     正在跑、还没有正文的消息不折叠。

4. **消息里没 @ 任何人时，默认跟最近沟通过的 AI 说话**
   - `core/tasks.py` 新增 `last_ai_member()`：从后往前找本场会话里回过话、
     且现在仍可用（enabled + 有 CLI 配置）的成员；没有 @ 时用它作为 owner。
     引用回复 / 显式 @ 仍然优先；一个可用成员都没有时，消息只作上下文保留。
   - 用户消息写入 `auto_to = {id, name}`（只作数据留痕，A73 起不再在气泡上显示提示）。

5. **刷新页面会不会影响正在跑的 AI；为什么刷新后出现「● tool」**
   - 任务跑在服务端线程里，状态与会话都落盘，刷新只重建界面，不会终止执行；
     `/live` 是进程内暂存，刷新后下一轮 500ms 轮询就能重新接上。
   - 「tool」是因为 agy 的 `step_type` 只有笼统的 `"tool"`，旧代码把它当活动文案直出。
     现在解析 `tool_name` + `tool_info.parameters`（`read_file · /tmp/note.md`），
     前端 `toolLabel()` 再翻成人话（读取文件 / 执行命令……），
     认不出的泛型词统一显示「正在调用工具」。
   - 实测：agy 读文件时活动为 `read_file · /tmp/agy_probe_note.md`（13.4s 完成）。

验证：
- 新增测试 9 项（meta 指纹、保存/冲突/force/二进制拒绝/越界拒绝、默认接话 4 项、
  agy 工具名解析 2 项）；全量 `Ran 148 tests — OK`。
- 无头浏览器实测：长回答限高 476px（20 行）+「展开全文」，展开后 963px；
  外部进程改写 `notes.md` 后 1.2s 内面板内容与「刚刚已更新」同步；
  面板内编辑保存后磁盘内容一致、toast「已保存」；
  回答「稍后到达」时（条数不变）也能在下一轮轮询后出现展开按钮。
- 资源版本 `?v=20261112r11`；i18n 338 条，未收录 0。

## A72 · 修复 agy 回答「同一句话重复三遍、链接被截断」

用户反馈：一条 agy 的回复开头变成

```
已按要求完成对 [亲子教育硬核手册_…A01_一
已按照要求完成对 [亲子教育硬核手册_…_标杆场景_
已按要求完成对 [A01 实操指南：开工防磨蹭清障法](file:///…E4%BA%B2…md) 的全面修订，具体改动如下：
```

- 定位：`raw_response` 本身就是错的（不是渲染问题）。该任务跑在 agy 上（`emp_agy`，293 秒）。
- 根因：agy 的 stream-json 里，模型每次「说到一半去读写文件」都会结束当前 `agent_response` step、
  换一个 `step_index` 重新开口。旧解析器把所有 step 的 `text_delta` 首尾相接放到 `self.text`，
  `result` 事件缺失时 `final_text` 用这个拼接结果 → 两段半截草稿 + 一段完整回答串成一条。
  三条证据：① 三段 URL 写法各不相同（未编码 / 未编码 / 百分号编码），是三次独立生成而非流式重发；
  ② 每段都以完整句子开头、中间没有连接词；③ 只有中间两段「话没说完」。
- 修法（`core/executor.py`）：
  - `StreamJsonParser` 增加分段缓冲 `_segments`：agy 按 `step_index`、codebuddy 按 `message_start`
    切换段落，`text_delta` / `thinking_delta` 只写进当前段。
  - `live_text` / `final_text` 都只暴露**最后一段**；`result` 事件存在时仍以它为准（权威答复）。
  - `self.text` 保留全量，只用于调试与兼容旧断言，不再参与最终输出。
- 历史数据：把这条消息、以及同一任务的 `task_record` 副本按「最后一次出现的开头」截取
  （1110 → 898 字），改动前备份在 `/tmp/6373f3ce.bak.json`。
- 验证：新增 3 项回归测试（agy 多 step 只留最后一段、codebuddy 第二条消息覆盖第一条草稿、
  result 优先于 step 草稿）；全量 `Ran 151 tests — OK`；真实 agy 改文档（257 秒，含 edit_file）
  输出 408 字、无重复开头；界面复查该消息只显示一段完整回答。
- 另外扫了全部会话：其余「重复前缀」命中都是正文里的列表项（`- **版本 A`、`- \`A01…\``），
  不是解析问题，未改动。

## A73 · 去掉「默认发给」提示与预览顶部的两个刷新按钮

- 聊天气泡：用户消息下方不再显示「↪ 默认发给 XXX」。默认接话逻辑完全保留
  （`last_ai_member()` 仍决定 owner），`auto_to` 字段继续写入会话，只作数据留痕。
  同时清掉「默认发给」「这条消息没有 @ 成员…」两条 i18n 词条。
- 文件预览顶部：去掉「🔄 刷新」与「⚡ 实时刷新（可暂停）」两个按钮，只留
  「✏️ 编辑 / 全屏 / 复制路径 / ×」。自动刷新改为常开（1.2s 探一次 mtime，变了才读正文），
  编辑期间照旧暂停；「刚刚已更新 / N 秒前已更新」提示保留。
  随之删掉 `togglePreviewAutoRefresh()` / `refreshPreviewNow()` 与 `previewAutoRefresh` 状态，
  以及 4 条不再使用的词条。
- 验证：无头浏览器实测 `pageHasLabel=false`、`msgHasAutoTo=true`（数据在、提示不在）；
  预览面板按钮列表里 `hasRefresh=false`、`hasLive=false`，编辑/全屏/复制路径仍在。
  i18n 331 条、未收录 0；资源版本 `?v=20261112r12`。

## A74 · 中断续跑：重试 / 新指令都能拿到「上次做到哪」

- 问题：成员每次执行都是新的 CLI 进程（不传 `--session-id` / `--resume`），
  模型内部的思路、读过的文件全都不留；失败时连已经输出的正文也一起丢了
  （`streaming.clear()` 清暂存区，聊天里只剩一句「CLI 执行报错」）。
  结果就是「点重试」≈ 从头再来一遍，用户还得自己提醒「接着上次的写」。
- 修法（`core/tasks.py`，提示层，不动进程模型）：
  - 执行结束若落在「失败 / 超时 / 已取消」，把两样物证写进 `task["interrupt"]`：
    中断前的半成品正文（流式暂存区最后一个快照，截 4000 字）+
    那次执行期间被改动过的文件（`scan_recent_files()`：跳过依赖/缓存目录、
    最多 4000 条目 / 6 层深、按 mtime 倒序取 20 个）。
  - `render_resume()` 生成交接条，`build_prompt(resume=…)` 把它排在【用户原话】之后：
    同一环节重试 → 【上次中断的进度】+【续跑要求】（先读已有文件、写完的不要重做、
    要重写就说明理由）；**换了新指令** → 【上一条任务留下的半成品】，只给文件线索
    不给旧正文（`recent_interrupt()`，限同一位成员、2 小时内），并注明「与本条无关就忽略」。
  - 半成品正文同时挂到执行记录 `raw_response` 前面，「输入输出」里仍能看到中断前写到哪。
  - 成功跑完则清掉 `interrupt`，避免下次重试误以为没跑成。
- 验证：新增 8 项测试（交接条渲染、失败/取消落库、重试 Prompt 带续跑要求、
  新指令只拿文件线索、成功即清空、换人/超 2 小时不打扰）；全量 `Ran 159 tests — OK`，
  连跑 8 轮全绿。真机：给成员 6 秒超时让 codebuddy 跑到一半被打断 →
  交接条记下那句英文自述与 `book.md`，放开超时点重试，第二轮 Prompt 里
  `【上次中断的进度】` 与 `【续跑要求】` 都在，任务一次交付成功。
- 已知边界：这是「物证交接」，不是真正的上下文续跑——模型没读过的东西它仍然不知道；
  需要连思考链路一起续上的话，得接 CLI 原生 `--resume`（codebuddy `-r/--resume`、
  agy `--continue`），当前未实现。

## A75 · 一条消息 @ 多位：并行分工，每位各出一份

- 问题：普通消息 `parse_single()` 只取第一个 `@`，`@a @b @c 各写一份去北京的攻略`
  只有 a 收到任务，b、c 静默无感，界面也没有任何提示。`/collab` 顶不上——
  它是「一位主创 + 其余审核」，产出一份终稿，语义正好相反。
- 修法：
  - `parse_fanout()`：普通消息（非 `/collab`、非引用回复）里 @ 到 ≥2 位可用成员时，
    为每位各建一条单任务，共享 `source_msg_id` 与同一条用户原话，用 `fanout_id` 归组。
    重复 @ 同一人自动去重；有人已停用 / 缺 CLI 配置则整条消息拦截（少发一份比发错更难发现）。
  - 渲染不动：单任务本来就是「一条任务 → 一条回答气泡」，三条任务天然是三条并列回答，
    各自带取消 / 重试 / 重新生成。不同成员各走自己的队列，所以并行且互不牵连。
  - 产物冲突：三人同处一个工作目录，Prompt 里加【并行分工】段，要求文件名以自己的前缀开头
    （`agy-攻略.md`、`DS4-攻略.md`）；前缀取括号里的短名（`DeepSeek-V4 (DS4)` → `DS4`），
    并写明同批还有谁，避免模型自我发挥。
  - 互相可见：兄弟任务彼此看不到对方的答案（避免趋同与被最慢那位拖成串行）。
  - 输入框预览：新增 `POST /api/mentions/scan`，前端 250ms 防抖后显示
    「将分别发给 A、B、C：各自独立作答，互不参考」。谁被 @ 到一律由服务端判定
    （支持别名、`@//agy/`、散落在句子里），不在前端另写一套匹配。
- 验证：新增 8 项测试（三条任务/三条气泡、各自 Prompt 前缀、单 @ 不变、重复 @ 去重、
  停用成员拦截、一位失败不影响兄弟且重试只重跑它、`/collab` 不受影响、前缀规则）；
  全量 `Ran 167 tests — OK`。真机三成员并行（agy / DeepSeek-V4 / 混元-4）：
  3 条任务同 `fanout_id`、3 条回答气泡、全部 delivered，各自 Prompt 都带【并行分工】。
  无头浏览器：`@agy @DS4 @HY4 …` 时提示条渲染出三位成员名，单 @ 与 `/collab` 均不出现。

## A76 · 修掉 `./run.sh restart` 必然超时：继任进程不再继承父进程 stdio

- 现象：执行 `./run.sh restart` 稳定卡满 30 秒后报「没等到新进程接管端口」，
  且此后端口 5050 无人监听 —— 不是「重启慢」，是**新进程根本没起来**。
- 根因（实测坐实）：`_spawn_successor()` 用 `subprocess.Popen` 拉起继任进程时没指定
  stdio，于是它继承了父进程的 stdin/stdout/stderr。父进程随即 `os._exit(0)`，
  那根管道 / 伪终端的**读端跟着消失**；继任进程写第一个 `print()`（启动横幅）
  就拿到 EPIPE/EIO，Python 退出时还要 flush 一次 stdio，失败即连退出码一起变成 120。
  旧进程已经走了、新进程又秒退，端口自然空了。
  验证：把 `app.py` 作为子进程、stdout 接到读端已关闭的管道 → 存活 False、退出码 120、
  端口从未监听；同样子进程 stdout 指向文件 → 正常退出。
  （这也解释了为什么「从启动它的那个终端里 restart」有时能成，而在后台管理器/工具会话里必挂。）
- 修法：
  - `_spawn_successor()` 显式给继任进程 `stdin=DEVNULL`、`stdout/stderr` 指向
    `.run/service.log`（`start_new_session` 不变）。继任进程从此与父进程的终端脱钩，
    父进程怎么死的都不影响它。
  - `_SPAWN_WAIT_CODE` 补日志：接管开始、等旧进程超 60 秒放弃、execv 失败各写一行；
    日志超 2MB 轮转一代（`service.log.1`）。
  - 继任实例启动时自己刷新 `.run/app.pid`，避免从 Web 界面触发重启后 PID 文件过期。
  - `/api/system/restart` 的响应带上 `log_file`；`./run.sh restart` 成功后直接打印日志路径，
    失败时自动 tail 日志最后 20 行——以前超时只给一句「看看终端有没有报错」，等于没说。
  - 新增 `./run.sh logs` 跟踪日志。
- 代价：restart 之后新实例的请求日志不再打在终端，而去 `.run/service.log`。
  想在终端看日志就 `./run.sh stop && ./run.sh start`。
- 验证：连续 `./run.sh restart` 三次全部成功（PID 24584 → 24696 → 24785 → 24829），
  日志里能看到「旧进程 pid=… 拉起继任实例」与完整启动横幅；新增
  `tests/test_service_restart.py` 7 项（stdio 不继承、日志标记、非 py 入口拒绝、
  轮转、死管道必死对照实验、接口鉴权与日志路径），全量 `Ran 174 tests — OK`。

## A77 · 模型选择改为「基础模型 + 推理档位」，扫描覆盖本机所有已装 AI CLI

- 现象：成员管理里「本地已安装的模型」只扫 `agy` / `codebuddy` / `opencode` 三个写死的分支；
  本机已装的 `claude` / `codex` / `cursor-agent` 完全不出现。`agy models` 里的
  `gemini-3.8-flash-{low,medium,high}` 被当成三个不同模型摆在下拉里；编辑成员时
  看不出已绑定的模型；模型列表常驻展开占满弹窗；会话成员行右侧的「移出」按钮可误点。
- 修法：
  - 新建 `core/cli_registry.py`：常用大模型 CLI 的注册表（名称、模型枚举命令、解析方式、
    执行模板、推理档位落法、写权限开关、凭据环境变量）。`scanner.py` 遍历注册表，
    只纳入 `shutil.which()` 找得到的 CLI；`executor.py` 的白名单 / `_WRITE_FLAGS` /
    环境白名单同步由注册表生成，杜绝「能选不能跑」。
  - 推理程度统一低 / 中 / 高、默认高：agy 有 `-low/-medium/-high` 变体时用真实变体 ID，
    否则用 `--effort`；codebuddy 用 `--effort`；codex 用 `-c model_reasoning_effort=`；
    opencode 用 `--model <id>#<variant>`；claude / cursor-agent 无此参数，只给「默认」单档。
    每个档位自带完整 `cli_cmd`，前端不拼命令。
  - `opencode` 只保留自家 provider（`opencode` / `opencode-go`），过滤 github-copilot / gitlab。
  - 成员配置新增 `model_key` / `reasoning`（`_sanitize_agent` 白名单保留）；会话新增
    `member_models[emp_id]` 覆盖表（`_clean_member_models` 校验），
    `TaskManager._settings_for_session()` 在 `_run_task` 与 `_retry_worker` 两条执行路径上
    替换 cli_cmd，全局设置不受影响。
  - 前端：模型选择器默认收起、点击才展开，浮层用 `<Teleport to="body">` + fixed 定位避免被弹窗
    `overflow-y-auto` 裁剪；触发器回显「模型 · 推理: 高」；选模型后展示该模型档位按钮。
    设置菜单把「成员管理」挪到第一位。会话成员行改为点开选档位（仅当前会话），
    去掉「移出」，成员增删走下方「调整 / 增减成员」。
  - `data/settings.json` 五个既有角色统一改为高推理：agy → `gemini-3.8-flash-high`，
    其余 codebuddy 角色补 `--effort high`，人设 prompt 与权限不变。
  - `Executor._run_cli` 的 Popen 补 `stdin=DEVNULL`：prompt 全走 argv，避免 codex 等 CLI
    等 stdin 输入。缓存结构版本升到 3，旧缓存自动重扫。
- 验证：全量 `Ran 184 tests — OK`（新增 8 项：档位聚合、agy 回退、opencode provider 过滤、
  遍历已装 CLI、各 CLI 模板合法、会话覆盖与安全拦截、会话 API 校验、内置角色配置）；
  `/api/scan_models?refresh=1` 返回 80 条 / 6 个 CLI（agy 4、codebuddy 19、opencode 44、
  claude 4、codex 6、cursor-agent 3），无 github/gitlab；会话 override 的 PUT / GET、
  非法命令 400、普通字段保存不清覆盖均通过；Vue 两段新模板编译通过，搜索 / 免费过滤 /
  编辑回显 / 档位选择 / 会话覆盖请求体逐项跑通；opencode 端到端仍返回 `OK`。
- 已知限制（本机环境，非代码问题）：claude 报 API 404、cursor-agent 未登录、
  codex 的 `~/.codex/config.toml` 里 `gpt-6.1-sol` 被 ChatGPT 账户拒绝；三者命令参数均已
  被 CLI 正确解析，需在各自 CLI 登录或换用账户支持的模型后才能实际产出。

## A78 · 成员名带空格：补齐 @ 候选与 Backspace 整删

- 现象：把 `emp_agy` 的 `name` 从 `agy` 改成 `gemini 3.8` 后，服务端解析与任务执行都正常 ——
  `_alias_pattern` 把空格当「可有可无的分隔符」，`submit_message` 按 `owner_id` 派发，
  `cli_cmd` 里是 model_id，名字只做展示。隔离夹具实测 `@gemini 3.8 只回复OK` →
  `owner_id=emp_agy`、`status=delivered`、argv 里完全没有名字。
  但前端两处仍沿用「名字是单 token」的旧假设：
  ① `onInputSuggest` 的 `([@/][^\s@/]*)$` 让候选在手打到 `@gemini 3` 时关闭，
     `/collab` 后 implicit 候选的 `!/[\s@/]/` 同理；
  ② `onInputKeydown` 的 `/@([^\s@]+)\s?$/` 让 `@gemini 3.8 ` 无法整删，退化成逐字删。
  这不是改名才引入的问题：模型下拉自动填的 `Gemini 3.8 Flash` 本来就带空格。
- 修法（`static/js/app.js`）：
  - 新增 `spacedMentionToken()`：单 token 正则失配时，只在「@ 后面仍是某位会话成员
    名字 / id 的前缀」时续开候选；写完正文、打错字、`a@b` / 路径一律照旧关闭。
  - implicit 分支同样改用「名字 / id 前缀」判断，不再依赖「不含空白」。
  - 新增 `spacedMentionTag()`：Backspace 在单 token 正则之外，只认「@ 后面恰好是完整的
    成员名 / id / 括号短名（可带一个尾空格）」。刻意不放宽成 `@[^@]+`，否则
    `@gemini 3.8 请写代码` 会被一次删掉整句话。
  - `static/index.html` 的 `?v=` 升到 `20261113r2`（i18n 无新增文案）。
- 验证：`node --check static/js/app.js`；Node `vm` 模拟 22 项交互全 PASS（带空格候选、
  implicit 候选、整删，旧单 token 行为、邮箱 / 路径不弹、正文不误删、
  候选插入 `@gemini 3.8 `）；全量 `Ran 184 tests — OK`；线上
  `/static/js/app.js?v=20261113r2` 返回 200 且包含新方法。
- 边界：`@agy` 仍不匹配（别名随名字变化，现为 `gemini 3.8` / `emp_agy` / 括号短名）。
  按用户意愿不动别名与配置，需要时自行改名或写成 `gemini 3.8 (agy)`。

## A79 · 新建会话成员默认全不勾选，右侧提供全选 / 反选

- 现象：新建会话弹窗一打开就把 `availableEmployees` 全部塞进 `employee_ids`，
  每次都要先手动取消不需要的成员。
- 修法：
  - `openNewSessionModal()` 的 `employee_ids` 改为 `[]`（`data` 初始态本来就是空）。
  - 「会话成员名单」标题行右侧加「已选 N/M」计数与「全选」「反选」两个按钮；
    `selectAllNewSessionMembers()` 取全部启用成员，`invertNewSessionMembers()`
    取启用成员的补集，停用成员永远不入列；批量操作只作用于当前弹窗，不碰已有会话。
  - `i18n.js` 补「已选 / 全选 / 反选」英文词条；`?v=` 升到 `20261113r3`。
- 后端配合确认：`_clean_employee_ids([])` 返回 `[]`（不是 None），
  `storage.create_session` 只在 `employee_ids is None` 时才回退默认启用名单 ——
  所以「一位都不选」创建的会话确实是空成员，不会被偷偷全选；之后可在
  「编辑会话配置」里补人。编辑会话弹窗保持原样（回显当前成员）。
- 验证：`node --check` 两个 JS、`git diff --check`；Node `vm` 13 项交互全 PASS
  （默认空、全选、全选后反选、空态反选、部分选中反选补集、停用成员排除、
  创建请求体带 `employee_ids: []`、i18n 词条、Vue 模板编译）；全量 unittest OK；
  线上 `/static/js/{app,i18n}.js?v=20261113r3` 均 200 且含新代码。

## A80 · 气泡不再卡在「正在思考中」；流式块补动点；确认 agy 可流式

- 现象（用户报的三条）：
  1. 并发任务里 gemini-3.8 没有提前输出，怀疑不能流式；
  2. 思考中的气泡里虽然有流式文本，但底部没有「正在思考中」后面那种会跳的点；
  3. 「ai meeting重命名」会话里气泡一直停在「正在思考中」，刷新页面才看到结果。
- 排查结论：
  - **agy 能流式**。实测 `agy --model gemini-3.8-flash-high -p ... --output-format stream-json`
    会按 `step_update.text_delta` 逐段吐字；执行器 `stream_flags()` 确实给它补了
    `--output-format stream-json`，`{tid}:exec:{emp_id}` 与前端拼的 key 完全一致。
    真正的差别是**首个字很晚**：`agy` 的思考 / 工具阶段一个字的 `text_delta` 都没有，
    实测一句话任务 13.1 秒才出第一个字（带读文件的 29 秒任务里，前 24.8 秒全是
    `phase=tool`）。用户看到的「只有 gemini 没提前输出」就是这个空窗。
  - 气泡卡住的真因是**前端轮询提前停摆**：11:49:14 有进程执行了
    `recover_on_startup()`（把 3 个 running 任务写成 cancelled，见 A79 末尾记录），
    而 `hasActiveTasks` 只按任务状态判断「还要不要轮询」，任务一转终态轮询就停，
    之后真实结果落库也没人取 → 只能刷新。`_run_single` 收尾时看到任务已 cancelled，
    还把完成的结果一并标成已取消。
- 修法：
  - `static/js/app.js` `hasActiveTasks`：任务状态之外，再加一条「有 `task_result`
    消息仍是 running / queued 就继续轮询」，把「任务终态、气泡没转」这种组合兜住。
  - `core/tasks.py` `recover_on_startup()`：任务转已取消时，同步把结果消息也转成
    cancelled，内容为空时补一句「已取消（服务重启，进程已终止）」—— 从源头消灭
    「任务终态、消息还在 running」的不一致状态。
  - `static/index.html`：聊天气泡的流式块与卡片简报的流式块底部各加一组
    `thinking-dots`，这一秒没有新字符时也能看到还在推进；`?v=` 升到 `20261113r4`。
  - 数据修复：`0ccb9ad6`（ai meeting重命名）3 条 + `6373f3ce` 1 条结果内容完整、
    却被误标 cancelled 的任务改回 delivered（含结果消息状态），备份在
    `.run/session-backup-20261113/`。
- 验证：全量 `Ran 185 tests — OK`（新增 `test_recover_syncs_result_message_status`）；
  Node `vm` 6 项 `hasActiveTasks` 场景 + 3 段模板编译 PASS（编译器要 stub
  `document.createElement().children[0].getAttribute`，否则属性里的 `&&` 会
  报 `undefined[0]`）；agy 端到端 live 快照 `t=4.5s chars=0` → `t=13.1s chars=24`，
  任务 `delivered`、消息 `delivered`。
- 已知限制：agy 没有 `--include-partial-messages` 之类的更细开关（`agy --help` 只有
  `--output-format text|json|stream-json`），思考阶段注定无字可显示，只能靠
  ⏱ 计时 + 动点维持「在跑」的观感。

## A81 · 项目重命名为 AI Atelier（中文「AI 工坊」，ai-atelier）

- 动机：原名 `AI-Meeting` 存在心智偏差（非讨论/会议工具，而是本机多 AI 任务执行与交付工坊）。
  参考团队提案与建议，正式确定英文名称为 `AI Atelier`（短名/仓库名 `ai-atelier`），中文名称为 `AI 工坊`，保留副标题 `多 AI 协同任务工作台`（`Multi-AI Task Workspace`）。
- 落地改动：
  - 核心协议与注入 Prompt：`prompts/protocol/task-flow.md` 首行改为 `# AI Atelier 协作流程`；
  - 联动测试断言：`tests/test_tasks.py` 第 987 行同步断言 `self.assertIn("AI Atelier 协作流程", prompt)`；
  - Web 界面与元数据：`static/index.html` 标题与左栏 `h1` 改为 `AI Atelier`，更新静态资源版本号为 `?v=20261113r5`；`static/js/app.js` 语言切换 `document.title` 同步支持中英新名称；
  - 启动与运维脚本：`app.py` 启动横幅更新为 `AI Atelier（多 AI 协同任务工作台）`；`run.sh` 脚本注释与启动回显同步更新；
  - 跨站防护动作头：`app.py` 双向兼容 `X-Atelier-Action` 与 `X-Meeting-Action`，前端和 `run.sh` 升级为 `X-Atelier-Action`，`tests/test_service_restart.py` 补充新旧头双向覆盖用例；
  - 文档与许可证：`README.md`、`README_zh.md`、`docs/task-review-protocol.md`、`LICENSE` 版权声明同步更新。
- 验证：
  - 全量 unittest 186 项测试全部通过（含新增兼容性测试）；
  - `python3 -m py_compile app.py core/*.py` 语法检查 PASS；
  - `node --check static/js/app.js && node --check static/js/i18n.js` 语法检查 PASS。

## A82 · 会话背景截断改为「头 + 尾」，最近一条成员产出放宽

- 现象：`build_context()` 用 `text[:600]` 只保留开头，而成员产出（方案 / 报告 / 审核意见）
  的结论几乎都写在末尾 ——「所以最终建议是 X」「改完了，文件在 Y」整段被砍掉。
  下一位成员接手时看不到结论，只能拿开头自己猜，或者把活重做一遍。
  另外最近一条产出也被压到 600 字，用户刚要说「接着改」就断片。
- 修法（`core/tasks.py`）：
  - 新增 `_truncate_middle()`：保留开头与结尾、中间用 `…` 标记；
    `CONTEXT_TAIL_RATIO = 0.62` 让尾部分得多一点（结论在末尾）。
  - `CONTEXT_LATEST_CHARS = 1600`：`build_context()` 里最新的那条成员产出单独放宽上限，
    更早的仍按 `CONTEXT_MSG_CHARS = 600` 截。
  - `summary`（更早进展的兜底摘要）同样改头尾保留，避免摘要自己也丢掉结论。
  - 短文本直接返回原文；`limit < 8` 的极端预算退回只截开头。
- 验证：全量 `Ran 189 tests — OK`（新增 3 项：头尾都保留、短文本不动、
  最近一条放宽而旧的仍按 600 截）。真实会话「ai meeting重命名」三份成果截断后
  尾部都落在结论句上（「已整理至工作目录中的产物文件」「我多做了旧名审计和注册、
  审核风险这两块」「改之前建议先确认有没有"接会议纪要"这类未上线功能」）。
- 未做（用户未要求）：被丢弃的产出仍只保留最后一条的 400 字摘要，没有
  「本会话产出索引 / 文件指针」。多轮之后早先的产出依然会失联，只有文件还在盘上。

## A83 · 排查「gemini 干活时服务重启」并修正被误标的任务

- 现象：`ai会议室重命名`（97117ff4）12:35:41 派给 gemini 的「使用 Atelier，把需要改的
  地方都改一下」在 12:36:46 被标成「服务重启，进行中的环节已取消」，聊天气泡只有
  「已取消（服务重启，进程已终止）」，用户以为活没干完。
- 根因：**是热重载，而且是被这个任务自己触发的**。当前服务以 `FLASK_DEBUG=1` 运行
  （`hot_reload: true`），而这个任务改的正是 `app.py` / `static/*` / `prompts/*` /
  `tests/*`；`app.py` 顶层的 `recover_on_startup()` 会随 reloader 重启子进程再执行一次，
  把 `running` 任务一律判死。文件 mtime 印证：12:38:28–12:39:42 之间
  `task-flow.md` / `test_tasks.py` / `index.html` / `app.js` / `app.py` 连续被写，
  服务在 12:39:42 换了新 PID（21561），本轮改 `core/tasks.py` 后又换到 29626。
- 实际状态：任务**在被中断前已经改完并提交** `64611ff`（14 文件 / +209 −41，
  A81 记录齐全，186 项测试通过），只是结果没回到聊天。CLI 子进程在父进程被杀后
  成了孤儿，继续跑完并提交 —— 这也是「刷新后能看到改动」的原因。
- 处理：`t_33c549e0` 及其结果消息、环节记录一并修正为 `delivered`，按 git 记录补录
  真实改动清单，flow 注明「结果按实际改动补录」；备份
  `.run/session-backup-20261113/97117ff4.before-fix.json`。
- 遗留（**未改机制**）：热重载 / 重启都会终止进行中的任务，这是既定设计
  （`/api/system/restart` 的提示也是「重启后可在卡片里重试」）。但「AI 自己改代码 →
  触发 reload → 杀掉自己的任务」这条链值得防：跑任务期间避免改 `.py`，
  或改用 `./run.sh start`（非 debug、不热重载）配手动 restart。

## A84 · 仓库改名、README 与演示动图重制

- 远端仓库 `BinaryAlan/ai-meeting` 改名为 `BinaryAlan/ai-atelier`（GitHub 侧自动保留旧名跳转），
  本地 `origin` 同步指向新地址，仓库简介从「多 AI 协同圆桌讨论系统」改为
  「多 AI 协同任务工作台（AI Atelier）：本地 CLI 执行，单任务 / 并发分工 / 协助任务三阶段」。
- README 中英两份对齐当前实现：
  - 动图说明改成新脚本的节奏（场景停留 3s、滚动 4s、场景之间 0.8s 白场，全片 64s），
    并说明示例是「一条消息并发分工 + /collab 协助任务」；
  - 上手示例成员名换成设置里真实的 `gemini-3.8 / ds-4.1 / hy4-free`；
  - 新增「成员管理」表格：模型扫描与搜索、「只看免费」、推理程度（高 / 中 / 低不拆成三个模型、
    各 CLI 落到自己的参数）、CLI 模板、读写权限与超时，以及「会话成员里改推理程度只对当前会话生效」；
  - 测试数 105 → 189、验收范围 A1–A59 → A1–A83；目录结构补 `core/cli_registry.py`
    与 `data/scanned_models.json`（git 忽略）。
- 演示动图重制（`docs/demo.gif` / `docs/demo-en.gif`，1200×750 / 8fps / 515 帧 / 64.4s，
  4.9–5.3 MB）：虚构会话改为「协助任务已完成 → 一条消息并发 @ 三位成员（最后一位仍在思考，
  带跳动的思考点）」，九段场景依次是并发分工 → 输入框分工提示 → 输入输出弹窗 →
  协助任务卡片 → 逐段简报滚动 → 完整任务详情 → 审核补全自动交付标签 → 右侧文件预览 →
  收尾；转场由单帧白场改为 6 帧三档淡入 + 7 帧淡出（约 1.6s），滚动每步 2 帧共 12 步，
  明显放慢。录制脚本 `/private/tmp/make_demo_atelier.mjs`（依赖 `/tmp/cdp.mjs`），
  ffmpeg palettegen/paletteuse（160 色、bayer 抖动、diff_mode=rectangle）合成。
- 顺带修掉录制时暴露的排版问题（`static/index.html`）：气泡头部原来是
  `flex` 不换行，`gemini-3.8` 这类长名字会把「名字 + 任务结果标签」挤成两行、
  标签竖着断成「任务结 / 果」。改成可整组换行 + 各项 `whitespace-nowrap`，
  并把耗时 `⏱ 44s` 移到时间后面（右侧只留按钮），长名字也能保持单行；
  真窄时整组换行且按钮右对齐。`?v=` 升到 `20261113r6`。
- 验证：`node --check` 录制脚本、`npm` 无（前端无构建）；全量 `Ran 189 tests — OK`；
  动图逐帧抽查（并发三条结果、分工提示、输入输出弹窗、卡片、详情、自动补全标签、
  Mermaid 预览、片尾）无空白帧与错误字幕。
- 删除三个未跟踪的项目命名建议 md（`ds-4.1-项目命名建议.md`、
  `gemini-3.8-项目重命名提案.md`、`hy4-free-命名建议.md`）。

## A85 · 本地目录改名为 ai-atelier

- 本地工作目录 `~/Code/ai-meeting` → **`~/Code/ai-atelier`**（`mv`，同一卷内原子改名），
  与远端仓库名、README 里的 `git clone … && cd ai-atelier` 保持一致。
- 改名会带走一路硬编码的绝对路径，逐项核对并修掉：
  - `.venv/bin/{flask,pip,pip3,pip3.12,activate,activate.fish,activate.csh}` 的 shebang / 前缀、
    `.venv/pyvenv.cfg` 的 `command =` 全部指向旧目录 → 替换为新路径；旧 `__pycache__/*.pyc`
    里的 `co_filename` 一并清掉（删目录，由解释器重新生成）。改后 `.venv/bin/python`、
    `pip --version`、`flask --version` 均正常，venv 无需重建。
  - `data/sessions/97117ff4.json` 的 `workspace_dir` 存的是旧的绝对路径，改名后该目录不存在，
    右侧工作目录面板会失效 → 先备份到 `.run/rename-backup-<ts>/`，再改为新路径
    （只改这一个结构字段；消息正文里的历史路径属于历史记录，不动）。另两个会话的
    工作目录是 `~/Documents/social skill`，不受影响。
- 全仓库复查：`prompts/protocol/task-flow.md`、`README.md` / `README_zh.md`、`docs/task-review-protocol.md`、
  `static/index.html`（`<title>` 与左栏 `h1`）、`static/js/app.js`（语言切换改 `document.title`）、
  `app.py` 启动横幅、`run.sh` 注释与回显、`LICENSE`、`tests/test_tasks.py` 断言均为 `AI Atelier`，
  无旧名残留；`docs/acceptance.md` 中旧名只出现在描述「改名前后」的历史记录里（A81 / A84 / 本条目）。
- `.git` 侧无需变动：`origin` 上一轮已指向 `https://github.com/BinaryAlan/ai-atelier.git`。
- 验证：新路径下 `Ran 189 tests — OK`；`core.storage.BASE_DIR` 解析为 `/Users/lc/Code/ai-atelier`；
  `py_compile app.py` 通过；临时启动 `app.py` 后 `/api/system/status` 返回 200、
  `cwd` 为 `/Users/lc/Code/ai-atelier`、`session_count` 为 3，随后立即关闭（未用 `run.sh start`，
  避免沙箱进程随会话结束消失造成假故障）。
- 已知保留项（不影响使用）：`.venv` 之外的 `data/sessions/*.json` 消息正文里仍有历史 `file://` 旧路径；
  `.run/service.log` 是历史日志，含旧路径，已被 git 忽略；前端 `localStorage` 的键前缀仍是 `aim.`
  （改前缀会让用户已保存的语言与面板布局重置，故不改）。
