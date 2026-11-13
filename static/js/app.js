const { createApp } = Vue;

// 聊天里「像个文件」的判定：只认带已知扩展名的单个 token。
// 名字起得再像也不认（比如 write_access），否则正文里满屏都会变成可点链接。
const FILE_EXT_ALT = "md|markdown|mdx|txt|json|jsonl|ya?ml|toml|ini|conf|env|xml|csv|tsv|log|py|js|mjs|cjs|jsx|ts|tsx|vue|html?|css|scss|less|sh|bash|zsh|sql|java|kt|go|rs|rb|php|swift|cc|cpp|hpp|png|jpe?g|gif|webp|bmp|svg|ico|pdf|docx?|xlsx?|pptx?";
const FILE_EXT_RE = new RegExp("\\.(?:" + FILE_EXT_ALT + ")$", "i");
// 「Node.js / Next.js」这类框架名不是工作目录里的文件，别包成可点
const FILE_TOKEN_DENY = /^(node|next|nuxt|vue|react|angular|svelte|ember|express|nest|three|d3|chart|moment|alpine|jquery|backbone|knex|prisma|vite|webpack)\.js$/i;

// 预览面板里允许「直接编辑并保存」的后缀：与后端 EDITABLE_EXTS 保持一致。
// 二进制文件（docx / 图片 / 压缩包）绝不能当文本写回去。
const EDITABLE_EXTS = new Set([
  "md", "markdown", "mdx", "txt", "text", "log",
  "json", "jsonl", "yml", "yaml", "toml", "ini", "conf", "cfg",
  "csv", "tsv", "html", "htm", "css", "scss", "less",
  "js", "mjs", "cjs", "ts", "tsx", "jsx", "vue", "svelte",
  "py", "sh", "bash", "zsh", "sql", "xml", "java", "go", "rs", "rb", "php",
]);

// 实时刷新轮询间隔：AI 正在写文件时，面板里能跟着变；1.2s 足够快又不至于压满磁盘
const PREVIEW_POLL_MS = 1200;

createApp({
  data() {
    return {
      // 界面语言：zh | en。存在 settings.json（跨浏览器一致），本地再存一份用于首屏不闪。
      lang: (window.localStorage.getItem("aim.lang") === "en") ? "en" : "zh",
      sessionTab: "all", // 'all' | 'fav'
      sessions: [],
      currentSessionId: null,
      currentSession: {
        id: "",
        title: "",
        messages: [],
        employee_ids: [],
        workspace_dir: "",
        tasks: {},
        task_queues: {},
        active_task_id: null
      },
      settings: {
        ui_language: "zh",
        employees: []
      },
      inputContent: "",
      // 输入候选列表：打 / 弹命令、打 @ 弹成员。
      // { kind: 'command'|'member', query, items, active, start, end }，start/end 是被替换的文本范围
      suggest: null,
      // /collab 的服务端校验结果：{ content, ok, error }；content 用来丢弃过期响应
      collabCheck: null,
      collabCheckTimer: null,
      // 多 @ 的并行分工预览：{ content, fanout, names, error }，同样按 content 防过期
      fanoutCheck: null,
      fanoutCheckTimer: null,
      // 引用回复：{ id, sender, sender_name, content }，发送时把 id 交给后端定向
      replyTarget: null,
      // 长消息展开状态（按消息 id 记，轮询刷新不会把用户展开的又收回去）
      expandedMsgs: {},
      // 哪些消息真的被限高截断了（渲染后实测，决定是否给「展开全文」按钮）
      overflowMsgs: {},
      workspaceItems: [],
      workspaceCurrentDir: "",           // 当前正列出的目录（默认是会话根目录）
      workspacePathStack: [],            // 面包屑栈：[{ name, path }]，末项即当前目录，支持回跳上级
      selectedWorkspaceFile: null,
      selectedFileContent: "",
      // 文件预览弹窗：右侧面板只列目录，点文件在这里看，md 走渲染、图片走 img、其余按等宽文本
      showFilePreview: false,
      // { name, path, size, mtime, kind, content, url, error, updatedAt, external }
      filePreview: null,
      filePreviewLoading: false,
      previewUpdatedAt: 0,       // 最近一次因外部改写而重新读取的时刻（毫秒）
      // 预览面板的「就地编辑」：draft 是草稿，dirty 用于确认是否放弃
      previewEditing: false,
      previewDraft: "",
      previewDirty: false,
      previewSaving: false,
      previewPollTimer: null,

      showMemberDropdown: false,

      // 文件夹选择器弹窗 (小窗口选择本地目录)
      showFolderPickerModal: false,
      folderPickerPath: "",
      folderPickerParent: null,
      folderPickerDirs: [],
      folderPickerTargetForm: "newSessionForm", // 'newSessionForm' | 'editingSessionForm'

      // 新建会话弹窗
      showNewSessionModal: false,
      newSessionForm: {
        title: "",
        workspace_dir: "",
        employee_ids: []
      },

      // 编辑当前会话弹窗（改名、改目录、增减成员）
      showEditSessionModal: false,
      editingSessionForm: {
        id: "",
        title: "",
        workspace_dir: "",
        employee_ids: []
      },

      // 设置中心：成员管理 / 界面语言 / 服务
      showSettings: false,
      // 设置默认落在「成员管理」：日常最常改的就是这个，界面语言基本一次设完
      settingsTab: "members",
      // 成员列表是日常最常改的一项，放在菜单第一位
      settingsTabs: [
        { key: "members", icon: "👥", label: "成员管理" },
        { key: "general", icon: "🎛️", label: "界面语言" },
        { key: "service", icon: "⏻", label: "服务进程" }
      ],

      // 成员配置弹窗
      showEmpEditModal: false,
      isEditingEmployee: false,
      editingEmpIdx: -1,
      selectedModelId: "",
      scannedModels: [],
      // 模型选择器：默认收起，点击触发器才展开（浮层 Teleport 到 body，
      // 避免被成员弹窗的 overflow-y-auto 裁掉）
      modelSearch: "",
      onlyFreeModels: false,
      modelDropdownOpen: false,
      modelDropdownStyle: {},
      // 会话成员列表里当前展开推理档位的成员 id（同一时刻只展开一个）
      sessionReasoningEmpId: "",
      editingEmp: {
        id: "",
        name: "",
        avatar: "🤖",
        avatar_color: "#3b82f6",
        type: "cli",
        cli_cmd: "",
        model_key: "",
        reasoning: "",
        system_prompt: "",
        enabled: true,
        timeout: 0,
        // 新建成员默认「读 + 写」都开：只勾读不足以做实际交付，
        // 两个都不勾则连产物都读不到（只会回一句「我先读取一下」）。
        write_access: true,
        read_access: true
      },
      presetAvatars: ["🤖", "🧠", "⚡", "🦉", "🛡️", "🚀", "💻", "🔬", "🎯", "🎨", "👑", "🕵️", "🔧", "💡", "⚖️", "📊"],

      // 协助任务卡片弹窗（两部分 + 各环节取消 / 重试）
      showCardModal: false,
      cardModalMsg: null,
      nowTick: Date.now(),   // 卡片外壳的「实时耗时」由每秒心跳驱动
      tickTimer: null,
      // 聊天卡片里某段简报是否展开全文（key → true）。不存进后端：
      // 这是纯阅读状态，刷新页面回到「三行摘要」是符合预期的。
      briefOpen: {},
      cardClosed: {},
      // 已删除（或正在删除）的消息 id：本地先屏蔽，别等轮询把旧数据带回来闪一下
      deletedMsgIds: {},
      // 消息审查详情弹窗
      showInspectModal: false,
      inspectTab: "prompt",
      // 原始回复默认按 markdown 渲染；模型偶发输出半截表格/代码块时，
      // 渲染结果会难看懂，所以留一个「原文」开关让人切回纯文本对照。
      inspectRaw: false,
      inspectingMsg: null,
      // Prompt 分段折叠状态（key → 是否展开）。只记用户手动点过的段，
      // 没点过的走 promptSections 里的 defaultOpen，所以每次打开弹窗都是默认视图。
      promptOpen: {},
      // 点引用块跳转时，被引用那条的短暂高亮 id
      flashMsgId: "",
      // 原话已不在列表里时的快照预览弹窗
      quotePreview: null,
      // 自动跟随：只有用户主动往上翻时才停跟随，否则无论内容长多快都贴底。
      // 不用「距底部多少像素」当判据——一次到达几百字就足以把距离撑过阈值，
      // 于是跟随永久失效，用户以为「跳不到底」。
      stickToBottom: true,
      showJumpToLatest: false,

      isComposing: false, // 正在使用输入法组词或英文回车确认
      defaultWorkspaceDir: "",
      switchToken: 0,     // 会话切换请求序号，用于丢弃过期的在途响应
      pollInterval: null,
      // 正在生成的文本（服务端进程内暂存，不落库）：key = `{task_id}:{stage}:{member_id}`
      liveStreams: {},
      // 刚结束、正式结果还没轮询回来的一小段时间里，先用上一份文本兜着，
      // 免得卡片从「正在写」闪回「正在思考中」
      liveLast: {},
      liveInterval: null,

      // 服务进程控制（状态 / 重启 / 停止）
      systemStatus: null,
      serviceBusy: false,
      serviceDown: false,
      toastText: "",      // 顶部轻提示

      // 布局：左栏可收起、左右两栏宽度可拖拽
      sidebarWidth: 256,
      rightWidth: 440,
      sidebarCollapsed: false,
      dragTarget: null,   // 'sidebar' | 'right'
      dragStartX: 0,
      dragStartW: 0
    };
  },
  computed: {
    // 本地模型列表：空格分隔的多关键词（如 "opencode 3.8"）全部命中才算匹配，
    // 关键词可以是模型名、model_id、key 或 CLI 名。
    filteredScannedModels() {
      const terms = String(this.modelSearch || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
      return (this.scannedModels || []).filter(m => {
        if (this.onlyFreeModels && !m.is_free) return false;
        if (!terms.length) return true;
        const hay = `${m.cli || ""} ${m.display_name || ""} ${m.model_id || ""} ${m.key || ""}`.toLowerCase();
        return terms.every(t => hay.includes(t));
      });
    },
    selectedModelObj() {
      return (this.scannedModels || []).find(x => this.modelKey(x) === this.selectedModelId) || null;
    },
    // 选中模型后才出现的推理档位按钮
    selectedReasoningLevels() {
      return this.selectedModelObj ? (this.selectedModelObj.reasoning_levels || []) : [];
    },
    selectedModelLabel() {
      const m = this.selectedModelObj;
      if (!m) return "";
      const lv = (m.reasoning_levels || []).find(x => x.id === this.editingEmp.reasoning);
      const suffix = lv ? ` · ${this.t("推理:")} ${this.t(lv.label)}` : "";
      return `[${m.cli}] ${m.display_name}${suffix}`;
    },

    // 完整输入按【区块标题】切段：大段材料默认折叠，只留标题和体量提示
    promptSections() {
      const raw = String((this.inspectingMsg && this.inspectingMsg.sent_prompt) || "");
      if (!raw.trim()) return [];

      const COLLAPSED_BY_DEFAULT = ["上下文", "正文", "回复的这条消息"];
      const headerRe = /^[ \t]*【([^】\n]{1,60})】[ \t]*$/;
      const buckets = [];
      let cur = { title: "", body: [] };
      for (const line of raw.split("\n")) {
        const m = headerRe.exec(line);
        if (m) {
          buckets.push(cur);
          cur = { title: m[1], body: [] };
        } else {
          cur.body.push(line);
        }
      }
      buckets.push(cur);

      return buckets
        .map((b, i) => {
          const body = b.body.join("\n").replace(/^\n+/, "").replace(/\n+$/, "");
          return {
            key: "sec" + i,
            title: b.title,
            body,
            chars: body.length,
            lineCount: body ? body.split("\n").length : 0,
            defaultOpen: !COLLAPSED_BY_DEFAULT.some(k => b.title.includes(k)),
          };
        })
        .filter(s => s.title || s.body);
    },

    // 查看某环节输入输出、但这一环节没有 Prompt 时该说什么
    inspectEmptyHint() {
      const st = (this.inspectingMsg && this.inspectingMsg.stage_status) || "";
      if (["waiting", "queued"].includes(st)) {
        return "该环节还没开始，执行后才能看到发送给模型的 Prompt。";
      }
      if (["running", "in_progress"].includes(st)) {
        return "该环节正在执行，Prompt 已随这次执行留存，稍后刷新即可查看。";
      }
      return "该条记录生成于 Prompt 持久化功能上线前，未留存独立 Prompt。";
    },

    // 输入输出弹窗顶部的解释条：把 CLI 的英文报错翻成人话。
    // 起因：端口冲突修复上线后，用户点开一条旧记录看到满屏 EADDRINUSE，
    // 会以为「修了还在报」。原文一个字不改（可复制、可排查），只在上面加一句说明。
    inspectNote() {
      const msg = this.inspectingMsg || {};
      const text = String(msg.raw_response || msg.content || "");
      if (/EADDRINUSE|address already in use/i.test(text)) {
        return this.t(
          "这是命令行的端口冲突：CLI 启动时要监听的本机端口被别的程序占了，进程随即退出，所以这一环节没有产出。当前版本会在干净环境里启动 CLI，撞端口还会自动重试；若这条是较早的记录，点该环节的「重试」跑一次即可确认。"
        );
      }
      if (/Authentication required|not logged in|please log ?in/i.test(text)) {
        return this.t(
          "这是 CLI 自己的登录态失效，和软件无关：在终端里重新登录该 CLI，再点该环节的「重试」即可。"
        );
      }
      return "";
    },

    // 弹窗默认视图的正文：登记块已摘掉；整段都是 JSON 时退回到 summary 字段
    inspectBody() {
      const split = this.splitRegistry(
        (this.inspectingMsg && (this.inspectingMsg.raw_response || this.inspectingMsg.content)) || ""
      );
      if (split.body) return split.body;
      if (!split.registry) return "";
      try {
        const obj = JSON.parse(split.registry);
        const summary = String(obj.summary || "").trim();
        const changes = String(obj.changes || obj.opinion_handling || "").trim();
        if (summary && changes) return `${summary}\n\n**处理说明**\n\n${changes}`;
        return summary || changes || "";
      } catch (e) {
        return "";
      }
    },
    inspectRegistry() {
      return this.splitRegistry(
        (this.inspectingMsg && (this.inspectingMsg.raw_response || this.inspectingMsg.content)) || ""
      ).registry;
    },

    // 本会话已选成员列表（未勾选的不在此列表中）
    activeSessionEmployees() {
      if (!this.currentSession || !Array.isArray(this.currentSession.employee_ids)) return [];
      const empMap = new Map((this.settings.employees || []).filter(e => e.enabled).map(e => [e.id, e]));
      return this.currentSession.employee_ids
        .map(id => empMap.get(id))
        .filter(Boolean);
    },
    favoriteSessions() {
      return this.sessions.filter(s => s.is_favorite);
    },
    displayedSessions() {
      if (this.sessionTab === "fav") {
        return this.favoriteSessions;
      }
      return this.sessions;
    },
    availableEmployees() {
      return (this.settings.employees || []).filter(e => e.enabled);
    },
    // 输入框打 / 时弹出的命令清单。这里只列真正实现了的指令，
    // 不做「以后可能会有」的占位项——点了没反应比没有更糟。
    commandCandidates() {
      return [
        {
          id: "cmd/collab",
          kind: "command",
          label: "/collab",
          desc: this.t("发起协助任务：@主创 @协助成员 任务内容"),
          insert: "/collab ",
          opensMembers: true,   // 选完立刻接着让人选主创
        },
      ];
    },
    inputPlaceholder() {
      return this.t("输入任务：@ 成员（@ 多位则各自独立作答），或 /collab @主创 @协助成员… (Enter 发送)");
    },
    // 协助任务各环节的执行记录不进聊天流（但能在卡片弹窗里查完整输入 / 原始输出）
    visibleMessages() {
      return (this.currentSession?.messages || []).filter(
        m => m && m.in_chat !== false && !(m.id && this.deletedMsgIds[m.id])
      );
    },
    hasActiveTasks() {
      const tasks = this.currentSession?.tasks || {};
      // 任务终态之后仍可能有环节在跑（重试审核 / 重新生成交付）：这些状态下也要继续轮询，
      // 否则点了「重试」关掉弹窗，卡片就停在「进行中」不再更新
      const inflight = (t) => {
        const a = t.part_a || {};
        if (a.status === "in_progress" || a.final_status === "in_progress") return true;
        return (t.part_b || []).some(i => i && i.status === "in_progress");
      };
      return Object.values(tasks).some(t => t && (
        t.status === "running" || t.status === "queued" || inflight(t)
      ));
    },
    // 输入时的 /collab 校验：交给服务端 /api/collab/validate，前后端用同一套解析。
    // 本地再写一套「谁被 @ 了」的判断必然会和 Python 那边越走越偏（现在已支持
    // @//agy/ 这种路径写法、大小写、提及散落在句子里），所以这里只负责显示结果。
    collabHint() {
      const text = (this.inputContent || "").trim();
      if (!/^\/collab(\s|$)/i.test(text)) return null;
      const res = this.collabCheck;
      // 还没校验完（或期间又改了字）就先不显示，避免闪一下错的提示
      if (!res || res.content !== text) return null;
      if (res.ok) return { ok: true, text: this.t("格式正确：将发起一轮协助任务") };
      return { ok: false, text: this.t(res.error || "格式不正确") };
    },
    // 普通消息 @ 了多位：服务端会并行分工（每人一条单任务、各出一份）。
    // 「谁被 @ 到了」同样交给服务端算：别名、`@//agy/` 这类写法本地重写一遍必然跑偏。
    fanoutHint() {
      const text = (this.inputContent || "").trim();
      if (!text || this.replyTarget) return null;
      if (/^\/collab(\s|$)/i.test(text)) return null;
      const res = this.fanoutCheck;
      if (!res || res.content !== text) return null;
      if (res.error) return { ok: false, text: this.t(res.error) };
      if (!res.fanout || !res.names.length) return null;
      return {
        ok: true,
        text: this.t("将分别发给 {names}：各自独立作答，互不参考", { names: res.names.join("、") }),
      };
    },
    // 卡片弹窗里的「历次执行记录」：取消 / 重试产生的旧记录也保留，可按记录查看完整输入与原始输出
    cardRecords() {
      const tid = this.showCardModal ? this._taskId(this.cardModalMsg) : "";
      if (!tid) return [];
      const seen = {};
      return (this.currentSession?.messages || [])
        .filter(m => m && m.task_id === tid && m.kind === "task_record")
        .map(m => {
          const k = `${m.stage}:${m.member_id || ""}`;
          seen[k] = (seen[k] || 0) + 1;
          return { ...m, _label: this.recordLabel(m, seen[k]) };
        });
    },

    // 实时流水：早期记录里写的是 emp_xxx 这种内部 id，显示时换成成员名
    cardFlow() {
      return (this.cardModalMsg?.card?.flow || []).slice(-8).map(f => ({
        ...f,
        text: this._localizeMemberIds(f.text),
      }));
    },

    // 卡片顶部的三步进度：执行 → 审核 → 交付
    cardSteps() {
      return this.cardStepsOf(this.cardModalMsg && this.cardModalMsg.card);
    },

    // 审核环节的整体进度，例如「1/2 已完成」
    reviewProgress() {
      return this.reviewProgressOf(this.cardModalMsg && this.cardModalMsg.card);
    },

  },
  mounted() {
    this.restoreLayout();
    this.init();
    this.pollInterval = setInterval(this.pollActiveSession, 1500);
    // 正在生成的文本单独轮询：会话接口 1.5s 一次且整份重渲染，做不了「逐句增长」
    this.liveInterval = setInterval(this.pollLiveStreams, 500);
    // 卡片外壳的「实时耗时」流水：每秒只更新一个时间戳，不重拉接口
    this.tickTimer = setInterval(() => { this.nowTick = Date.now(); }, 1000);
    // 全局点击关闭成员下拉框
    document.addEventListener("click", this.handleGlobalClick);
    // 聊天气泡里的文件名：v-html 里挂不了 Vue 事件，用捕获阶段的委托来接管，
    // 免得点了文件名还顺带触发父级的「展开全文 / 收起」
    document.addEventListener("click", this.onDocFileClick, true);
    // 拖拽调宽
    window.addEventListener("mousemove", this.onDragMove);
    window.addEventListener("mouseup", this.onDragEnd);
    window.addEventListener("blur", this.onDragEnd);
    window.addEventListener("resize", this.onWindowResize);
    window.addEventListener("keydown", this.onGlobalKeydown);
    // 预览面板里的文件可能正被 AI 改写：低频轮询 mtime，变了才重读正文
    this.previewPollTimer = setInterval(this.pollPreviewFile, PREVIEW_POLL_MS);
    // 拖拽右栏 / 缩放窗口都会让气泡重新排版：高度变了要重新量「有没有被截断」，
    // 否则一条长回答会被限高切掉、却因为没有「展开全文」按钮而看不到后半段
    if (typeof ResizeObserver === "function" && this.$refs.chatContainer) {
      this.chatResizeObserver = new ResizeObserver(() => this.scheduleMsgOverflowMeasure());
      this.chatResizeObserver.observe(this.$refs.chatContainer);
    }
    // markdown 里的 mermaid 图要在 v-html 落地后再换成 SVG。
    // 聊天流、预览弹窗、任务详情都会插入新节点，统一用观察器兜住，不用每处都插一次调用。
    this.mermaidObserver = new MutationObserver(this.scheduleMermaid);
    this.mermaidObserver.observe(document.body, { childList: true, subtree: true });
    this.scheduleMermaid();
  },
  beforeUnmount() {
    if (this.pollInterval) clearInterval(this.pollInterval);
    if (this.liveInterval) clearInterval(this.liveInterval);
    if (this.tickTimer) clearInterval(this.tickTimer);
    if (this.previewPollTimer) clearInterval(this.previewPollTimer);
    if (this._overflowTimer) clearTimeout(this._overflowTimer);
    if (this.chatResizeObserver) this.chatResizeObserver.disconnect();
    if (this.mermaidObserver) this.mermaidObserver.disconnect();
    if (this._mermaidTimer) clearTimeout(this._mermaidTimer);
    document.removeEventListener("click", this.handleGlobalClick);
    document.removeEventListener("click", this.onDocFileClick, true);
    window.removeEventListener("mousemove", this.onDragMove);
    window.removeEventListener("mouseup", this.onDragEnd);
    window.removeEventListener("blur", this.onDragEnd);
    window.removeEventListener("resize", this.onWindowResize);
    window.removeEventListener("keydown", this.onGlobalKeydown);
  },
  methods: {
    // Esc 关掉最上面那层弹窗（文件预览 → 输入输出 → 任务详情），输入框里按 Esc 不受影响
    onGlobalKeydown(e) {
      if (e.key !== "Escape") return;
      // 正在编辑文件时按 Esc 只退出编辑，别把整个预览面板关掉（草稿还在，可以再来）
      if (this.previewEditing) { this.cancelPreviewEdit(); return; }
      if (this.filePreview) { this.closeFilePreview(); return; }
      if (this.showInspectModal) { this.showInspectModal = false; return; }
      if (this.showCardModal) { this.showCardModal = false; }
    },
    // 折叠卡片也要显示审核进度，所以按任意 card 计算
    reviewProgressOf(card) {
      const reviewers = (card && card.part_b) || [];
      if (!reviewers.length) return "";
      const finished = reviewers.filter(r =>
        ["done", "failed", "cancelled", "timeout", "no_opinion"].includes(r.status)
      ).length;
      return this.t("{done}/{total} 已完成", { done: finished, total: reviewers.length });
    },
    // 流水文案里残留的内部成员 id 换成成员名（折叠卡片只显示最后一行，也要换）
    _localizeMemberIds(raw) {
      let text = String(raw || "");
      const nameOf = {};
      (this.settings.employees || []).forEach(e => { nameOf[e.id] = e.name || e.id; });
      // 长 id 先替换，避免 emp_agy 被 emp_agy_2 这种更长的 id 抢先切掉一半
      Object.keys(nameOf).sort((a, b) => b.length - a.length).forEach(id => {
        if (text.includes(id)) text = text.split(id).join(nameOf[id]);
      });
      return text;
    },

    latestFlowText(card) {
      const flow = (card && card.flow) || [];
      const last = flow[flow.length - 1];
      return last ? this._localizeMemberIds(last.text) : this.t("（暂无）");
    },

    // 聊天卡片的主体：主创执行 / 每位协助审核 / 主创交付，各一段简报。
    // 产物清单、实时流水、完整 Prompt 与原始输出都留在「完整任务详情」里，
    // 聊天流里只回答问题「这一轮谁做了什么、结论是什么」。
    cardBriefRows(card) {
      if (!card) return [];
      const a = card.part_a || {};
      const ownerName = card.owner_name || (card.owner && card.owner.name) || "";
      const rows = [{
        key: "exec",
        icon: "🎯",
        name: ownerName,
        memberId: (card.owner && card.owner.id) || "",
        role: this.t("主创"),
        stage: this.t("执行"),
        status: a.status || "waiting",
        elapsed: Number(a.elapsed) || 0,
        started_at: a.started_at || null,
        finished_at: a.finished_at || null,
        brief: a.brief || "",
        inspect: {
          sent_prompt: a.sent_prompt || "",
          raw_response: a.raw_output || "",
          content: a.brief || "",
          stage_status: a.status || "",
          elapsed: Number(a.elapsed) || 0,
          started_at: a.started_at || null,
          finished_at: a.finished_at || null,
        },
      }];
      (card.part_b || []).forEach(item => {
        const st = item.status || "waiting";
        rows.push({
          key: "review:" + (item.member_id || ""),
          icon: "🤝",
          name: item.name || item.member_id || "",
          memberId: item.member_id || "",
          role: this.t("协助审核"),
          stage: this.t("审核"),
          status: st,
          // 审核规程的「达标即过」：verdict=pass 时卡片挂一个「通过」标签，
          // 让「没有改动要求」和「提了意见」一眼可分
          reviewVerdict: item.review_verdict || "",
          elapsed: Number(item.elapsed) || 0,
          started_at: item.started_at || null,
          finished_at: item.finished_at || null,
          brief: item.summary || "",
          inspect: {
            sent_prompt: item.sent_prompt || "",
            raw_response: item.raw_output || "",
            content: item.summary || "",
            stage_status: st,
            elapsed: Number(item.elapsed) || 0,
            started_at: item.started_at || null,
            finished_at: item.finished_at || null,
          },
        });
      });
      const final = a.final || {};
      rows.push({
        key: "deliver",
        icon: "📦",
        name: ownerName,
        memberId: (card.owner && card.owner.id) || "",
        role: this.t("主创"),
        stage: this.t("交付"),
        status: a.final_status || "waiting",
        elapsed: Number(a.final_elapsed) || 0,
        started_at: a.final_started_at || null,
        finished_at: a.final_finished_at || null,
        brief: final.summary || "",
        inspect: {
          sent_prompt: a.final_sent_prompt || "",
          raw_response: a.final_raw_output || "",
          content: final.summary || "",
          stage_status: a.final_status || "",
          elapsed: Number(a.final_elapsed) || 0,
          started_at: a.final_started_at || null,
          finished_at: a.final_finished_at || null,
        },
      });
      return rows;
    },

    // 角色 + 环节：「协助审核」本身就是环节名，再拼一次会变成「协助审核 · 审核」
    briefRoleText(row) {
      const role = (row && row.role) || "";
      const stage = (row && row.stage) || "";
      if (!stage || (role && role.includes(stage))) return role || stage;
      return `${role} · ${stage}`;
    },

    // 简报正文：没出结果时给出「为什么没有」，比空白或「暂无」有用
    briefText(row) {
      const raw = String((row && row.brief) || "").trim();
      const st = (row && row.status) || "";
      // 取消与报错时，后端写进 summary 的是给日志看的内部提示（【任务已被用户取消】、
      // 【CLI 执行报错: Error: listen EADDRINUSE …】）。原样铺在卡片上既占地方又吓人，
      // 卡片上说清结论，报错原文留给「输入输出」弹窗。
      if (st === "cancelled") return this.t("已取消，未给出意见");
      if (["failed", "timeout"].includes(st) && (!raw || /^【/.test(raw))) {
        return st === "failed"
          ? this.t("执行失败，点击查看报错详情")
          : this.t("已超时，点击查看已产出的内容");
      }
      if (raw) return raw;
      if (["running", "in_progress"].includes(st)) return this.t("正在执行，简报生成后会显示在这里");
      if (["waiting", "queued"].includes(st)) return this.t("排队等待中");
      if (st === "failed") return this.t("执行失败，点击查看报错详情");
      if (st === "timeout") return this.t("已超时，点击查看已产出的内容");
      if (st === "no_opinion") return this.t("未提出意见");
      return this.t("（暂无）");
    },

    briefKey(card, row) {
      return `${(card && card.task_id) || ""}:${(row && row.key) || ""}`;
    },
    // 整张卡片的收起：点头部横栏切换。和「每段各自的展开」是两层，
    // 卡片收起来时三段内容一起藏掉，头部与底部流水还留着（详情入口在那里）
    isCardClosed(msg) {
      const key = (msg && msg.card && msg.card.task_id) || (msg && msg.id) || "";
      return !!this.cardClosed[key];
    },
    toggleCardClosed(msg) {
      const key = (msg && msg.card && msg.card.task_id) || (msg && msg.id) || "";
      if (!key) return;
      if (this.cardClosed[key]) delete this.cardClosed[key];
      else this.cardClosed[key] = true;
      this.cardClosed = { ...this.cardClosed };
    },
    isBriefOpen(card, row) {
      return !!this.briefOpen[this.briefKey(card, row)];
    },
    toggleBrief(card, row) {
      const k = this.briefKey(card, row);
      if (this.briefOpen[k]) delete this.briefOpen[k];
      else this.briefOpen[k] = true;
      // Vue 3 的 reactive 对 delete 有响应，但要触发视图更新还是换个引用更稳
      this.briefOpen = { ...this.briefOpen };
    },
    // 超过 3 行才给「展开全文」：短简报下面挂个没用的按钮更碍眼
    briefLong(row) {
      const text = this.briefText(row);
      return text.length > 96 || text.split("\n").length > 3;
    },
    // 这一段的秒数：运行中按心跳实时算（卡片计时才会动），其余用落库的 elapsed
    briefElapsedSec(row) {
      const st = String((row && row.status) || "");
      const start = Number((row && row.started_at) || 0);
      if (["running", "in_progress"].includes(st) && start) {
        return Math.max(0, Math.floor(this.nowTick / 1000) - start);
      }
      return Number((row && row.elapsed) || 0);
    },
    fmtSec(sec) {
      const s = Math.max(0, Math.round(Number(sec) || 0));
      if (!s) return "";
      return s >= 60 ? `${Math.floor(s / 60)}m${Math.round(s % 60)}s` : `${s}s`;
    },
    briefElapsed(row) {
      return this.fmtSec(this.briefElapsedSec(row));
    },
    // 卡片每段标题后的时间：几点开始 + 跑了多久。
    // 老任务没有 started_at（这个字段是后加的），退回只显示耗时，不显示 "Invalid Date"
    briefWhen(row) {
      const ts = Number((row && row.started_at) || 0);
      const text = this.fmtSec(this.briefElapsedSec(row));
      if (!ts) return text;
      const clock = this.formatFlowTime(ts);
      return text ? `${clock} · ${text}` : clock;
    },
    briefWhenTitle(row) {
      const s = Number((row && row.started_at) || 0);
      const f = Number((row && row.finished_at) || 0);
      if (!s) return "";
      return f
        ? this.t("开始 {a} · 返回 {b}", { a: this.formatFlowTime(s), b: this.formatFlowTime(f) })
        : this.t("开始 {a}", { a: this.formatFlowTime(s) });
    },
    // 「输入输出」弹窗顶部那一行：开始 / 返回 / 用时
    inspectTimeText() {
      const m = this.inspectingMsg || {};
      const s = Number(m.started_at || 0);
      // 老记录 / 普通消息没有独立的 finished_at：消息落库时刻就是结果返回时刻，
      // 拿 created_at 兜底，至少能回答「这条是什么时候出来的」
      let f = Number(m.finished_at || 0);
      if (!f && !s) f = Number(m.created_at || 0);
      const sec = Number(m.elapsed || m.elapsed_time || 0);
      if (!s && !f && !sec) return "";
      const parts = [];
      if (s) parts.push(this.t("开始 {a}", { a: this.formatFlowTime(s) }));
      if (f) parts.push(this.t("返回 {b}", { b: this.formatFlowTime(f) }));
      else if (["running", "in_progress"].includes(m.stage_status || "")) parts.push(this.t("执行中"));
      if (sec) parts.push(this.t("用时 {d}", { d: this.briefElapsed({ elapsed: sec }) }));
      return parts.join("  ·  ");
    },
    briefBusy(row) {
      return ["running", "in_progress"].includes((row && row.status) || "");
    },

    // 每段左侧的状态色条：谁在跑、谁失败、谁通过，不读文字也能分出来。
    // 用独立色条而不是给容器加 border-l，避免和容器的统一边框抢颜色。
    rowBarClass(row) {
      const st = String((row && row.status) || "");
      if (["running", "in_progress"].includes(st)) return "bg-blue-400";
      if (["failed", "timeout"].includes(st)) return "bg-rose-400";
      if (st === "cancelled") return "bg-gray-300";
      if (st === "no_opinion") return "bg-amber-400";
      if (["done", "delivered"].includes(st)) return "bg-emerald-400";
      return "bg-slate-300";
    },

    // 段头的头像：优先用成员的 emoji 头像，查不到就退回环节图标
    rowAvatar(row) {
      const id = (row && row.memberId) || "";
      const emp = id ? (this.settings.employees || []).find(e => e.id === id) : null;
      return (emp && emp.avatar) || (row && row.icon) || "🤖";
    },
    // 还没有正文、但这一轮正在跑（或排队）：显示带动画省略号的占位，而不是渲染空 markdown
    briefLivePlaceholder(row) {
      const has = String((row && row.brief) || "").trim();
      return !has && ["running", "in_progress", "waiting", "queued"].includes((row && row.status) || "");
    },

    // ---------------- 实时输出（流式） ----------------
    // 服务端的 key 是 `{task_id}:{stage}:{member_id}`（见 core/streaming.py），
    // 卡片上每一段的 row.key 要映射回这个三元组才能对上号
    liveKeyOf(card, row) {
      const tid = (card && card.task_id) || "";
      const key = (row && row.key) || "";
      if (!tid || !key) return "";
      const stage = key === "deliver" ? "deliver" : (key.indexOf("review:") === 0 ? "review" : "exec");
      return `${tid}:${stage}:${row.memberId || ""}`;
    },
    liveTail(text, limit) {
      const body = String(text || "");
      const max = limit || 900;
      if (body.length <= max) return body;
      // 只显示尾部：长回答要看到「现在写到哪」，而不是最早那几段
      return "…" + body.slice(body.length - max);
    },
    // CLI 的工具名大多是英文 / 下划线（read_file、run_command），直接显示成
    // 「● tool」这种谁也不知道在干什么的标签。这里翻成人话，认不出来的保留原名，
    // 但一定带上「正在调用工具」的上下文。
    toolLabel(activity) {
      const raw = String(activity || "").trim();
      if (!raw) return "";
      const parts = raw.split(" · ");
      const name = parts[0] || "";
      const detail = parts.slice(1).join(" · ");
      const map = {
        read_file: "读取文件", read: "读取文件", readfile: "读取文件", read_multiple_files: "读取文件",
        write_file: "写入文件", write: "写入文件", writefile: "写入文件",
        edit_file: "修改文件", edit: "修改文件", str_replace: "修改文件", apply_patch: "修改文件",
        list_dir: "浏览目录", ls: "浏览目录", list_directory: "浏览目录",
        glob: "查找文件", grep: "搜索内容", search: "搜索内容", codebase_search: "搜索内容",
        run_command: "执行命令", bash: "执行命令", shell: "执行命令", terminal: "执行命令",
        web_search: "联网搜索", webfetch: "抓取网页", web_fetch: "抓取网页", fetch: "抓取网页",
        todo_write: "整理计划", task: "派发子任务",
      };
      const raw0 = map[String(name).toLowerCase()] || name;
      const zh = map[String(name).toLowerCase()] ? this.t(raw0) : raw0;
      return detail ? `${zh} · ${detail}` : zh;
    },
    livePhaseText(stream) {
      const phase = (stream && stream.phase) || "";
      if (phase === "tool") {
        // 工具阶段直接显示在干什么（读取文件 · docs/plan.md / 执行命令 · npm test），
        // 比一句笼统的「正在思考」更能解释这段时间
        const act = String((stream && stream.activity) || "").trim();
        // 后端在拿不到具体工具名时会退化成步骤类型（tool / step / observation），
        // 这种词对用户没有信息量，统一说「正在调用工具」
        if (!act || /^(tool|tool_use|tool_call|step|observation|action|function|unknown)$/i.test(act)) {
          return this.t("正在调用工具");
        }
        return this.toolLabel(act);
      }
      if (phase === "thinking") return this.t("正在思考");
      if (phase === "writing") return this.t("正在输出");
      return this.t("正在输出");
    },
    // 卡片某一段的实时文本：优先取当前流；刚结束那一瞬用上一份兜住，避免闪回占位。
    // 还没有正文、但模型正在读文件 / 跑命令时也算「有进展」：把工具活动放出来，
    // 比一直显示「正在思考中」更能解释这段时间在干什么。
    liveForRow(card, row) {
      const key = this.liveKeyOf(card, row);
      if (!key) return null;
      const cur = this.liveStreams[key];
      if (cur && (cur.text || (cur.activity && cur.phase === "tool"))) return cur;
      const last = this.liveLast[key];
      if (last && last.text && this.briefBusy(row)) return last;
      return null;
    },
    // 单任务结果消息：执行阶段永远是主创本人，key 与卡片执行段同源
    liveForMsg(msg) {
      const tid = (msg && msg.task_id) || "";
      if (!tid) return null;
      const key = `${tid}:exec:${msg.sender || ""}`;
      const cur = this.liveStreams[key];
      if (cur && (cur.text || (cur.activity && cur.phase === "tool"))) return cur;
      const last = this.liveLast[key];
      if (last && last.text && ["running", "queued"].includes(msg.status)) return last;
      return null;
    },

    // 「输入输出」弹窗的入参：从卡片直接取那一段（补齐起止时刻，
    // 详情弹窗里点进来的和聊天卡片里点进来的是同一份数据、同一个弹窗）
    inspectForCard(card, key, memberId) {
      const rows = this.cardBriefRows(card || {});
      const target = memberId ? `review:${memberId}` : key;
      const row = rows.find(r => r.key === target);
      if (!row) return {};
      return Object.assign({}, row.inspect, { sender_name: row.name, stage_label: row.role });
    },

    // 每段简报自己的重跑入口：执行段重跑主创、审核段重跑这一位、交付段重出终稿。
    // 运行中的那一段不再给「重跑」，换成「取消」，同一个环节不能并发两份执行。
    briefAction(card, row) {
      if (!card || !row) return null;
      const key = row.key || "";
      const stage = key === "deliver"
        ? "deliver"
        : (key.indexOf("review:") === 0 ? "review" : "exec");
      const memberId = stage === "review" ? (row.memberId || key.slice(7)) : "";
      const st = (row.status || "").toLowerCase();
      if (this.briefBusy(row)) {
        return { kind: "cancel", stage, memberId, label: this.t("取消") };
      }
      const bad = ["failed", "timeout", "cancelled", "no_opinion"];
      if (stage === "exec") {
        if (st === "done" || st === "delivered") {
          return { kind: "retry", stage, memberId, label: this.t("重新执行") };
        }
        if (bad.includes(st)) return { kind: "retry", stage, memberId, label: this.t("重试执行") };
        return null;
      }
      if (stage === "review") {
        if (st === "done") return { kind: "retry", stage, memberId, label: this.t("重新审核") };
        if (bad.includes(st)) return { kind: "retry", stage, memberId, label: this.t("重试审核") };
        return null;
      }
      // 交付段：执行没成功之前不能重跑交付，否则会出一个没有产物也没有意见的「已交付」
      const a = card.part_a || {};
      if ((a.status || "") !== "done") return null;
      if (st === "done" || st === "delivered") {
        return { kind: "retry", stage, memberId, label: this.t("重新生成交付") };
      }
      if (bad.includes(st)) return { kind: "retry", stage, memberId, label: this.t("重试交付") };
      const settled = (card.part_b || []).every(r =>
        ["done", "failed", "timeout", "cancelled", "no_opinion"].includes((r.status || ""))
      );
      if ((st === "waiting" || !st) && settled) {
        return { kind: "retry", stage, memberId, label: this.t("生成交付") };
      }
      return null;
    },
    briefAct(msg, row) {
      const act = this.briefAction(msg.card, row);
      if (!act) return;
      if (act.kind === "cancel") {
        return this.cancelTask(msg, act.stage, act.memberId);
      }
      return this.retryTask(msg, act.stage, act.memberId);
    },

    // 三步进度的计算（弹窗与聊天流里的折叠卡片共用）
    cardStepsOf(card) {
      if (!card) return [];
      const a = card.part_a || {};
      const reviewStatus = this._reviewAggregate(card.part_b || []);
      const busy = s => ["running", "in_progress"].includes(s);
      return [
        { key: "exec", label: this.t("执行"), status: a.status || "waiting", active: busy(a.status) },
        { key: "review", label: this.t("审核"), status: reviewStatus, active: busy(reviewStatus) },
        { key: "deliver", label: this.t("交付"), status: a.final_status || "waiting", active: busy(a.final_status) },
      ];
    },

    // 多位协助成员的状态合成一个环节状态（进度条上只用这一个圆点）
    _reviewAggregate(reviewers) {
      if (!reviewers || !reviewers.length) return "waiting";
      const states = reviewers.map(r => r.status || "waiting");
      if (states.some(s => ["running", "in_progress"].includes(s))) return "in_progress";
      const ended = states.filter(s => ["done", "failed", "cancelled", "timeout", "no_opinion"].includes(s));
      if (ended.length !== states.length) return "waiting";
      if (states.some(s => ["failed", "timeout"].includes(s))) return "failed";
      if (states.every(s => s === "cancelled")) return "cancelled";
      return "done";
    },

    stepDotClass(status) {
      if (status === "done") return "bg-emerald-50 text-emerald-700 border-emerald-300";
      if (["failed", "timeout"].includes(status)) return "bg-red-50 text-red-600 border-red-300";
      if (status === "cancelled") return "bg-gray-100 text-gray-500 border-gray-300";
      if (["running", "in_progress"].includes(status)) return "bg-blue-50 text-blue-700 border-blue-400";
      return "bg-white text-gray-400 border-gray-300";
    },

    stepGlyph(step, idx) {
      if (step.status === "done") return "✓";
      if (["failed", "timeout"].includes(step.status)) return "!";
      if (step.status === "cancelled") return "–";
      const i = typeof idx === "number" ? idx : this.cardSteps.findIndex(s => s.key === step.key);
      return String(i + 1);
    },

    // ----------------- 任务卡片与消息上的取消 / 重试 -----------------
    openCardModal(msg) {
      this.cardModalMsg = msg;
      this.showCardModal = true;
    },
    openInspectMsg(payload) {
      // 起止时刻 / 耗时必须一起透传：弹窗顶部的「开始 · 返回 · 用时」读的是
      // inspectingMsg 上的这些字段，卡片每段与历次执行记录都带了，漏一个就整行不显示。
      const p = payload || {};
      this.openInspectModal({
        id: "task-record",
        sender: "task",
        sender_name: p.sender_name || this.t("任务记录"),
        content: p.content || "",
        sent_prompt: p.sent_prompt || "",
        raw_response: p.raw_response || "",
        // 没有 Prompt 时要区分「这一环节还没开始/正在跑」和「老记录没留存」，
        // 一律说成「持久化上线前生成」会让人以为功能坏了
        stage_status: p.stage_status || "",
        stage_label: p.stage_label || "",
        started_at: p.started_at || null,
        finished_at: p.finished_at || null,
        created_at: p.created_at || null,
        elapsed: Number(p.elapsed || p.elapsed_time || 0)
      });
    },
    _taskId(msg) {
      return msg && (msg.task_id || (msg.card && msg.card.task_id));
    },
    async _taskAction(msg, stage, memberId, action) {
      const tid = this._taskId(msg);
      if (!tid || !this.currentSessionId) return;
      try {
        await fetch(`/api/sessions/${this.currentSessionId}/tasks/${tid}/${action}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ stage, member_id: memberId || "" })
        });
        // 静默：不弹窗、不 toast，输入框本来就一直可用
        await this.refreshSession();
      } catch (e) {
        console.error(`${action} 任务失败:`, e);
      }
    },
    cancelTask(msg, stage, memberId = "") {
      return this._taskAction(msg, stage, memberId, "cancel");
    },
    retryTask(msg, stage, memberId = "") {
      return this._taskAction(msg, stage, memberId, "retry");
    },
    async refreshSession() {
      if (!this.currentSessionId) return;
      const res = await fetch(`/api/sessions/${this.currentSessionId}`);
      if (!res.ok) return;
      const latest = await res.json();
      if (latest.id !== this.currentSessionId) return;
      this.currentSession = latest;
      if (this.showCardModal && this.cardModalMsg) {
        const updated = (latest.messages || []).find(m => m.id === this.cardModalMsg.id);
        if (updated) this.cardModalMsg = updated;
      }
    },
    stateLabel(status) {
      const map = {
        queued: "排队中", running: "执行中", delivered: "已交付", cancelled: "已取消",
        failed: "失败", timeout: "超时", no_opinion: "无意见", done: "已完成",
        waiting: "等待", in_progress: "进行中"
      };
      return this.t(map[status] || status || "等待");
    },
    statusClass(status) {
      if (["delivered", "done", "completed"].includes(status)) return "bg-emerald-50 text-emerald-700 border-emerald-200";
      if (["failed", "timeout", "cancelled"].includes(status)) return "bg-rose-50 text-rose-700 border-rose-200";
      if (["running", "in_progress"].includes(status)) return "bg-blue-50 text-blue-700 border-blue-200";
      return "bg-gray-50 text-gray-600 border-gray-200";
    },
    stageLabel(card) {
      if (!card) return "";
      if (card.status === "delivered") return this.t("已交付");
      if (card.status === "cancelled") return this.t("已取消");
      if (card.step === 1) return this.t("主创执行中");
      if (card.step === 2) return this.t("成员并行审核中");
      if (card.step === 3) return this.t("主创交付中");
      return this.t("排队中");
    },
    // 卡片是否有环节正在跑：头部要据此显示「开始时刻 + 实时计时」
    cardIsBusy(card) {
      if (!card) return false;
      const a = card.part_a || {};
      if (["running", "in_progress"].includes(String(a.status || ""))) return true;
      if (["running", "in_progress"].includes(String(a.final_status || ""))) return true;
      return (card.part_b || []).some(i =>
        ["running", "in_progress"].includes(String(i.status || ""))
      );
    },
    // 这一轮到底从哪一刻开始：优先用「正在跑的那一段」的 started_at。
    // 单条重审 / 重新生成交付都不该沿用整条任务最早的 started_at（那会显示成跑了半小时）。
    cardClockStart(card) {
      if (!card) return 0;
      const a = card.part_a || {};
      if (["running", "in_progress"].includes(String(a.status || "")) && a.started_at) {
        return Number(a.started_at);
      }
      if (["running", "in_progress"].includes(String(a.final_status || "")) && a.final_started_at) {
        return Number(a.final_started_at);
      }
      const busy = (card.part_b || []).find(i =>
        ["running", "in_progress"].includes(String(i.status || "")) && i.started_at
      );
      if (busy) return Number(busy.started_at);
      return Number(card.started_at || 0);
    },
    taskElapsed(card) {
      const start = this.cardClockStart(card);
      if (!start) return "0s";
      const end = card.finished_at || Math.floor(this.nowTick / 1000);
      const sec = Math.max(0, end - start);
      if (sec < 60) return `${sec}s`;
      return `${Math.floor(sec / 60)}m ${sec % 60}s`;
    },
    // 单任务结果消息（如给 agy 发消息）顶部的时间：
    // 执行中实时跳动（nowTick 驱动），完成后按 started_at/finished_at 算真实耗时，
    // 全都统一进阶成分钟；老消息没有 started_at 就退回 elapsed_time。
    msgElapsed(msg) {
      if (!msg) return "";
      const s = Number(msg.started_at || 0);
      const f = Number(msg.finished_at || 0);
      let sec;
      if (s) {
        const end = f || Math.floor(this.nowTick / 1000);
        sec = Math.max(0, Math.round(end - s));
      } else {
        sec = Math.round(Number(msg.elapsed || msg.elapsed_time || 0));
        if (!sec) return "";
      }
      if (sec < 60) return `${sec}s`;
      const m = Math.floor(sec / 60);
      const r = sec % 60;
      return r ? `${m}m ${r}s` : `${m}m`;
    },
    // 同一成员多个任务排队时，外壳上显示它在队列里的位置
    queueHint(taskId, memberId) {
      const queues = this.currentSession?.task_queues || {};
      const tasks = this.currentSession?.tasks || {};
      const done = ["delivered", "cancelled", "failed", "timeout"];
      const queue = (queues[memberId] || []).filter(t => {
        const s = (tasks[t] || {}).status;
        return s && !done.includes(s);
      });
      const idx = queue.indexOf(taskId);
      if (idx < 0 || queue.length < 2) return "";
      return this.t("该成员队列：第 {n} / {m} 项", { n: idx + 1, m: queue.length });
    },
    recordLabel(rec, seq) {
      const stage = rec.stage === "review" ? this.t("审核")
        : rec.stage === "deliver" ? this.t("交付") : this.t("执行");
      const name = rec.sender_name || rec.member_id || "";
      return `${name} · ${stage} · ${this.t("第 {n} 次", { n: seq })}`;
    },
    formatFlowTime(ts) {
      if (!ts) return "--:--:--";
      return new Date(ts * 1000).toLocaleTimeString();
    },
    // 卡片上的「创建时间」要带日期：隔天的任务只看 12:30 分不清是哪一天。
    // 新任务的 created_at 由 build_card 透传；老会话退回卡片消息落库的时刻。
    cardCreatedAt(msg) {
      const card = (msg && msg.card) || {};
      const ts = Number(card.created_at || (msg && msg.created_at) || 0);
      if (!ts) return "";
      const d = new Date(ts * 1000);
      if (isNaN(d.getTime())) return "";
      const pad = n => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${d.toLocaleTimeString()}`;
    },
    // 交付段标签的悬停说明：把自动补交付的时间一并带上，方便复盘
    autoDeliverTitle(card) {
      const at = Number((card && card.deliver_source_at) || 0);
      const base = this.t("全部审核出结论后自动补一次交付，这一版终稿是按最新审核意见重新生成的");
      return at ? `${base} · ${this.t("重新生成于 {a}", { a: this.formatFlowTime(at) })}` : base;
    },

    // ------------------- 多语言 -------------------
    // t("中文原文")：中文直接返回原文，英文查 i18n.js 的词典；查不到时退回中文，
    // 这样漏翻一个词只会显示中文，不会出现空白按钮或 undefined。
    t(text, params) {
      if (text === undefined || text === null) return "";
      const dict = (window.AIM_I18N && window.AIM_I18N.en) || {};
      let out = this.lang === "en" ? (dict[text] !== undefined ? dict[text] : text) : text;
      if (params) {
        for (const key of Object.keys(params)) {
          out = String(out).split(`{${key}}`).join(params[key]);
        }
      }
      return out;
    },
    // 历史消息里的 sender_name 已经落盘，语言切换后也要跟着换；
    // 成员名 / 模型名不在映射表里，原样保留。
    displaySender(name) {
      const s = String(name || "");
      if (this.lang !== "en") return s;
      const labels = { "我": "Me", "任务卡片": "Task card", "任务记录": "Task record" };
      return labels[s] || s;
    },
    applyLanguage() {
      const en = this.lang === "en";
      document.documentElement.setAttribute("lang", en ? "en" : "zh-CN");
      document.title = en ? "AI-Meeting · Multi-AI Task Workspace" : "AI-Meeting · 多 AI 协同任务工作台";
      try { localStorage.setItem("aim.lang", this.lang); } catch (e) { /* 隐私模式忽略 */ }
    },
    async setLanguage(lang) {
      const next = lang === "en" ? "en" : "zh";
      if (next === this.lang) return;
      this.lang = next;
      this.applyLanguage();
      // 语言是系统级偏好：写回 settings.json，其他浏览器打开也是同一种语言
      try {
        const res = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...this.settings, ui_language: next })
        });
        if (res.ok) {
          const data = await res.json();
          if (data.settings) this.settings = data.settings;
        }
      } catch (e) {
        console.warn("语言设置未能同步到服务端（本机仍已切换）:", e);
      }
    },

    // ------------------- 设置中心 -------------------
    openSettings(tab = "members") {
      this.settingsTab = tab;
      this.showSettings = true;
      if (tab === "service") this.fetchSystemStatus();
    },
    // 批量开关写权限：只改内存里的配置，仍需点「保存配置」才落盘
    setAllWriteAccess(enabled) {
      (this.settings.employees || []).forEach(emp => { emp.write_access = !!enabled; });
      this.showToast(enabled
        ? this.t("已为所有成员勾选写权限，点「保存配置」后生效。")
        : this.t("已关闭所有成员的写权限，点「保存配置」后生效。"));
    },

    // 结束当前的自主任务（负责人不再自动往下跑，已产出的内容都保留）
    onWindowResize() {
      this.clampPanels();
      // 连续缩放窗口时 resize 每秒能来几十次，合并成一帧后再量
      this.scheduleMsgOverflowMeasure();
    },

    handleGlobalClick(e) {
      const el = e.target && e.target.closest ? e.target : null;
      if (!el || !el.closest(".relative")) {
        this.showMemberDropdown = false;
        this.sessionReasoningEmpId = "";
      }
      // 模型浮层被 Teleport 到 body，不在弹窗的 .relative 里：单独判断是否点在外面
      if (!el || (!el.closest(".model-dropdown") && !el.closest(".model-trigger"))) {
        this.modelDropdownOpen = false;
      }
    },

    // ------------------- 布局：收起 / 拖拽 / 全屏 -------------------
    restoreLayout() {
      try {
        const saved = JSON.parse(localStorage.getItem("aim.layout") || "{}");
        if (Number.isFinite(saved.sidebarWidth)) this.sidebarWidth = saved.sidebarWidth;
        if (Number.isFinite(saved.rightWidth)) this.rightWidth = saved.rightWidth;
        if (typeof saved.sidebarCollapsed === "boolean") this.sidebarCollapsed = saved.sidebarCollapsed;
      } catch (e) { /* 忽略损坏的本地配置 */ }
      this.clampPanels();
    },
    persistLayout() {
      try {
        localStorage.setItem("aim.layout", JSON.stringify({
          sidebarWidth: this.sidebarWidth,
          rightWidth: this.rightWidth,
          sidebarCollapsed: this.sidebarCollapsed
        }));
      } catch (e) { /* 隐私模式下写不了，忽略 */ }
    },
    // 两边都留出中间聊天区的最低宽度（320px），否则拖到极端会把对话挤没
    clampPanels() {
      const total = window.innerWidth || 1440;
      const minChat = 320;
      const maxSidebar = Math.max(180, Math.min(520, total - minChat - 260));
      this.sidebarWidth = Math.round(Math.min(Math.max(this.sidebarWidth, 180), maxSidebar));
      const maxRight = Math.max(300, total - (this.sidebarCollapsed ? 0 : this.sidebarWidth) - minChat - 4);
      this.rightWidth = Math.round(Math.min(Math.max(this.rightWidth, 300), maxRight));
    },
    startResize(target, e) {
      this.dragTarget = target;
      this.dragStartX = e.clientX;
      this.dragStartW = target === "sidebar" ? this.sidebarWidth : this.rightWidth;
      document.body.style.cursor = "col-resize";
      document.documentElement.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    onDragMove(e) {
      if (!this.dragTarget) return;
      e.preventDefault();
      const dx = e.clientX - this.dragStartX;
      if (this.dragTarget === "sidebar") {
        // 侧栏向右拖变宽；拖到 120px 以下视为「收起」
        const next = this.dragStartW + dx;
        if (next < 120) {
          if (!this.sidebarCollapsed) {
            this.sidebarCollapsed = true;
            // 重设锚点：收起后继续往右拖可以直接展开，不用先拖回 136px
            this.dragStartW = 0;
            this.dragStartX = e.clientX;
          }
          return;
        }
        if (this.sidebarCollapsed) {
          this.sidebarCollapsed = false;
          this.dragStartW = 0;
          this.dragStartX = e.clientX;
        }
        this.sidebarWidth = Math.round(next);
      } else {
        // 右侧面板向左拖变宽（方向相反）
        this.rightWidth = Math.round(this.dragStartW - dx);
      }
      this.clampPanels();
    },
    onDragEnd() {
      // 鼠标在窗口外松开时 mouseup 可能收不到：blur / 重新回到窗口时也走一遍，
      // 否则页面会一直停在「col-resize + 不能选中文字」的状态，继续跟着鼠标改宽度
      const wasDragging = !!this.dragTarget;
      this.dragTarget = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      document.documentElement.style.cursor = "";
      if (!wasDragging) return;
      this.clampPanels();
      this.persistLayout();
      this.scheduleMsgOverflowMeasure();
    },
    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed;
      this.clampPanels();
      this.persistLayout();
    },
    async init() {
      // 先用本地缓存把语言铺上（避免首屏中文闪一下），服务端设置是最终依据
      this.applyLanguage();
      await this.loadDefaultWorkspace();
      await this.loadSettings();
      const serverLang = this.settings?.ui_language;
      if (serverLang === "en" || serverLang === "zh") {
        if (serverLang !== this.lang) this.lang = serverLang;
        this.applyLanguage();
      } else {
        this.settings.ui_language = this.lang;
      }
      await this.loadSessions();
      await this.triggerScanModels(false);

      if (this.sessions.length > 0) {
        // 刷新页面（尤其是「正在回答」时刷新）要回到刚才那场会，而不是列表第一场：
        // 会话数据在服务端，刷新不影响任务进程，只有选错会话才会让人误以为「丢了」。
        const remembered = this.readLastSessionId();
        const target = this.sessions.find(s => s.id === remembered) || this.sessions[0];
        await this.switchSession(target.id);
      } else {
        this.openNewSessionModal();
      }
    },

    async loadDefaultWorkspace() {
      try {
        const res = await fetch("/api/default_workspace");
        const data = await res.json();
        this.defaultWorkspaceDir = data.workspace_dir || "";
      } catch (e) {
        this.defaultWorkspaceDir = "";
      }
    },

    escapeHtml(text) {
      return String(text == null ? "" : text).replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    },

    // ----------------- 会话成员：会话级推理程度 -----------------
    toggleSessionReasoning(empId) {
      this.sessionReasoningEmpId = this.sessionReasoningEmpId === empId ? "" : empId;
    },

    // 优先用会话覆盖表里的 model_key，老配置则按 cli_cmd 反查（含推理档位）
    sessionMemberModel(empId) {
      const ov = ((this.currentSession || {}).member_models || {})[empId];
      const emp = (this.settings.employees || []).find(e => e.id === empId) || {};
      let model = null;
      let reasoning = "";
      if (ov && ov.model_key) {
        model = (this.scannedModels || [])
          .find(m => this.modelKey(m) === ov.model_key) || null;
        reasoning = ov.reasoning || "";
      }
      if (!model) {
        const hit = this.findModelByCli((ov && ov.cli_cmd) || emp.cli_cmd || "");
        model = hit ? hit.model : null;
        reasoning = (hit && hit.level && hit.level.id)
          || (ov && ov.reasoning) || emp.reasoning || "";
      }
      return { model, reasoning, override: !!ov };
    },

    sessionReasoningLevels(empId) {
      const info = this.sessionMemberModel(empId);
      return info.model ? (info.model.reasoning_levels || []) : [];
    },

    sessionReasoningLabel(empId) {
      const info = this.sessionMemberModel(empId);
      const levels = (info.model && info.model.reasoning_levels) || [];
      const lv = levels.find(x => x.id === info.reasoning);
      return lv ? lv.label : "";
    },

    // 只改当前会话：PUT 一条 session.member_models[emp_id]，全局设置不动
    async setSessionReasoning(empId, lv) {
      if (!this.currentSession || !lv) return;
      const info = this.sessionMemberModel(empId);
      if (!info.model) return;
      const overrides = Object.assign({}, (this.currentSession.member_models || {}));
      overrides[empId] = {
        model_key: this.modelKey(info.model),
        reasoning: lv.id,
        cli_cmd: lv.cli_cmd
      };
      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ member_models: overrides })
        });
        if (!res.ok) {
          const d = await res.json().catch(() => ({}));
          this.showToast(d.error || `HTTP ${res.status}`);
          return;
        }
        this.currentSession = await res.json();
        this.sessionReasoningEmpId = "";
        this.showToast(this.t("已为当前会话设置推理程度"));
      } catch (e) {
        this.showToast(this.t("设置推理程度失败:") + " " + e);
      }
    },

    async loadSettings() {
      try {
        const res = await fetch("/api/settings");
        const data = await res.json();
        // 设置只有界面语言与成员列表
        const employees = (Array.isArray(data.employees) ? data.employees : []).map(e => ({
          ...e, write_access: !!e.write_access
        }));
        this.settings = {
          ui_language: data.ui_language === "en" ? "en" : "zh",
          employees
        };
      } catch (e) {
        console.error("加载配置失败:", e);
      }
    },

    async loadSessions() {
      try {
        const res = await fetch("/api/sessions");
        this.sessions = await res.json();
      } catch (e) {
        console.error("加载会话列表失败:", e);
      }
    },

    // 记住最近打开的是哪一场（只存 id，真正的消息都在服务端）
    readLastSessionId() {
      try {
        return localStorage.getItem("aim.lastSession") || "";
      } catch (e) {
        return "";
      }
    },
    persistLastSessionId(id) {
      try {
        if (id) localStorage.setItem("aim.lastSession", id);
      } catch (e) { /* 隐私模式写不了，忽略 */ }
    },

    async switchSession(id) {
      this.currentSessionId = id;
      this.persistLastSessionId(id);
      // 请求序号：快速连切会话时，丢弃已过期的在途响应，避免界面显示 A、操作发往 B
      const token = this.switchToken + 1;
      this.switchToken = token;
      this.selectedWorkspaceFile = null;
      this.selectedFileContent = "";
      this.showMemberDropdown = false;
      this.sessionReasoningEmpId = "";
      this.suggest = null;
      // 引用与展开状态都属于具体会话的某条消息，切会话就清掉
      this.replyTarget = null;
      this.expandedMsgs = {};
      try {
        const res = await fetch(`/api/sessions/${id}`);
        if (token !== this.switchToken) return;
        if (res.ok) {
          this.currentSession = await res.json();
          this.pinToBottom();
          this.measureMsgOverflow();
          this.resetWorkspaceDir();
          this.loadWorkspaceFiles();
        }
      } catch (e) {
        console.error("切换会话失败:", e);
      }
    },

    // ----------------- 新建会话 -----------------
    openNewSessionModal() {
      const defaultDir = this.currentSession?.workspace_dir || this.defaultWorkspaceDir;
      this.newSessionForm = {
        title: "",
        workspace_dir: defaultDir,
        employee_ids: this.availableEmployees.map(e => e.id)
      };
      this.showNewSessionModal = true;
    },

    async confirmCreateSession() {
      const title = this.newSessionForm.title.trim() || this.t("新会话");
      try {
        const res = await fetch("/api/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: title,
            workspace_dir: this.newSessionForm.workspace_dir.trim(),
            employee_ids: this.newSessionForm.employee_ids
          })
        });
        const newSession = await res.json();
        this.showNewSessionModal = false;
        await this.loadSessions();
        await this.switchSession(newSession.id);
      } catch (e) {
        alert(this.t("创建会话失败:") + " " + e);
      }
    },

    // ----------------- 编辑会话信息 Modal（标题/目录/成员） -----------------
    openEditSessionModal(session) {
      if (!session) return;
      this.editingSessionForm = {
        id: session.id,
        title: session.title,
        workspace_dir: session.workspace_dir || this.defaultWorkspaceDir,
        employee_ids: [...(session.employee_ids || [])]
      };
      this.showEditSessionModal = true;
    },

    async saveEditedSession() {
      const sid = this.editingSessionForm.id;
      if (!sid) return;
      try {
        const res = await fetch(`/api/sessions/${sid}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: this.editingSessionForm.title.trim(),
            workspace_dir: this.editingSessionForm.workspace_dir.trim(),
            employee_ids: this.editingSessionForm.employee_ids
          })
        });
        if (res.ok) {
          const updated = await res.json();
          this.showEditSessionModal = false;
          await this.loadSessions();
          if (this.currentSessionId === sid) {
            this.currentSession = updated;
            this.resetWorkspaceDir();
            this.loadWorkspaceFiles();
          }
        } else {
          // 失败原因要回显，否则用户只会看到弹窗关不掉
          const data = await res.json().catch(() => ({}));
          alert(data.error || `HTTP ${res.status}`);
        }
      } catch (e) {
        alert(this.t("保存会话配置失败:") + " " + e);
      }
    },

    // ----------------- 本地文件夹选择器 Modal -----------------
    async openFolderPicker(targetForm = "newSessionForm") {
      this.folderPickerTargetForm = targetForm;
      let startPath = "";
      if (targetForm === "newSessionForm") {
        startPath = this.newSessionForm.workspace_dir || "";
      } else if (targetForm === "editingSessionForm") {
        startPath = this.editingSessionForm.workspace_dir || "";
      }
      await this.browseFolder(startPath);
      this.showFolderPickerModal = true;
    },

    async browseFolder(path = "") {
      try {
        const res = await fetch(`/api/browse_dirs?path=${encodeURIComponent(path)}`);
        const data = await res.json();
        this.folderPickerPath = data.current || "";
        this.folderPickerParent = data.parent || null;
        this.folderPickerDirs = data.dirs || [];
      } catch (e) {
        console.error("浏览目录失败:", e);
      }
    },

    confirmSelectFolder() {
      if (this.folderPickerTargetForm === "newSessionForm") {
        this.newSessionForm.workspace_dir = this.folderPickerPath;
      } else if (this.folderPickerTargetForm === "editingSessionForm") {
        this.editingSessionForm.workspace_dir = this.folderPickerPath;
      }
      this.showFolderPickerModal = false;
    },

    // ----------------- 收藏与删除 -----------------
    async toggleFavorite(session) {
      try {
        await fetch(`/api/sessions/${session.id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ is_favorite: !session.is_favorite })
        });
        await this.loadSessions();
        if (this.currentSessionId === session.id) {
          this.currentSession.is_favorite = !session.is_favorite;
        }
      } catch (e) {
        console.error("收藏操作失败:", e);
      }
    },

    async deleteSessionItem(id) {
      if (!confirm(this.t("确定要删除此会话记录吗？"))) return;
      try {
        await fetch(`/api/sessions/${id}`, { method: "DELETE" });
        await this.loadSessions();
        if (this.currentSessionId === id) {
          if (this.sessions.length > 0) {
            await this.switchSession(this.sessions[0].id);
          } else {
            this.openNewSessionModal();
          }
        }
      } catch (e) {
        console.error("删除失败:", e);
      }
    },

    async saveSessionEmployees() {
      if (!this.currentSessionId) return;
      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ employee_ids: this.currentSession.employee_ids })
        });
        if (res.ok) {
          const updated = await res.json();
          this.currentSession = updated;
          this.pinToBottom();
          await this.loadSessions();
        }
      } catch (e) {
        console.error("更新会话成员失败:", e);
      }
    },

    // ----------------- 输入法状态与回车防误触 -----------------
    onCompositionEnd() {
      // 保持短暂标志位，抵消浏览器在 compositionend 与 keydown(Enter) 之间的瞬态事件
      this.isComposing = true;
      setTimeout(() => {
        this.isComposing = false;
      }, 120);
    },

    // ----------------- 输入候选：打 / 出命令，打 @ 出成员 -----------------
    // /collab 的格式校验（主创 + 协助成员 + 任务内容 + 成员可用）由服务端给答案：
    // 输入停下来 250ms 再问，边打字边请求没意义，也避免每键一个响应互相覆盖。
    scheduleCollabCheck() {
      if (this.collabCheckTimer) {
        clearTimeout(this.collabCheckTimer);
        this.collabCheckTimer = null;
      }
      const text = (this.inputContent || "").trim();
      if (!/^\/collab(\s|$)/i.test(text)) {
        this.collabCheck = null;
        return;
      }
      this.collabCheckTimer = setTimeout(async () => {
        this.collabCheckTimer = null;
        try {
          const res = await fetch("/api/collab/validate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content: text }),
          });
          const data = await res.json().catch(() => ({}));
          // 期间用户又改了字：这份结果已经过期，直接丢掉
          if (String(this.inputContent || "").trim() !== text) return;
          this.collabCheck = { content: text, ok: !!data.ok, error: data.error || "" };
        } catch (err) {
          // 校验请求失败不该打断输入，交给发送时的服务端校验兜底
        }
      }, 250);
    },

    // 多 @ 的并行分工预览：同样交给服务端算（/api/mentions/scan），停下 250ms 再问。
    // 只在「有 @ 且不是 /collab」时问，避免每敲一个键发一个请求。
    scheduleFanoutCheck() {
      if (this.fanoutCheckTimer) {
        clearTimeout(this.fanoutCheckTimer);
        this.fanoutCheckTimer = null;
      }
      const text = (this.inputContent || "").trim();
      if (!text.includes("@") || /^\/collab(\s|$)/i.test(text)) {
        this.fanoutCheck = null;
        return;
      }
      this.fanoutCheckTimer = setTimeout(async () => {
        this.fanoutCheckTimer = null;
        try {
          const res = await fetch("/api/mentions/scan", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content: text }),
          });
          const data = await res.json().catch(() => ({}));
          // 期间又改了字：这份结果已过期，丢掉
          if (String(this.inputContent || "").trim() !== text) return;
          this.fanoutCheck = {
            content: text,
            fanout: !!data.fanout,
            names: Array.isArray(data.names) ? data.names : [],
            error: data.error || "",
          };
        } catch (err) {
          // 预览失败不打断输入；发送时服务端还有一次权威校验
        }
      }, 250);
    },

    // 触发条件只看「光标前是否是一个刚敲的 token」，不看输入历史：    // 这样在句子中间补一个 @ 也能弹，而段落里打路径（如 a/b）不会乱弹命令。
    onInputSuggest(e) {
      this.scheduleCollabCheck();
      this.scheduleFanoutCheck();
      const el = (e && e.target) || this.$refs.chatInput;
      const text = String(this.inputContent || "");
      const caret = el && Number.isFinite(el.selectionStart) ? el.selectionStart : text.length;
      const before = text.slice(0, caret);

      // 程序刚替用户打开候选（例如选完 /collab）时，人会直接接着打名字，不会再打一个 @。
      // 这段追加的文本当查询词处理，别因为「前面没有 @ 前缀」就把菜单关掉。
      if (this.suggest && this.suggest.implicit) {
        const from = this.suggest.start;
        const q = caret >= from ? text.slice(from, caret) : "";
        // 成员名可以带空格（"gemini 3.8"），不能再靠「不含空白」判断候选是否该继续；
        // 只要 q 仍是某位成员名字 / id 的前缀就继续弹，打歪了或开始写正文就关掉。
        const stillPrefix = caret >= from && !/[@/\n]/.test(q) &&
          this.activeSessionEmployees.some(emp =>
            String(emp.name || "").toLowerCase().startsWith(q.toLowerCase()) ||
            String(emp.id || "").toLowerCase().startsWith(q.toLowerCase()));
        if (stillPrefix) {
          this.suggest = { ...this.suggest, query: q, items: this.memberSuggestItems(q), active: 0, end: caret };
          return;
        }
        this.suggest = null;
      }

      // 左边界用「非单词字符」而不是「空白」：中文不打空格，`请@agy` 也要能弹；
      // 同时把 a@b、src/app.js 这类邮箱/路径挡在外面。
      const hit = /(^|[^\w.\-@/])([@/][^\s@/]*)$/.exec(before);
      let start, token;
      if (hit) {
        start = hit.index + hit[1].length;
        token = before.slice(start);
      } else {
        // 名字带空格时手打到中间（`@gemini 3`）会被上面的单 token 正则丢掉，
        // 交给 spacedMentionToken 判断是不是还停在某个成员名字的前缀上。
        const spaced = this.spacedMentionToken(before);
        if (!spaced) {
          this.suggest = null;
          return;
        }
        start = spaced.start;
        token = spaced.token;
      }
      const sigil = token.slice(0, 1);
      const query = token.slice(1);

      if (sigil === "/") {
        // 命令只在行首生效：后端只认 ^/collab，别的地方打 / 多半是路径
        if (start !== 0) {
          this.suggest = null;
          return;
        }
        const q = query.toLowerCase();
        const items = this.commandCandidates.filter(c => c.label.toLowerCase().startsWith("/" + q));
        this.suggest = { kind: "command", query, items, active: 0, start, end: caret };
        return;
      }

      const items = this.memberSuggestItems(query);
      if (!items.length) {
        this.suggest = null;
        return;
      }
      this.suggest = { kind: "member", query, items, active: 0, start, end: caret };
    },

    // 成员名允许带空格（"gemini 3.8"）：手打到 `@gemini 3` 时，上面的单 token
    // 正则会把候选菜单误关。只在「@ 后面这段文本仍是某位会话成员名字 / id 的
    // 前缀」时续开菜单；句子里正常的空格、写错的名字都不会把菜单一直吊着。
    spacedMentionToken(before = "") {
      const at = before.lastIndexOf("@");
      // 与后端 _mention_start_ok 对齐：a@b、src/app.js 这类不算提及
      if (at < 0 || (at > 0 && /[\w.\-@/]/.test(before[at - 1]))) return null;
      const raw = before.slice(at);
      const query = raw.slice(1);
      if (!/\s/.test(query) || /[\n\r@]/.test(query)) return null;
      const q = query.toLowerCase();
      const stillPrefix = this.activeSessionEmployees.some(emp =>
        String(emp.name || "").toLowerCase().startsWith(q) ||
        String(emp.id || "").toLowerCase().startsWith(q));
      return stillPrefix ? { start: at, token: raw } : null;
    },

    // Backspace 整删 @ 标签：带空格的名字只认「@ 后面恰好是某位成员的完整
    // 名字 / id / 括号短名（可带一个尾空格）」。不加这层校验的话，把正则放宽成
    // `@[^@]+` 会在 `@gemini 3.8 请写代码` 里一次删掉整句话。
    spacedMentionTag(textBefore = "") {
      const at = textBefore.lastIndexOf("@");
      if (at < 0 || (at > 0 && /[\w.\-@/]/.test(textBefore[at - 1]))) return null;
      const raw = textBefore.slice(at);
      const body = raw.replace(/\s$/, "").slice(1);
      if (!/\s/.test(body) || /[\n\r@]/.test(body)) return null;
      const q = body.toLowerCase();
      const isMember = this.activeSessionEmployees.some(emp => {
        const name = String(emp.name || "");
        const shorts = (name.match(/[（(]([^（）()]+)[）)]/g) || []).map(s => s.slice(1, -1));
        return [name, emp.id, ...shorts]
          .some(v => String(v || "").toLowerCase() === q);
      });
      return isMember ? raw : null;
    },

    // @ 候选：按名字 / id / 括号短名（DS4、HY4）做不区分大小写的包含匹配
    memberSuggestItems(query = "") {
      const q = String(query || "").trim().toLowerCase();
      const matched = this.activeSessionEmployees.filter(emp => {
        if (!q) return true;
        const name = String(emp.name || "");
        const short = (name.match(/[（(]([^（）()]+)[）)]/g) || []).map(s => s.slice(1, -1));
        return [name, emp.id, ...short]
          .some(v => String(v || "").toLowerCase().includes(q));
      });
      return matched.map(emp => ({
        id: emp.id,
        kind: "member",
        label: emp.name || emp.id,
        avatar: emp.avatar || "🤖",
        desc: emp.write_access
          ? this.t("可读写")
          : (emp.read_access === false ? this.t("无工具") : this.t("只读")),
        // 缺 CLI 的成员点了也发不出去，这里直接置灰，别让人选完才发现
        disabled: !String(emp.cli_cmd || "").trim(),
        hint: this.t("缺少 CLI 配置"),
        insert: "@" + (emp.name || emp.id) + " ",
      }));
    },

    closeSuggest() {
      this.suggest = null;
    },
    // 候选是用 mousedown.prevent 点中的：不先让 textarea 失焦，选完光标还在输入框
    // 所以这里的 blur 处置只服务于「点别处关掉菜单」
    onSuggestBlur() {
      setTimeout(() => { this.suggest = null; }, 120);
    },
    moveSuggest(step) {
      const s = this.suggest;
      if (!s || !s.items.length) return;
      const n = s.items.length;
      let i = s.active;
      for (let k = 0; k < n; k++) {
        i = (i + step + n) % n;
        if (!s.items[i].disabled) break;
      }
      s.active = i;
    },
    pickSuggest(idx) {
      const s = this.suggest;
      if (!s) return;
      const item = s.items[idx];
      if (!item || item.disabled) return;
      const text = String(this.inputContent || "");
      const caret = s.start + item.insert.length;
      this.inputContent = text.slice(0, s.start) + item.insert + text.slice(s.end);
      this.suggest = null;
      this.scheduleCollabCheck();
      this.$nextTick(() => {
        const el = this.$refs.chatInput;
        if (!el) return;
        el.focus();
        el.selectionStart = el.selectionEnd = caret;
        // 选完 /collab 紧接着弹成员列表：先选主创，再选协助成员
        if (item.opensMembers) {
          const items = this.memberSuggestItems("");
          if (items.length) {
            this.suggest = {
              kind: "member", query: "", items, active: 0,
              start: caret, end: caret, implicit: true,
            };
          }
        }
      });
    },

    // ----------------- 消息输入与 Backspace 整删 @ 标签 -----------------
    onInputKeydown(e) {
      // 候选列表打开时先接管方向键与回车：此时回车是「确认候选」，不是发送
      if (this.suggest) {
        if (e.key === "ArrowDown") { e.preventDefault(); this.moveSuggest(1); return; }
        if (e.key === "ArrowUp") { e.preventDefault(); this.moveSuggest(-1); return; }
        if (e.key === "Escape") { e.preventDefault(); this.suggest = null; return; }
        if (e.key === "Enter" || e.key === "Tab") {
          if (e.isComposing || this.isComposing || e.keyCode === 229) return;
          e.preventDefault();
          this.pickSuggest(this.suggest.active);
          return;
        }
      }

      // Esc 撤掉引用回复条（不打断已打的字）
      if (e.key === "Escape" && this.replyTarget) {
        e.preventDefault();
        this.cancelReply();
        return;
      }

      if (e.key === "Backspace") {
        const textarea = this.$refs.chatInput;
        if (!textarea) return;
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        
        // 当光标未做选中且光标位置大于 0 时，检查光标前面是否紧挨着 @... 标签
        if (start === end && start > 0) {
          const textBefore = textarea.value.substring(0, start);
          // 匹配光标前最近的一个 @成员标签（例如 "@DeepSeek-V4 (DS4) "）
          // 名字带空格时（"@gemini 3.8 "）单 token 正则会漏掉，交给
          // spacedMentionTag 只认「完整成员名」，避免把整句话一口吞掉。
          const simple = textBefore.match(/@([^\s@]+)\s?$/);
          const tag = simple ? simple[0] : this.spacedMentionTag(textBefore);
          if (tag) {
            e.preventDefault();
            const tagLength = tag.length;
            const newText = textarea.value.substring(0, start - tagLength) + textarea.value.substring(start);
            this.inputContent = newText;
            this.$nextTick(() => {
              textarea.selectionStart = textarea.selectionEnd = start - tagLength;
            });
            return;
          }
        }
      }

      if (e.key === "Enter" && !e.shiftKey) {
        // 关键修复：输入法拼音/英文字符确认时按回车，绝不能误触发发送消息！
        if (e.isComposing || this.isComposing || e.keyCode === 229) {
          return;
        }
        e.preventDefault();
        this.sendMessage();
      }
    },

    async sendMessage() {
      const text = this.inputContent.trim();
      if (!text || !this.currentSessionId) return;

      // 输入时严格校验 /collab：缺主创或缺协助成员直接拦截，不发给后端
      const hint = this.collabHint;
      if (hint && !hint.ok) {
        this.showToast(hint.text);
        return;
      }

      const reply = this.replyTarget;
      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}/message`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            content: text,
            // 只传 id：引用对象与定向目标由服务端按存档解析，前端伪造不了
            reply_to: reply ? { id: reply.id } : null
          })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          // 失败时保留输入内容与引用条，别让用户白打一遍；校验错误走轻提示，不弹窗
          this.showToast(data.error || this.t("发送失败（HTTP {status}）", { status: res.status }));
          return;
        }
        this.inputContent = "";
        this.replyTarget = null;
        this.suggest = null;
        this.currentSession = data;
        this.pinToBottom();
        this.measureMsgOverflow();
        await this.loadSessions();
      } catch (e) {
        console.error("发送消息失败:", e);
        this.showToast(this.t("发送失败：") + e);
      }
    },

    // ----------------- 长消息折叠 -----------------
    // 判断依据是「真实渲染高度是否被限高截断」，而不是字数拍脑袋：
    // 窗口小、气泡窄时会自动更早折叠，正好对应「超过三十行或聊天窗口高度」。
    msgIsCollapsed(msg) {
      return !!(msg && msg.id) && !this.expandedMsgs[msg.id];
    },
    // 折叠用哪一档限高：
    // - 单任务回答给 ~20 行（用户要求「一眼能扫完，想看再展开」）
    // - 其余 AI 长文仍是「一屏内」
    // - 正在跑、还没有正文的消息不折叠：正文还没落定，切一半反而更难看
    msgClampClass(msg) {
      if (!msg || !msg.content) return "";
      if (!this.msgIsCollapsed(msg)) return "";
      return msg.kind === "task_result" ? "msg-clamped msg-clamped-task" : "msg-clamped";
    },
    toggleMsgExpand(msg) {
      const id = msg && msg.id;
      if (!id) return;
      // 整体替换对象，确保 Vue 3 的响应式一定被触发
      this.expandedMsgs = { ...this.expandedMsgs, [id]: !this.expandedMsgs[id] };
      this.$nextTick(this.measureMsgOverflow);
    },
    // 合并同一帧里的多次触发（拖拽 + 轮询 + 图片加载可能同时发生），
    // 避免一次缩放就把几十条消息量好几遍
    scheduleMsgOverflowMeasure() {
      if (this._overflowTimer) return;
      this._overflowTimer = setTimeout(() => {
        this._overflowTimer = null;
        this.measureMsgOverflow();
      }, 120);
    },
    measureMsgOverflow() {
      this.$nextTick(() => {
        const root = this.$refs.chatContainer;
        if (!root) return;
        const next = { ...this.overflowMsgs };
        let changed = false;
        root.querySelectorAll("[data-msg-id]").forEach(el => {
          const id = el.getAttribute("data-msg-id");
          if (!id || this.expandedMsgs[id]) return;   // 展开态没有限高，不参与测量
          const over = el.scrollHeight - el.clientHeight > 4;
          if (!!next[id] !== over) {
            next[id] = over;
            changed = true;
          }
        });
        if (changed) {
          this.overflowMsgs = next;
          // 折叠会改变消息高度，贴底时补滚一次，否则会停在「差一屏」的位置
          if (this.stickToBottom) this.$nextTick(() => this.scrollToBottom());
        }
      });
    },

    // ----------------- 引用回复（微信式） -----------------
    canReplyMsg(msg) {
      if (!msg || !msg.id) return false;
      if (msg.sender === "user" || msg.sender === "system") return false;
      if (!String(msg.content || "").trim()) return false;
      // 已移出本场会话的成员不再提供回复入口（后端也会拒绝）
      return this.activeSessionEmployees.some(e => e.id === msg.sender);
    },
    startReply(msg) {
      this.replyTarget = {
        id: msg.id,
        sender: msg.sender,
        sender_name: msg.sender_name || msg.sender,
        content: msg.content || ""
      };
      this.$nextTick(() => {
        if (this.$refs.chatInput) this.$refs.chatInput.focus();
      });
    },

    // Markdown 渲染（成员消息与任务结果）：本地依赖不可用时退回安全纯文本。
    // 加一层结果缓存：模板里每次 re-render 都会重新调用，不缓存就等于每 1.5 秒
    // 把整屏历史消息全部重新解析一遍（长会话下滚动会明显卡）。
    renderMdText(text) {
      const src = String(text || "");
      if (!src.trim()) return "";
      if (!this._mdCache) this._mdCache = new Map();
      const cached = this._mdCache.get(src);
      if (cached !== undefined) return cached;
      let out;
      // gfm + breaks：模型输出习惯用单个换行分行（不是空行分段）。
      // 不开 breaks 的话这些换行会被合并成一个空格，简报和审核意见会挤成一坨。
      if (window.marked && !this.__mdOptionsReady) {
        try { window.marked.setOptions({ gfm: true, breaks: true }); } catch (e) { /* 老版本没有 setOptions */ }
        this.__mdOptionsReady = true;
      }
      if (!window.marked || !window.DOMPurify) {
        out = `<pre style="white-space:pre-wrap">${this.escapeHtml(src)}</pre>`;
      } else {
        try {
          out = DOMPurify.sanitize(marked.parse(src));
        } catch (e) {
          out = `<pre style="white-space:pre-wrap">${this.escapeHtml(src)}</pre>`;
        }
      }
      if (this._mdCache.size > 150) this._mdCache.clear();
      out = this.linkifyFilePaths(out);
      this._mdCache.set(src, out);
      return out;
    },

    // 正文里的文件名标成可点：记 data-file-path，交给 mounted 里注册的
    // 捕获阶段点击处理器打开右侧预览面板。
    // 两种写法都认：行内代码 `A01.md`，以及裸文本（模型的回复大多不写反引号，
    // 甚至夹在《》里、后面跟 :65-83 行号）。代码块 <pre> 与链接 <a> 一律不动。
    linkifyFilePaths(html) {
      if (!html) return html;
      let doc;
      try {
        doc = new DOMParser().parseFromString(`<div id="__mdroot">${html}</div>`, "text/html");
      } catch (e) {
        return html;
      }
      const root = doc.getElementById("__mdroot");
      if (!root) return html;
      let touched = false;

      // 1) 行内代码：整段就是路径才认，避免 `a.md 和 b.md` 这种被当成一个文件
      root.querySelectorAll("code").forEach(el => {
        if (el.closest("pre") || el.querySelector("*")) return;
        const path = this.filePathFromText(el.textContent);
        if (!path) return;
        el.setAttribute("data-file-path", path);
        el.classList.add("file-link");
        el.setAttribute("title", this.t("点击在右侧预览面板打开"));
        touched = true;
      });

      // 2) 裸文本：按「边界 + 已知扩展名」切出文件名，只包这一小段
      const SKIP = ["CODE", "PRE", "A", "SCRIPT", "STYLE", "TEXTAREA"];
      const nodes = [];
      const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
          if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
          for (let p = node.parentNode; p && p !== root; p = p.parentNode) {
            if (SKIP.includes(p.tagName)) return NodeFilter.FILTER_REJECT;
          }
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(node => {
        if (this.wrapFileTokens(node)) touched = true;
      });

      return touched ? root.innerHTML : html;
    },

    // 把一段文本里像文件名的片段包成 <code class="file-link">
    wrapFileTokens(node) {
      const text = node.nodeValue || "";
      const tokens = this.findFileTokens(text);
      if (!tokens.length) return false;
      const doc = node.ownerDocument;
      const frag = doc.createDocumentFragment();
      let cursor = 0;
      tokens.forEach(t => {
        if (t.start > cursor) frag.appendChild(doc.createTextNode(text.slice(cursor, t.start)));
        const code = doc.createElement("code");
        code.className = "file-link";
        code.setAttribute("data-file-path", t.path);
        code.setAttribute("title", this.t("点击在右侧预览面板打开"));
        code.textContent = t.text;
        frag.appendChild(code);
        cursor = t.end;
      });
      if (cursor < text.length) frag.appendChild(doc.createTextNode(text.slice(cursor)));
      node.parentNode.replaceChild(frag, node);
      return true;
    },

    // 从纯文本里挑出文件名片段。
    // 左边界必须是行首 / 空白 / 常见标点（含中文标点与《》，模型爱把文件名括起来），
    // 右边界同理——文件名后面跟的是「（说明）」「，」还是「:65-83」都算结束，
    // 行号本身不进可点范围。
    findFileTokens(text) {
      const out = [];
      // 边界字符：空白 + 中英文里会出现「名字前后」的标点
      const B = "[\\s()（）【】\\[\\]《》<>\\\"'`「」『』：:,，。、；;！!？?]";
      const re = new RegExp(
        "(^|" + B + ")" +
        "([^\\s()（）【】\\[\\]《》<>\\\"'`「」『』：:，,。；;！!？?]{1,160}?\\.(?:" + FILE_EXT_ALT + "))" +
        "(?::\\d+(?:-\\d+)?)?" +
        "(?=$|" + B + ")",
        "gi"
      );
      let m;
      while ((m = re.exec(text)) !== null) {
        const name = m[2];
        const start = m.index + (m[1] ? m[1].length : 0);
        // URL 的尾巴（https 之后那段）不是文件；框架名同理
        if (name.includes("://") || name.startsWith("//")) continue;
        if (text[start - 1] === ":") continue;
        if (FILE_TOKEN_DENY.test(name)) continue;
        out.push({ start, end: start + name.length, text: name, path: name });
      }
      return out;
    },

    // 一段文本是不是「工作目录里的某个文件」：是就返回可用的路径，不是返回空串。
    // 判据偏保守：单个不含空格的 token + 已知扩展名，且不是 URL / 接口路径 / @成员。
    filePathFromText(text) {
      let raw = String(text || "").trim();
      if (!raw || raw.length > 200) return "";
      if (/[\s"'`<>|*？?]/.test(raw)) return "";
      if (/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)) return "";
      if (/^(mailto|data|javascript):/i.test(raw)) return "";
      if (raw.startsWith("/api/")) return "";
      raw = raw.replace(/[),;，。；、】）]+$/, "");
      raw = raw.replace(/:\d+(?:-\d+)?$/, "");   // 允许编译器式写法：a.js:12
      if (!raw) return "";
      const base = raw.split(/[\\/]/).pop() || "";
      if (!base || /[{}%$]/.test(base)) return "";
      return FILE_EXT_RE.test(base) ? raw : "";
    },

    // 捕获阶段的文件点击：命中 data-file-path 就打开右侧预览，并拦住向上冒泡
    onDocFileClick(e) {
      const target = e.target;
      const el = target && target.closest ? target.closest("[data-file-path]") : null;
      if (!el) return;
      const path = el.getAttribute("data-file-path") || "";
      if (!path) return;
      e.preventDefault();
      e.stopPropagation();
      this.openWorkspacePath(path);
    },

    fileBaseName(p) {
      const segs = String(p || "").split(/[\\/]/).filter(Boolean);
      return segs.length ? segs[segs.length - 1] : String(p || "");
    },

    // 产物清单里的文件名：点开即看。
    // 任务详情弹窗盖在最上面，先关掉它再打开预览，否则看着像「点了没反应」
    openArtifact(a) {
      const p = (a && (a.path || a)) || "";
      if (!p) return;
      if (this.showCardModal) this.showCardModal = false;
      if (this.showInspectModal) this.showInspectModal = false;
      this.openWorkspacePath(p);
    },

    // 点聊天里的文件名：解析成绝对路径后在右侧预览面板打开。
    // 相对路径按当前会话的工作目录解析；跑出工作目录的路径一律拒绝。
    async openWorkspacePath(rawPath) {
      let p = String(rawPath || "").trim();
      if (!p) return;
      if (/^file:\/\//i.test(p)) {
        const stripped = p.replace(/^file:\/\//i, "");
        try { p = decodeURIComponent(stripped); } catch (e) { p = stripped; }
      }
      const root = String((this.currentSession && this.currentSession.workspace_dir) || "").replace(/[/\\]+$/, "");
      const abs = this.normalizeFsPath(p.startsWith("/") ? p : (root ? `${root}/${p.replace(/^\.\//, "")}` : ""));
      if (!abs) { this.showToast(this.t("这个会话还没设置工作目录")); return; }
      // 绝对路径也可能指到工作目录之外：那种文件不该借这个入口被读出来
      const inRoot = !p.startsWith("/") || !root || abs === root || abs.startsWith(root + "/");
      if (!inRoot) { this.showToast(this.t("这个文件不在当前工作目录里")); return; }
      this.focusWorkspaceDir(abs);
      if (await this.openFilePreview({ name: this.fileBaseName(abs), path: abs, size: 0 }, { fromChat: true })) return;
      // 裸文本里文件名常和中文粘在一起（「参考亲子教育手册.md」）：到同级目录里
      // 按「文件名后缀」再找一次，唯一命中就用它
      const alt = await this.matchFileBySuffix(abs);
      if (alt) {
        this.focusWorkspaceDir(alt);
        const item = { name: this.fileBaseName(alt), path: alt, size: 0 };
        if (await this.openFilePreview(item, { fromChat: true })) return;
      }
      this.showToast(this.t("找不到这个文件") + "：" + this.fileBaseName(abs));
    },

    // 目录清单里找「被给定基名包含、且落在末尾」的真实文件：
    // 裸文本常把「参考」「见」这类词粘在文件名前面，直接按整串查是查不到的
    async matchFileBySuffix(abs) {
      const dir = String(abs || "").split("/").slice(0, -1).join("/") || "/";
      const base = (this.fileBaseName(abs) || "").toLowerCase();
      if (!base) return "";
      try {
        const res = await fetch(`/api/workspace/files?path=${encodeURIComponent(dir)}`);
        if (!res.ok) return "";
        const data = await res.json();
        const hits = (data.items || [])
          .filter(it => !it.is_dir && String(it.name || "").toLowerCase() && base.endsWith(String(it.name).toLowerCase()))
          .sort((a, b) => String(b.name).length - String(a.name).length);
        return hits.length ? hits[0].path : "";
      } catch (e) {
        return "";
      }
    },

    // 归一化 /a/./b/../c 这种路径（只做字符串层面，交给后端再校验一次）
    normalizeFsPath(p) {
      const raw = String(p || "");
      const isAbs = raw.startsWith("/");
      const out = [];
      raw.split(/[\\/]+/).forEach(seg => {
        if (!seg || seg === ".") return;
        if (seg === "..") { out.pop(); return; }
        out.push(seg);
      });
      return (isAbs ? "/" : "") + out.join("/");
    },

    // 从聊天里打开的文件：让右侧列表先切到它所在的目录，
    // 这样关掉预览看到的就是「这个文件所在的地方」，而不是原先停着的目录
    focusWorkspaceDir(absFile) {
      const root = String((this.currentSession && this.currentSession.workspace_dir) || "").replace(/[/\\]+$/, "");
      if (!root) return;
      const dir = String(absFile || "").split("/").slice(0, -1).join("/") || "/";
      if (!(dir === root || dir.startsWith(root + "/"))) return;
      if (this.workspaceCurrentDir === dir) return;
      const stack = [{ name: this.workspaceRootName(), path: root }];
      let cur = root;
      dir.slice(root.length).split("/").filter(Boolean).forEach(seg => {
        cur = `${cur}/${seg}`;
        stack.push({ name: seg, path: cur });
      });
      this.workspacePathStack = stack;
      this.workspaceCurrentDir = dir;
      this.loadWorkspaceFiles();
    },

    // ---- mermaid ----------------------------------------------------------
    // marked 把 ```mermaid 渲染成 <pre><code class="language-mermaid">，不处理就是一坨代码。
    // 库有 2.4MB，所以只在页面上真的出现图时才加载，别拖慢首屏。
    scheduleMermaid() {
      if (this._mermaidTimer) clearTimeout(this._mermaidTimer);
      this._mermaidTimer = setTimeout(() => { this.hydrateMermaid(); }, 150);
    },

    ensureMermaid() {
      if (window.mermaid) return Promise.resolve(window.mermaid);
      if (this._mermaidLoading) return this._mermaidLoading;
      this._mermaidLoading = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "/static/vendor/mermaid.min.js?v=11.4.1";
        script.async = true;
        script.onload = () => {
          try {
            window.mermaid.initialize({
              startOnLoad: false,
              theme: "neutral",
              securityLevel: "strict",
              fontFamily: "inherit",
              flowchart: { htmlLabels: false, useMaxWidth: true },
            });
          } catch (e) { /* 初始化异常留到 render 时暴露 */ }
          resolve(window.mermaid);
        };
        script.onerror = () => {
          this._mermaidLoading = null;
          reject(new Error("mermaid 加载失败"));
        };
        document.head.appendChild(script);
      });
      return this._mermaidLoading;
    },

    // 把页面里所有还没处理的 mermaid 代码块换成 SVG。
    // 失败时保留原代码并给一句说明——宁可看代码，也别给一块空白。
    async hydrateMermaid() {
      if (!document.querySelector("pre > code.language-mermaid")) return;
      let lib = null;
      try { lib = await this.ensureMermaid(); } catch (e) { lib = null; }

      const blocks = [...document.querySelectorAll("pre > code.language-mermaid")];
      for (let i = 0; i < blocks.length; i++) {
        const code = blocks[i];
        const pre = code.parentElement;
        if (!pre || pre.dataset.mermaidDone) continue;
        pre.dataset.mermaidDone = "1";
        if (!lib) {
          pre.insertAdjacentHTML(
            "afterend",
            `<div class="mermaid-error">${this.t("流程图组件没能加载，先按代码展示。")}</div>`
          );
          continue;
        }
        const src = (code.textContent || "").trim();
        if (!src) continue;
        const id = "mmd-" + Date.now().toString(36) + "-" + i;
        try {
          const out = await lib.render(id, src);
          const box = document.createElement("div");
          box.className = "mermaid-box";
          box.innerHTML = out.svg;
          pre.replaceWith(box);
        } catch (e) {
          // mermaid 出错时会在 body 里留一个临时节点，顺手清掉
          const junk = document.getElementById("d" + id) || document.getElementById(id);
          if (junk) junk.remove();
          pre.insertAdjacentHTML(
            "afterend",
            `<div class="mermaid-error">${this.t("流程图语法没能解析，已保留代码。")}</div>`
          );
        }
      }
    },

    // 删一条消息：服务端物理删除，build_context 下次就取不到它了。
    // 本地先按 id 屏蔽，避免轮询刚好返回旧数据时消息「闪回来」。
    async deleteMessage(msg) {
      if (!msg || !msg.id || !this.currentSessionId) return;
      if (!confirm(this.t("确定删除这条消息吗？删除后它不会再进入后续对话的上下文。"))) return;

      const sid = this.currentSessionId;
      const mid = msg.id;
      this.deletedMsgIds = Object.assign({}, this.deletedMsgIds, { [mid]: 1 });

      try {
        const res = await fetch(`/api/sessions/${sid}/messages/${mid}`, { method: "DELETE" });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          const next = Object.assign({}, this.deletedMsgIds);
          delete next[mid];
          this.deletedMsgIds = next;
          this.showToast(data.error || this.t("删除失败，请重试"));
          return;
        }
        const s = this.currentSession;
        if (s && Array.isArray(s.messages)) {
          s.messages = s.messages.filter(m => m.id !== mid);
        }
        this.showToast(this.t("已删除，后续对话不再带上这条"));
      } catch (e) {
        const next = Object.assign({}, this.deletedMsgIds);
        delete next[mid];
        this.deletedMsgIds = next;
        this.showToast(this.t("删除失败，请重试"));
      }
    },

    // 「模型原始回复」里存的是 CLI 的完整 stdout，正文后面常常还挂着一块
    // ```json { "summary": …, "artifacts": […] }``` 的产物登记。整段直接渲染会把登记
    // 铺成一个大代码块、正文被挤到下面，看起来就是「还是 md 源码」。
    // 这里按后端的同一套规则把登记块摘出来：正文照常渲染，登记单独折叠。
    // 注意：带参数，只能放 methods（放 computed 里 Vue 会当无参 getter 缓存）。
    splitRegistry(text) {
      const raw = String(text || "");
      if (!raw.trim()) return { body: "", registry: "" };
      const keys = ["artifacts", "changes", "summary", "opinion_handling"];
      const re = /```(?:json)?\s*(\{[\s\S]*?\})\s*```/gi;
      let m;
      while ((m = re.exec(raw)) !== null) {
        let obj = null;
        try { obj = JSON.parse(m[1]); } catch (e) { obj = null; }
        if (!obj || typeof obj !== "object") continue;
        if (!keys.some(k => k in obj)) continue;
        const body = (raw.slice(0, m.index) + raw.slice(m.index + m[0].length)).trim();
        return { body, registry: JSON.stringify(obj, null, 2) };
      }
      // 整段就是一份 JSON（老写法，或模型只回了一份结构）
      const whole = raw.trim();
      if (whole.startsWith("{") && whole.endsWith("}")) {
        try {
          const obj = JSON.parse(whole);
          if (obj && typeof obj === "object") {
            return { body: "", registry: JSON.stringify(obj, null, 2) };
          }
        } catch (e) { /* 不是 JSON，按正文处理 */ }
      }
      return { body: whole, registry: "" };
    },

    // 点引用块：原话还在列表里就滚过去并高亮；已经不在（被重试覆盖/清空）就弹快照预览
    jumpToQuote(quote) {
      if (!quote || !quote.id) return;
      const exists = (this.currentSession?.messages || []).some(m => m.id === quote.id);
      if (exists) {
        const root = this.$refs.chatContainer;
        const el = root && root.querySelector(`[data-msg-id="${quote.id}"]`);
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "center" });
          this.flashMsgId = quote.id;
          if (this._flashTimer) clearTimeout(this._flashTimer);
          this._flashTimer = setTimeout(() => { this.flashMsgId = ""; }, 1700);
          return;
        }
      }
      this.quotePreview = quote;
    },
    cancelReply() {
      this.replyTarget = null;
    },
    quoteExcerpt(text) {
      // 引用的是 Markdown 原文时，摘要里的 # / ** / 表格竖线只是噪音，去掉更好读
      const t = String(text || "")
        .replace(/```[\s\S]*?```/g, " ")
        .replace(/^[#>\-\*\s]+/gm, "")
        .replace(/\*\*|__|`/g, "")
        .replace(/\|/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      return t.length > 120 ? t.slice(0, 120) + "…" : t;
    },

    showToast(text) {
      if (!text) return;
      this.toastText = text;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { this.toastText = ""; }, 4000);
    },

    // ---------------- 服务状态显示 ----------------
    formatUptime(sec) {
      const s = Math.max(0, Math.floor(sec || 0));
      const d = Math.floor(s / 86400);
      const h = Math.floor((s % 86400) / 3600);
      const m = Math.floor((s % 3600) / 60);
      if (this.lang === "en") {
        if (d > 0) return `${d}d ${h}h`;
        if (h > 0) return `${h}h ${m}m`;
        if (m > 0) return `${m}m ${s % 60}s`;
        return `${s}s`;
      }
      if (d > 0) return `${d} 天 ${h} 小时`;
      if (h > 0) return `${h} 小时 ${m} 分`;
      if (m > 0) return `${m} 分 ${s % 60} 秒`;
      return `${s} 秒`;
    },

    async fetchSystemStatus() {
      try {
        const res = await fetch("/api/system/status", { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        this.systemStatus = await res.json();
        this.serviceDown = false;
        return true;
      } catch (e) {
        this.systemStatus = null;
        return false;
      }
    },

    async restartService() {
      if (this.serviceBusy) return;
      if (!confirm(
        this.t("确定重启服务？") + "\n\n" +
        this.t("进行中的任务会被立即终止并标记为已取消，重启后可在任务卡片里重试。")
      )) return;

      if (!this.systemStatus) await this.fetchSystemStatus();
      const oldStart = this.systemStatus?.started_at || 0;
      const oldPid = this.systemStatus?.pid || 0;

      this.serviceBusy = true;
      try {
        await fetch("/api/system/restart", {
          method: "POST",
          headers: { "X-Meeting-Action": "1" }
        });
      } catch (e) {
        // 连接被切断属于预期：服务正在退出
      }

      const ok = await this.waitForServer(oldStart, oldPid);
      this.serviceBusy = false;
      if (ok) {
        location.reload();
      } else {
        alert(this.t("重启后 20 秒内没能连上服务。请查看终端输出，或手动执行 ./run.sh。"));
        this.serviceDown = true;
      }
    },

    async shutdownService() {
      if (this.serviceBusy) return;
      if (!confirm(
        this.t("确定停止服务？") + "\n\n" +
        this.t("所有在跑的任务会立即终止并标记为已取消。停止后需重新运行 ./run.sh 才能再次使用。")
      )) return;

      this.serviceBusy = true;
      try {
        await fetch("/api/system/shutdown", {
          method: "POST",
          headers: { "X-Meeting-Action": "1" }
        });
      } catch (e) {
        // 同上
      }
      // 等进程真正退出，避免界面还显示“正在连接”
      for (let i = 0; i < 20; i++) {
        await new Promise(r => setTimeout(r, 300));
        if (!(await this.fetchSystemStatus())) break;
      }
      this.serviceBusy = false;
      if (!this.systemStatus) {
        this.serviceDown = true;
      } else {
        alert(this.t("停止指令已发出，但服务仍在响应。请查看终端是否有异常。"));
      }
    },

    async waitForServer(oldStart, oldPid) {
      let sawDown = false;
      for (let i = 0; i < 40; i++) {
        await new Promise(r => setTimeout(r, 500));
        try {
          const res = await fetch("/api/system/status", { cache: "no-store" });
          if (!res.ok) {
            sawDown = true;
            continue;
          }
          const st = await res.json();
          if (oldStart && st.started_at !== oldStart) return true;
          if (oldPid && st.pid !== oldPid) return true;
          // 拿不到旧进程身份时，至少要确认服务真的断过一次，否则可能只是还没退出
          if (!oldStart && !oldPid && sawDown) return true;
        } catch (e) {
          sawDown = true;
        }
      }
      return false;
    },

    async retryConnect() {
      this.serviceBusy = true;
      const ok = await this.fetchSystemStatus();
      this.serviceBusy = false;
      if (ok) location.reload();
    },

        // 会话签名：内容没变就不整体替换 currentSession，避免每次轮询全量重渲染
    sessionSignature(s) {
      if (!s) return "";
      const msgs = s.messages || [];
      const last = msgs[msgs.length - 1] || {};
      const taskSig = Object.values(s.tasks || {})
        .map(t => `${t.task_id}:${t.status}:${t.step}:${(t.flow || []).length}`).join(",");
      const resultSig = msgs.filter(m => m.kind === "task_result")
        .map(m => `${m.id}:${m.status}`).join(",");
      // 卡片签名要含各环节状态：审核陆续进入「进行中」时卡片与流水都可能不变，
      // 漏掉这两项，界面就会在整个审核期间显示「等待」，直到完成才跳到「已完成」
      const stageSig = (card) => {
        if (!card) return "";
        const a = card.part_a || {};
        const b = (card.part_b || []).map(i => `${i.member_id}=${i.status}`).join("/");
        return `${a.status}>${a.final_status}>${b}`;
      };
      const cardSig = msgs.filter(m => m.kind === "task_card")
        .map(m => `${m.id}:${(m.card && m.card.status) || ""}:${((m.card && m.card.flow) || []).length}:${stageSig(m.card)}`)
        .join(",");
      const queueSig = Object.entries(s.task_queues || {})
        .map(([k, v]) => `${k}:${(v || []).length}`).join(",");
      return [taskSig, resultSig, cardSig, queueSig,
        msgs.length, last.id, (last.content || "").length].join("|");
    },

    async pollActiveSession() {
      const targetId = this.currentSessionId;
      if (!targetId) return;
      // 有活动任务时才轮询，空闲会话不发无谓请求
      if (!this.hasActiveTasks && !this.showCardModal) return;
      try {
        const res = await fetch(`/api/sessions/${targetId}`);
        if (!res.ok) return;
        const latest = await res.json();
        // 防止在途请求返回时用户已切换会话导致数据覆盖
        if (latest.id !== this.currentSessionId) return;
        if (this.sessionSignature(latest) === this.sessionSignature(this.currentSession)) return;
        const prevCount = (this.currentSession.messages || []).length;
        const nextCount = (latest.messages || []).length;
        this.currentSession = latest;
        if (this.showCardModal && this.cardModalMsg) {
          const updatedCard = (latest.messages || []).find(m => m.id === this.cardModalMsg.id);
          if (updatedCard) this.cardModalMsg = updatedCard;
        }
        if (nextCount > prevCount) {
          // 用户正在往上翻历史时不抢位置（角落有「回到最新」提示）
          this.$nextTick(() => this.scrollToBottom(false));
        }
        // 消息条数没变、但某条消息的正文变了（单任务从空内容变成整段回答）同样会改变高度。
        // 只在条数增加时量，长回答就会被限高切掉却没有「展开全文」按钮——正是用户看到的
        // 「内容挤下去了、又没法往下翻」。
        this.scheduleMsgOverflowMeasure();
      } catch (e) {
        // 静默容错
      }
    },

    // 拉「正在生成的文本」。只在有任务在跑时请求，空闲会话零开销。
    async pollLiveStreams() {
      const sid = this.currentSessionId;
      if (!sid) return;
      if (!this.hasActiveTasks) {
        // 没有任务在跑了：清掉残留，避免旧文本一直挂在界面上
        if (Object.keys(this.liveStreams).length) this.liveStreams = {};
        return;
      }
      try {
        const res = await fetch(`/api/sessions/${sid}/live`);
        if (!res.ok) return;
        const data = await res.json();
        if (sid !== this.currentSessionId) return;
        const streams = data.streams || {};
        let grew = false;
        for (const key of Object.keys(streams)) {
          const item = streams[key];
          if (!item || !item.text) continue;
          const prev = this.liveStreams[key];
          if (!prev || (item.chars || 0) > (prev.chars || 0)) grew = true;
          this.liveLast[key] = { text: item.text, phase: item.phase, activity: item.activity, chars: item.chars };
        }
        // 只留最近 2 分钟的兜底文本，防止长会话里越攒越多
        const cutoff = Date.now() - 120000;
        for (const key of Object.keys(this.liveLast)) {
          if (!streams[key] && (this.liveLast[key].at || 0) < cutoff) delete this.liveLast[key];
          else if (streams[key]) this.liveLast[key].at = Date.now();
        }
        this.liveStreams = streams;
        if (grew && this.stickToBottom) this.$nextTick(() => this.scrollToBottom(false));
      } catch (e) {
        // 静默容错：拿不到就继续显示上一份
      }
    },

    insertMention(name) {      this.suggest = null;
      this.inputContent = (this.inputContent ? this.inputContent + " " : "") + `@${name} `;
      this.$nextTick(() => {
        const textarea = this.$refs.chatInput;
        if (textarea) textarea.focus();
      });
    },

    getEmployeeColor(senderId) {
      if (senderId === "system") return "#f59e0b";
      const emp = (this.settings.employees || []).find(e => e.id === senderId);
      return emp?.avatar_color || "#3b82f6";
    },

    getEmployeeAvatar(senderId) {
      if (senderId === "system") return "📢";
      const emp = (this.settings.employees || []).find(e => e.id === senderId);
      return emp?.avatar || "🤖";
    },

    // ----------------- 工作目录文件浏览器 -----------------
    // 把当前会话根目录作为面包屑起点，并加载根目录内容。
    // 会话切换 / 改配置后调用，确保从根目录重新开始，不会停留在上一个会话的目录里。
    resetWorkspaceDir() {
      const root = this.currentSession?.workspace_dir || "";
      this.workspaceCurrentDir = root;
      this.workspacePathStack = root ? [{ name: this.workspaceRootName(), path: root }] : [];
    },

    // 根目录在面包屑里显示的友好名字（取路径最后一段）
    workspaceRootName() {
      const root = (this.currentSession?.workspace_dir || "").replace(/[/\\]+$/, "");
      const segs = root.split(/[/\\]+/).filter(Boolean);
      return segs.length ? segs[segs.length - 1] : this.t("未指定目录");
    },

    async loadWorkspaceFiles() {
      const dir = this.workspaceCurrentDir || this.currentSession?.workspace_dir;
      if (!dir) return;
      try {
        const res = await fetch(`/api/workspace/files?path=${encodeURIComponent(dir)}`);
        const data = await res.json();
        this.workspaceItems = data.items || [];
      } catch (e) {
        console.error("获取工作区文件失败:", e);
      }
    },

    async onWorkspaceItemClick(item) {
      if (item.is_dir) {
        // 就地进入子目录：把这一级压进面包屑栈，列表就地替换为子目录内容，
        // 不弹新窗口；之后点面包屑或「↑」即可返回原先预览的目录
        this.workspaceCurrentDir = item.path;
        this.workspacePathStack = this.workspacePathStack.concat([{ name: item.name, path: item.path }]);
        try {
          const res = await fetch(`/api/workspace/files?path=${encodeURIComponent(item.path)}`);
          const data = await res.json();
          this.workspaceItems = data.items || [];
        } catch (e) {
          console.error("展开目录失败:", e);
        }
      } else {
        this.selectedWorkspaceFile = item;
        await this.openFilePreview(item);
      }
    },

    // 点面包屑某一段：截断到该段并重新加载，实现「跳回任意上级目录」
    async gotoCrumb(index) {
      if (index < 0 || index >= this.workspacePathStack.length) return;
      this.workspacePathStack = this.workspacePathStack.slice(0, index + 1);
      const crumb = this.workspacePathStack[this.workspacePathStack.length - 1];
      this.workspaceCurrentDir = crumb.path;
      await this.loadWorkspaceFiles();
    },

    // 返回上一级目录（栈顶弹出一段）
    async goUpWorkspace() {
      if (this.workspacePathStack.length <= 1) return;
      await this.gotoCrumb(this.workspacePathStack.length - 2);
    },

    // 按扩展名决定怎么预览：md 渲染成文档，图片直接显示，其余按等宽文本，
    // 压缩包 / Office 这类二进制给一句话说明而不是把乱码铺出来
    filePreviewKind(item) {
      const name = String((item && item.name) || "").toLowerCase();
      const ext = name.includes(".") ? name.slice(name.lastIndexOf(".")) : "";
      if ([".md", ".markdown", ".mdx"].includes(ext)) return "markdown";
      if ([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"].includes(ext)) return "image";
      if ([
        ".pdf", ".zip", ".gz", ".tar", ".tgz", ".7z", ".rar",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".mp3", ".mp4", ".mov", ".wav", ".dmg", ".pkg", ".exe",
      ].includes(ext)) return "binary";
      return "text";
    },

    // 打开文件预览。返回 false 表示「文件不在」（调用方决定怎么提示）。
    async openFilePreview(item, opts = {}) {
      if (!item) return false;
      const kind = this.filePreviewKind(item);
      const fromChat = !!opts.fromChat;
      // 从聊天里点进来的路径可能早就被改名 / 删掉了：先确认文件在，
      // 不在就交给调用方按后缀再找一次，别把预览面板刷成「读取失败」
      if (fromChat && kind !== "text" && kind !== "markdown") {
        const ok = await this.workspaceFileExists(item.path);
        if (!ok) return false;
      }
      this.showFilePreview = true;
      this.filePreviewLoading = kind === "text" || kind === "markdown";
      // 换文件时先把编辑态清干净，避免上一条的草稿被带到新文件上
      this.previewEditing = false;
      this.previewDraft = "";
      this.previewDirty = false;
      this.previewUpdatedAt = 0;
      this.filePreview = {
        name: item.name, path: item.path, size: item.size || 0, kind,
        mtime: 0, content: "", url: "", error: "", updatedAt: 0,
      };
      if (kind === "image") {
        // 带时间戳：AI 重画了同名图片时，浏览器不会拿缓存里的旧图
        this.filePreview.url = `/api/workspace/raw?path=${encodeURIComponent(item.path)}&t=${Date.now()}`;
        this.filePreviewLoading = false;
        return true;
      }
      if (kind === "binary") {
        this.filePreviewLoading = false;
        this.filePreview.error = this.t("这一类文件不支持在线预览，可以复制路径后在本机打开");
        return true;
      }
      try {
        const res = await fetch(`/api/workspace/file?path=${encodeURIComponent(item.path)}`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          if (fromChat) {
            this.closeFilePreview();
            return false;
          }
          throw new Error(data.error || res.status);
        }
        // 连点两个文件时，先返回的那个不能盖掉后点的
        if (!this.filePreview || this.filePreview.path !== item.path) return true;
        if (data.too_large) {
          this.filePreview.error = data.error || this.t("文件过大，暂不支持在线预览");
          this.filePreview.too_large = true;
        } else {
          this.filePreview.content = data.content || "";
          this.filePreview.mtime = Number(data.mtime) || 0;
          this.filePreview.size = data.size || this.filePreview.size;
          this.selectedFileContent = this.filePreview.content;
        }
      } catch (e) {
        if (this.filePreview && this.filePreview.path === item.path) {
          this.filePreview.error = this.t("读取文件失败") + "：" + (e.message || e);
        }
      } finally {
        if (this.filePreview && this.filePreview.path === item.path) this.filePreviewLoading = false;
      }
      return true;
    },

    closeFilePreview() {
      this.showFilePreview = false;
      this.filePreview = null;
      this.filePreviewLoading = false;
      this.previewEditing = false;
      this.previewDraft = "";
      this.previewDirty = false;
      this.previewSaving = false;
    },

    // ----------------- 预览面板：就地编辑 + 保存 -----------------
    previewExt() {
      const name = String((this.filePreview && this.filePreview.name) || "").toLowerCase();
      return name.includes(".") ? name.slice(name.lastIndexOf(".") + 1) : "";
    },
    canEditPreview() {
      const pv = this.filePreview;
      if (!pv) return false;
      if (pv.kind !== "text" && pv.kind !== "markdown") return false;
      if (pv.error) return false;
      return EDITABLE_EXTS.has(this.previewExt());
    },
    startPreviewEdit() {
      if (!this.canEditPreview()) return;
      this.previewDraft = (this.filePreview && this.filePreview.content) || "";
      this.previewDirty = false;
      this.previewEditing = true;
      this.$nextTick(() => {
        const el = this.$refs.previewEditor;
        if (el) {
          el.focus();
          // 光标放到末尾：多半是「接着补两句」，不是从头改
          el.selectionStart = el.selectionEnd = el.value.length;
        }
      });
    },
    onPreviewDraftInput() {
      const pv = this.filePreview;
      this.previewDirty = !!(pv && this.previewDraft !== (pv.content || ""));
    },
    cancelPreviewEdit() {
      if (this.previewSaving) return;
      if (this.previewDirty && !confirm(this.t("放弃未保存的修改？"))) return;
      this.previewEditing = false;
      this.previewDraft = "";
      this.previewDirty = false;
    },
    // force=true 用于「文件已被别人改写，仍然覆盖」的二次确认
    async savePreviewFile(force = false) {
      const pv = this.filePreview;
      if (!pv || this.previewSaving) return;
      this.previewSaving = true;
      try {
        const res = await fetch("/api/workspace/file", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            path: pv.path,
            content: this.previewDraft,
            base_mtime: pv.mtime || 0,
            force: !!force,
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 409) {
          // AI 或别的编辑器在这段时间里写过这个文件：让人自己决定要不要盖掉
          const ok = confirm(
            this.t("这个文件刚被改写过（可能是 AI 正在写）。仍要用你编辑的版本覆盖吗？")
          );
          if (ok) return this.savePreviewFile(true);
          // 放弃覆盖：把磁盘上的新版读回来，别让人以为自己的草稿才是最新的
          await this.reloadPreviewContent(true);
          return;
        }
        if (!res.ok) {
          this.showToast(data.error || this.t("保存失败（HTTP {status}）", { status: res.status }));
          return;
        }
        if (!this.filePreview || this.filePreview.path !== pv.path) return;
        this.filePreview.content = this.previewDraft;
        this.filePreview.mtime = Number(data.mtime) || this.filePreview.mtime;
        this.filePreview.size = data.size || this.filePreview.size;
        this.selectedFileContent = this.filePreview.content;
        this.previewEditing = false;
        this.previewDirty = false;
        this.showToast(this.t("已保存"));
        this.loadWorkspaceFiles();
      } catch (e) {
        this.showToast(this.t("保存失败：") + (e.message || e));
      } finally {
        this.previewSaving = false;
      }
    },

    // ----------------- 预览面板：被外部改写时自动刷新 -----------------
    // 一直是开着的（顶部不再给开关，免得按钮堆一排）：正文可能被 AI 正在写，
    // 先看 mtime 有没有变，变了才读全文，免得 1.2s 一次把几百 KB 的文件反复读进内存。
    async pollPreviewFile() {
      const pv = this.filePreview;
      if (!pv || pv.error || this.previewEditing) return;
      if (pv.kind !== "text" && pv.kind !== "markdown" && pv.kind !== "image") return;
      if (pv.too_large) return;
      try {
        const res = await fetch(`/api/workspace/file?meta=1&path=${encodeURIComponent(pv.path)}`);
        if (!res.ok) return;
        const meta = await res.json();
        // 期间可能已经切到别的文件 / 关了面板
        if (!this.filePreview || this.filePreview.path !== pv.path) return;
        // 0.5ms 容差：mtime 是浮点秒，不同文件系统精度略有差异
        if (!(Number(meta.mtime) > (Number(pv.mtime) || 0) + 0.0005)) return;
        if (pv.kind === "image") {
          pv.mtime = Number(meta.mtime) || pv.mtime;
          pv.size = meta.size || pv.size;
          pv.url = `/api/workspace/raw?path=${encodeURIComponent(pv.path)}&t=${Math.round(meta.mtime * 1000)}`;
          this.previewUpdatedAt = Date.now();
          return;
        }
        await this.reloadPreviewContent(true);
        this.previewUpdatedAt = Date.now();
      } catch (e) {
        // 静默容错：这一轮拿不到就等下一轮
      }
    },
    // 重新读取正文（保留滚动位置）。silent=true 时不显示「正在读取」占位，
    // 避免 AI 每写一次面板就闪一下。
    async reloadPreviewContent(silent = false) {
      const pv = this.filePreview;
      if (!pv) return;
      const body = this.$refs.previewBody;
      const keepTop = body ? body.scrollTop : 0;
      if (!silent) this.filePreviewLoading = true;
      try {
        const res = await fetch(`/api/workspace/file?path=${encodeURIComponent(pv.path)}`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) return;
        if (!this.filePreview || this.filePreview.path !== pv.path) return;
        if (data.too_large) {
          this.filePreview.error = data.error || this.t("文件过大，暂不支持在线预览");
          return;
        }
        this.filePreview.content = data.content || "";
        this.filePreview.mtime = Number(data.mtime) || this.filePreview.mtime;
        this.filePreview.size = data.size || this.filePreview.size;
        this.filePreview.updatedAt = Date.now();
        this.selectedFileContent = this.filePreview.content;
        if (silent) {
          this.$nextTick(() => {
            if (body && this.$refs.previewBody === body) body.scrollTop = keepTop;
          });
        }
      } catch (e) {
        // 忽略：文件可能正好被改名 / 删除，下一次轮询会自然停在这里
      } finally {
        if (!silent) this.filePreviewLoading = false;
      }
    },
    // 头部那句「几秒前更新」：只在真的刷新过时才显示，避免长期占位
    previewUpdatedLabel() {
      if (!this.previewUpdatedAt) return "";
      const sec = Math.max(0, Math.round((this.nowTick - this.previewUpdatedAt) / 1000));
      if (sec < 3) return this.t("刚刚已更新");
      if (sec < 60) return this.t("{n} 秒前已更新", { n: sec });
      return this.t("{n} 分钟前已更新", { n: Math.floor(sec / 60) });
    },
    // 文件是否存在：用读内容接口探一下（后端对不存在直接 404）
    async workspaceFileExists(p) {
      if (!p) return false;
      try {
        const res = await fetch(`/api/workspace/file?path=${encodeURIComponent(p)}`, { method: "GET" });
        return res.ok;
      } catch (e) {
        return false;
      }
    },

    // 文件预览顶部「全屏」：只把右侧预览面板本身请求全屏（不是整页），
    // 退出全屏或关掉预览即回到面板内视图
    togglePreviewFullscreen() {
      const el = this.$refs.previewPanel;
      if (!el) return;
      try {
        if (document.fullscreenElement) {
          document.exitFullscreen();
        } else if (el.requestFullscreen) {
          el.requestFullscreen();
        } else {
          this.showToast(this.t("当前浏览器不支持全屏"));
        }
      } catch (e) {
        this.showToast(this.t("当前浏览器不支持全屏"));
      }
    },

    // 预览正文：md 走和聊天同一套 renderMdText（marked + DOMPurify），
    // 纯文本先转义再塞进 <pre>，文件里的 HTML 不会被当标签执行
    filePreviewHtml() {
      const pv = this.filePreview;
      if (!pv || !pv.content) return "";
      if (pv.kind === "markdown") {
        return `<div class="markdown-body">${this.renderMdText(pv.content)}</div>`;
      }
      return `<pre class="text-xs font-mono whitespace-pre-wrap break-words">${this.escapeHtml(pv.content)}</pre>`;
    },

    // md 预览里的链接：不在当前页跳走（会把整个工作台刷掉），外链开新标签，
    // 相对路径多半指向文件旁边的资源，应用里没有对应路由，给一句提示就好
    onPreviewClick(e) {
      const a = e.target && e.target.closest ? e.target.closest("a") : null;
      if (!a) return;
      const href = a.getAttribute("href") || "";
      e.preventDefault();
      if (/^https?:\/\//i.test(href)) {
        window.open(href, "_blank", "noopener,noreferrer");
      } else if (href && !href.startsWith("#")) {
        this.showToast(this.t("相对链接请在本机打开"));
      }
    },

    async copyFilePath() {
      const path = (this.filePreview && this.filePreview.path) || "";
      if (!path) return;
      try {
        await navigator.clipboard.writeText(path);
        this.showToast(this.t("路径已复制"));
      } catch (e) {
        // 非安全上下文（http + 非 localhost）拿不到剪贴板权限，退化成把路径显示出来
        this.showToast(path);
      }
    },

    formatFileSize(bytes) {
      if (!bytes || bytes === 0) return "0 B";
      const k = 1024;
      const sizes = ["B", "KB", "MB", "GB"];
      const i = Math.floor(Math.log(bytes) / Math.log(k));
      return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
    },

    // ----------------- 消息 Prompt 审查详情 (ℹ️) -----------------
    openInspectModal(msg) {
      this.inspectingMsg = msg;
      this.inspectTab = "prompt";
      this.inspectRaw = false;   // 每次打开都先给渲染后的版本
      this.promptOpen = {};   // 每次打开都回到默认视图（大块材料折叠）
      this.showInspectModal = true;
    },

    promptSectionOpen(sec) {
      const v = this.promptOpen[sec.key];
      return v === undefined ? sec.defaultOpen : v;
    },
    togglePromptSection(sec) {
      this.promptOpen = { ...this.promptOpen, [sec.key]: !this.promptSectionOpen(sec) };
    },
    promptAllExpanded() {
      return this.promptSections.length > 0 && this.promptSections.every(s => this.promptSectionOpen(s));
    },
    toggleAllPromptSections() {
      // 只要还有折叠着的段，这个按钮就是「展开全部」；全展开时才变成「收起全部」
      const expand = !this.promptAllExpanded();
      const next = {};
      for (const s of this.promptSections) next[s.key] = expand;
      this.promptOpen = next;
    },

    async copyText(text) {
      if (!text) return;
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
          this.showToast(this.t("已复制到剪贴板"));
        } else {
          const ta = document.createElement("textarea");
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
          this.showToast(this.t("已复制到剪贴板"));
        }
      } catch (e) {
        console.warn("复制失败:", e);
      }
    },

    // ----------------- 成员配置 -----------------
    async triggerScanModels(force = true) {
      try {
        const res = await fetch(`/api/scan_models?refresh=${force ? 1 : 0}`);
        const data = await res.json();
        this.scannedModels = data.models || [];
      } catch (e) {
        console.error("扫描模型失败:", e);
      }
    },

    modelKey(m) {
      return m.key || `${m.cli}:${m.model_id}`;
    },

    // 老配置只存了 cli_cmd：按任一推理档位的命令反查，编辑时也能回显模型/档位
    findModelByCli(cmd) {
      if (!cmd) return null;
      for (const m of (this.scannedModels || [])) {
        for (const lv of (m.reasoning_levels || [])) {
          if (lv.cli_cmd === cmd) return { model: m, level: lv };
        }
        if (m.cli_cmd === cmd) return { model: m, level: null };
      }
      return null;
    },

    toggleModelDropdown(ev) {
      if (this.modelDropdownOpen) {
        this.modelDropdownOpen = false;
        return;
      }
      const btn = (ev && ev.currentTarget)
        || (ev && ev.target && ev.target.closest && ev.target.closest(".model-trigger"));
      const r = (btn && btn.getBoundingClientRect)
        ? btn.getBoundingClientRect()
        : { left: 40, bottom: 120, width: 420 };
      const width = Math.max(r.width || 420, 340);
      const left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8));
      const top = Math.max(8, Math.min(r.bottom + 6, window.innerHeight - 340));
      this.modelDropdownStyle = {
        position: "fixed",
        left: `${left}px`,
        top: `${top}px`,
        width: `${width}px`,
        zIndex: 200
      };
      this.modelSearch = "";
      this.modelDropdownOpen = true;
    },

    closeModelDropdown() {
      this.modelDropdownOpen = false;
    },

    // 选模型：记下 model_key，推理程度自动落到该模型的默认档（通常是高）
    selectScannedModel(m) {
      this.selectedModelId = this.modelKey(m);
      this.editingEmp.model_key = this.modelKey(m);
      const levels = m.reasoning_levels || [];
      const def = levels.find(l => l.id === (m.default_reasoning || "high")) || levels[0];
      this.applyReasoning(def, m);
      this.modelDropdownOpen = false;
    },

    selectReasoning(lv) {
      this.applyReasoning(lv, this.selectedModelObj);
    },

    applyReasoning(lv, m) {
      if (!lv) return;
      this.editingEmp.reasoning = lv.id;
      this.editingEmp.cli_cmd = lv.cli_cmd;
      if (!this.editingEmp.name && m) {
        this.editingEmp.name = (m.display_name || "").replace(/\[.*?\]\s*/g, "");
      }
      this.syncEditingEmpCli();
    },

    // 兼容旧入口（原生 select 已移除）：按 selectedModelId 重新应用一次
    onModelSelectChanged() {
      if (this.selectedModelObj) this.selectScannedModel(this.selectedModelObj);
    },

    openAddEmployeeModal() {
      this.isEditingEmployee = false;
      this.editingEmpIdx = -1;
      this.selectedModelId = "";
      this.modelSearch = "";
      this.modelDropdownOpen = false;
      // 只保留「只看免费」这个偏好，搜索词每次打开都清掉

      const uniqueId = "emp_" + Date.now().toString(36) + "_" + Math.random().toString(36).substring(2, 6);

      this.editingEmp = {
        id: uniqueId,
        name: "",
        avatar: "🤖",
        avatar_color: "#3b82f6",
        type: "cli",
        cli_cmd: "",
        model_key: "",
        reasoning: "",
        system_prompt: "",
        enabled: true,
        timeout: 0,
        // 新建成员默认「读 + 写」都开：只勾读不足以做实际交付，
        // 两个都不勾则连产物都读不到（只会回一句「我先读取一下」）。
        write_access: true,
        read_access: true
      };
      this.showEmpEditModal = true;
    },

    openEditEmployeeModal(emp, idx) {
      this.isEditingEmployee = true;
      this.editingEmpIdx = idx;
      this.editingEmp = JSON.parse(JSON.stringify(emp));
      // 老配置只有 write_access，没有 read_access：按「有读权限」补齐
      if (this.editingEmp.read_access === undefined) this.editingEmp.read_access = true;
      if (this.editingEmp.write_access === undefined) this.editingEmp.write_access = false;
      this.editingEmp.model_key = this.editingEmp.model_key || "";
      this.editingEmp.reasoning = this.editingEmp.reasoning || "";
      this.modelSearch = "";
      this.modelDropdownOpen = false;

      // 先按已保存的 model_key 回显；老配置没这个字段时按 cli_cmd 反查档位
      let model = (this.scannedModels || [])
        .find(m => this.modelKey(m) === this.editingEmp.model_key) || null;
      let level = null;
      if (!model) {
        const hit = this.findModelByCli(emp.cli_cmd);
        model = hit ? hit.model : null;
        level = hit ? hit.level : null;
      } else {
        level = (model.reasoning_levels || [])
          .find(l => l.id === this.editingEmp.reasoning) || null;
      }
      this.selectedModelId = model ? this.modelKey(model) : "";
      if (model) {
        this.editingEmp.model_key = this.modelKey(model);
        const def = level
          || (model.reasoning_levels || []).find(l => l.id === model.default_reasoning)
          || (model.reasoning_levels || [])[0];
        this.editingEmp.reasoning = def ? def.id : "";
        // 收费模型别被「只看免费」藏起来
        if (!model.is_free) this.onlyFreeModels = false;
      }
      this.syncEditingEmpCli();
      this.showEmpEditModal = true;
    },

    // 勾选读/写权限后，把模板里的 --tools 同步成实际会执行的值，
    // 避免「界面上写 --tools ""、实际又补了工具」这种看不懂的状态。
    // 服务端保存时还会按权限再规范化一次（core.executor.normalize_cli_tools）。
    onAccessToggle(field, ev) {
      this.editingEmp[field] = !!(ev && ev.target ? ev.target.checked : this.editingEmp[field]);
      this.syncEditingEmpCli();
    },

    syncEditingEmpCli() {
      const cmd = this.editingEmp.cli_cmd || "";
      if (!/\bcodebuddy\b/.test(cmd)) return;
      const re = /--tools\s+("[^"]*"|'[^']*'|\S+)/;
      const m = cmd.match(re);
      if (!m) return;
      const cur = m[1].replace(/["']/g, "").trim();
      if (["default", "all"].includes(cur.toLowerCase())) return;

      const READ = ["Read", "Glob", "Grep"];
      const WRITE = ["Write", "Edit", "Bash"];
      const read = this.editingEmp.read_access !== false;
      const write = !!this.editingEmp.write_access;
      let tools = cur ? cur.split(",").map(s => s.trim()).filter(Boolean) : [];
      if (!read) tools = tools.filter(t => !READ.includes(t));
      if (!write) tools = tools.filter(t => !WRITE.includes(t));
      const lower = new Set(tools.map(t => t.toLowerCase()));
      [...(read ? READ : []), ...(write ? WRITE : [])].forEach(t => {
        if (!lower.has(t.toLowerCase())) { tools.push(t); lower.add(t.toLowerCase()); }
      });
      this.editingEmp.cli_cmd = cmd.replace(re, `--tools "${tools.join(",")}"`);
    },

    async saveEmployeeItem() {
      if (!this.editingEmp.name.trim()) {
        alert(this.t("请输入成员姓名！"));
        return;
      }
      if (!this.editingEmp.cli_cmd.trim()) {
        alert(this.t("CLI 执行模板不能为空！"));
        return;
      }

      // 先在副本上修改，保存成功后再落到 this.settings，避免失败时本地已被改脏
      const next = {
        ui_language: this.settings.ui_language,
        employees: JSON.parse(JSON.stringify(this.settings.employees || []))
      };

      if (this.isEditingEmployee && this.editingEmpIdx >= 0) {
        next.employees[this.editingEmpIdx] = JSON.parse(JSON.stringify(this.editingEmp));
      } else {
        next.employees.push(JSON.parse(JSON.stringify(this.editingEmp)));
      }

      const saved = await this.saveSettingsToServer(next, false);
      if (saved) {
        this.showEmpEditModal = false;
      }
    },

    async deleteEmployee(idx) {
      const targetEmp = this.settings.employees[idx];
      if (confirm(this.t("确定要删除成员 [{name}] 吗？", { name: targetEmp.name }))) {
        this.settings.employees.splice(idx, 1);
        const saved = await this.saveSettingsToServer(null, false);
        if (saved && this.currentSession?.employee_ids) {
          this.currentSession.employee_ids = this.currentSession.employee_ids.filter(id => id !== targetEmp.id);
          await this.saveSessionEmployees();
        }
      }
    },

    async saveSettingsToServer(nextSettings, closeMainModal = true) {
      try {
        // 界面语言与成员列表随整份配置一起提交，后端会做白名单校验
        const payload = nextSettings || this.settings;
        if (payload && !payload.ui_language) payload.ui_language = this.lang;
        const res = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          // 后端的 schema / 白名单校验失败原因必须回显，否则用户只会看到「什么都没发生」
          alert(this.t("保存配置失败:") + " " + (data.error || `HTTP ${res.status}`));
          return false;
        }
        this.settings = data.settings || this.settings;
        if (data.settings && data.settings.ui_language) {
          this.lang = data.settings.ui_language === "en" ? "en" : "zh";
          this.applyLanguage();
        }
        this.showEmpEditModal = false;
        if (closeMainModal) this.showToast(this.t("配置已保存。"));
        return true;
      } catch (e) {
        alert(this.t("保存配置失败:") + " " + e);
        return false;
      }
    },

    // 用户是否停在底部：只由用户自己的滚动行为判定，程序滚动不会把它关掉
    onChatScroll() {
      const c = this.$refs.chatContainer;
      if (!c) return;
      const dist = c.scrollHeight - c.scrollTop - c.clientHeight;
      this.stickToBottom = dist < 80;
      this.showJumpToLatest = dist > 240;
    },

    // follow=false 时只在「用户本来就在底部」才滚，别抢走别人正在读的位置
    scrollToBottom(follow = true) {
      const c = this.$refs.chatContainer;
      if (!c) return;
      if (!follow && !this.stickToBottom) return;
      c.scrollTop = c.scrollHeight;
      if (follow) this.showJumpToLatest = false;
    },

    // 强制贴底并恢复跟随：发消息、切会话、开始回答时用
    pinToBottom() {
      this.stickToBottom = true;
      this.showJumpToLatest = false;
      this.$nextTick(() => {
        this.scrollToBottom();
        // 字体/图片/代码块可能晚一帧才定高，再补一次，避免停在「差一点点」的位置
        if (typeof requestAnimationFrame === "function") {
          requestAnimationFrame(() => this.scrollToBottom());
        }
      });
    }
  }
}).mount("#app");
