const { createApp } = Vue;

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
      // 引用回复：{ id, sender, sender_name, content }，发送时把 id 交给后端定向
      replyTarget: null,
      // 长消息展开状态（按消息 id 记，轮询刷新不会把用户展开的又收回去）
      expandedMsgs: {},
      // 哪些消息真的被限高截断了（渲染后实测，决定是否给「展开全文」按钮）
      overflowMsgs: {},
      workspaceItems: [],
      selectedWorkspaceFile: null,
      selectedFileContent: "",

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
      settingsTab: "general",
      settingsTabs: [
        { key: "general", icon: "🎛️", label: "界面语言" },
        { key: "members", icon: "👥", label: "成员管理" },
        { key: "service", icon: "⏻", label: "服务进程" }
      ],

      // 成员配置弹窗
      showEmpEditModal: false,
      isEditingEmployee: false,
      editingEmpIdx: -1,
      selectedModelId: "",
      scannedModels: [],
      editingEmp: {
        id: "",
        name: "",
        avatar: "🤖",
        avatar_color: "#3b82f6",
        type: "cli",
        cli_cmd: "",
        system_prompt: "",
        enabled: true,
        timeout: 0,
        write_access: false
      },
      presetAvatars: ["🤖", "🧠", "⚡", "🦉", "🛡️", "🚀", "💻", "🔬", "🎯", "🎨", "👑", "🕵️", "🔧", "💡", "⚖️", "📊"],

      // 协助任务卡片弹窗（两部分 + 各环节取消 / 重试）
      showCardModal: false,
      cardModalMsg: null,
      nowTick: Date.now(),   // 卡片外壳的「实时耗时」由每秒心跳驱动
      tickTimer: null,
      // 消息审查详情弹窗
      showInspectModal: false,
      inspectTab: "prompt",
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
    inputPlaceholder() {
      return this.t("输入任务：@ 一位成员，或 /collab @主创 @协助成员 任务内容… (Enter 发送)");
    },
    // 协助任务各环节的执行记录不进聊天流（但能在卡片弹窗里查完整输入 / 原始输出）
    visibleMessages() {
      return (this.currentSession?.messages || []).filter(m => m && m.in_chat !== false);
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
    // 输入时的 /collab 严格校验：主创 + 至少一位协助成员 + 任务内容 + 成员可用
    collabHint() {
      const text = (this.inputContent || "").trim();
      if (!/^\/collab(\s|$)/i.test(text)) return null;
      const rest = text.replace(/^\/collab\s*/i, "").trim();
      const pool = (this.settings.employees || []).map(e => {
        const name = String(e.name || "");
        const short = (name.match(/[（(][^（）()]+[）)]/g) || []).map(s => s.slice(1, -1));
        const aliases = [name, String(e.id || ""), ...short].filter(Boolean)
          .sort((a, b) => b.length - a.length);
        return { emp: e, aliases };
      });
      const picked = [];
      let cursor = 0;
      while (true) {
        const rawTail = rest.slice(cursor);
        const tail = rawTail.trimStart();
        if (!tail.startsWith("@")) break;
        const hit = pool.find(m => m.aliases.some(n => tail.startsWith("@" + n)));
        if (!hit) break;
        const alias = hit.aliases.find(n => tail.startsWith("@" + n));
        picked.push(hit.emp);
        cursor += (rawTail.length - rawTail.trimStart().length) + alias.length + 1;
      }
      const goal = rest.slice(cursor).trim();
      if (picked.length === 0) return { ok: false, text: this.t("缺少主创：请用 /collab @主创 @协助成员 任务内容") };
      if (picked.length < 2) return { ok: false, text: this.t("缺少协助成员：主创之后至少再指定一位协助成员") };
      if (!goal) return { ok: false, text: this.t("请写上任务内容") };
      // 停用或缺 CLI 的成员在这里就提示：否则格式看着正确，发出去才在审核环节变成「成员不可用」
      const usable = e => e && e.enabled !== false && String(e.cli_cmd || "").trim() !== "";
      const label = e => String(e.name || e.id || "");
      if (!usable(picked[0])) {
        return { ok: false, text: this.t("主创 {name} 已停用或缺少 CLI 配置：请先在设置里启用", { name: label(picked[0]) }) };
      }
      const unusable = picked.slice(1).filter(e => !usable(e));
      if (unusable.length) {
        return { ok: false, text: this.t("协助成员 {name} 已停用或缺少 CLI 配置：请先在设置里启用", { name: unusable.map(label).join("、") }) };
      }
      return { ok: true, text: this.t("格式正确：将发起一轮协助任务") };
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
    }
  },
  mounted() {
    this.restoreLayout();
    this.init();
    this.pollInterval = setInterval(this.pollActiveSession, 1500);
    // 卡片外壳的「实时耗时」流水：每秒只更新一个时间戳，不重拉接口
    this.tickTimer = setInterval(() => { this.nowTick = Date.now(); }, 1000);
    // 全局点击关闭成员下拉框
    document.addEventListener("click", this.handleGlobalClick);
    // 拖拽调宽
    window.addEventListener("mousemove", this.onDragMove);
    window.addEventListener("mouseup", this.onDragEnd);
    window.addEventListener("resize", this.onWindowResize);
  },
  beforeUnmount() {
    if (this.pollInterval) clearInterval(this.pollInterval);
    if (this.tickTimer) clearInterval(this.tickTimer);
    document.removeEventListener("click", this.handleGlobalClick);
    window.removeEventListener("mousemove", this.onDragMove);
    window.removeEventListener("mouseup", this.onDragEnd);
    window.removeEventListener("resize", this.onWindowResize);
  },
  methods: {
    // ----------------- 任务卡片与消息上的取消 / 重试 -----------------
    openCardModal(msg) {
      this.cardModalMsg = msg;
      this.showCardModal = true;
    },
    openInspectMsg(payload) {
      this.openInspectModal({
        id: "task-record",
        sender: "task",
        sender_name: this.t("任务记录"),
        content: payload.content || "",
        sent_prompt: payload.sent_prompt || "",
        raw_response: payload.raw_response || ""
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
    taskElapsed(card) {
      if (!card || !card.started_at) return "0s";
      const end = card.finished_at || Math.floor(this.nowTick / 1000);
      const sec = Math.max(0, end - card.started_at);
      if (sec < 60) return `${sec}s`;
      return `${Math.floor(sec / 60)}m ${sec % 60}s`;
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
    openSettings(tab = "general") {
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
      this.measureMsgOverflow();
    },

    handleGlobalClick(e) {
      if (!e.target.closest(".relative")) {
        this.showMemberDropdown = false;
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
      if (!this.dragTarget) return;
      this.dragTarget = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      this.clampPanels();
      this.persistLayout();
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

    async quickRemoveMember(empId) {
      if (!this.currentSession || !Array.isArray(this.currentSession.employee_ids)) return;
      this.currentSession.employee_ids = this.currentSession.employee_ids.filter(id => id !== empId);
      await this.saveSessionEmployees();
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

    // ----------------- 消息输入与 Backspace 整删 @ 标签 -----------------
    onInputKeydown(e) {
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
          const match = textBefore.match(/@([^\s@]+)\s?$/);
          if (match) {
            e.preventDefault();
            const tagLength = match[0].length;
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
    toggleMsgExpand(msg) {
      const id = msg && msg.id;
      if (!id) return;
      // 整体替换对象，确保 Vue 3 的响应式一定被触发
      this.expandedMsgs = { ...this.expandedMsgs, [id]: !this.expandedMsgs[id] };
      this.$nextTick(this.measureMsgOverflow);
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
      this._mdCache.set(src, out);
      return out;
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
          this.measureMsgOverflow();
        }
      } catch (e) {
        // 静默容错
      }
    },

    insertMention(name) {
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
    async loadWorkspaceFiles() {
      const dir = this.currentSession?.workspace_dir;
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
        try {
          const res = await fetch(`/api/workspace/files?path=${encodeURIComponent(item.path)}`);
          const data = await res.json();
          this.workspaceItems = data.items || [];
        } catch (e) {
          console.error("展开目录失败:", e);
        }
      } else {
        try {
          const res = await fetch(`/api/workspace/file?path=${encodeURIComponent(item.path)}`);
          const data = await res.json();
          this.selectedWorkspaceFile = item;
          this.selectedFileContent = data.content || this.t("(空文件)");
        } catch (e) {
          alert(this.t("读取文件失败:") + " " + e);
        }
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

    openAddEmployeeModal() {
      this.isEditingEmployee = false;
      this.editingEmpIdx = -1;
      this.selectedModelId = "";
      
      const uniqueId = "emp_" + Date.now().toString(36) + "_" + Math.random().toString(36).substring(2, 6);
      
      this.editingEmp = {
        id: uniqueId,
        name: "",
        avatar: "🤖",
        avatar_color: "#3b82f6",
        type: "cli",
        cli_cmd: "",
        system_prompt: "",
        enabled: true,
        timeout: 0,
        write_access: false
      };
      this.showEmpEditModal = true;
    },

    openEditEmployeeModal(emp, idx) {
      this.isEditingEmployee = true;
      this.editingEmpIdx = idx;
      this.editingEmp = JSON.parse(JSON.stringify(emp));
      const match = this.scannedModels.find(m => m.cli_cmd === emp.cli_cmd);
      this.selectedModelId = match ? this.modelKey(match) : "";
      this.showEmpEditModal = true;
    },

    onModelSelectChanged() {
      const match = this.scannedModels.find(m => this.modelKey(m) === this.selectedModelId);
      if (match) {
        this.editingEmp.cli_cmd = match.cli_cmd;
        if (!this.editingEmp.name) {
          this.editingEmp.name = match.display_name.replace(/\[.*?\]\s*/g, "");
        }
        // 如果用户尚未填写 Prompt，且扫描出来的模型有预设则使用；否则保持干净空白
        if (!this.editingEmp.system_prompt && match.default_prompt) {
          this.editingEmp.system_prompt = match.default_prompt;
        }
      }
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
