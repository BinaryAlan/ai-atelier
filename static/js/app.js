const { createApp } = Vue;

createApp({
  data() {
    return {
      sessionTab: "all", // 'all' | 'fav'
      sessions: [],
      currentSessionId: null,
      currentSession: {
        id: "",
        title: "",
        messages: [],
        document: "",
        employee_ids: [],
        workspace_dir: "",
        status: "idle",
        phase: 1,
        current_speaker: null
      },
      settings: {
        moderator: {
          id: "moderator",
          name: "会议主持人",
          title: "会议主持人",
          avatar: "👑",
          avatar_color: "#d97706",
          type: "cli",
          cli_cmd: 'agy -p "{prompt}"',
          system_prompt: "你是会议主持人。负责主持会议进程、客观梳理各方意见、汇总生成方案草案，并在全员达成共识时出具终稿文档。",
          timeout: 0
        },
        employees: []
      },
      inputContent: "",
      // 引用回复：{ id, sender, sender_name, content }，发送时把 id 交给后端定向
      replyTarget: null,
      // 长消息展开状态（按消息 id 记，轮询刷新不会把用户展开的又收回去）
      expandedMsgs: {},
      // 哪些消息真的被限高截断了（渲染后实测，决定是否给「展开全文」按钮）
      overflowMsgs: {},
      rightTab: "doc", // 'doc' | 'workspace'
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

      // 新建议题弹窗
      showNewSessionModal: false,
      newSessionForm: {
        title: "",
        workspace_dir: "",
        employee_ids: []
      },

      // 编辑当前会议弹窗 (改名、改目录、增减参会人员)
      showEditSessionModal: false,
      editingSessionForm: {
        id: "",
        title: "",
        workspace_dir: "",
        employee_ids: []
      },

      // 员工与主持人配置弹窗
      showSettingsModal: false,
      showEmpEditModal: false,
      isEditingModerator: false,
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
        timeout: 0
      },
      presetAvatars: ["🤖", "🧠", "⚡", "🦉", "🛡️", "🚀", "💻", "🔬", "🎯", "🎨", "👑", "🕵️", "🔧", "💡", "⚖️", "📊"],

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
      // 流式输出：等待期间从 /stream 拉到的实时文本
      liveStream: null,
      streamTimer: null,

      isComposing: false, // 正在使用输入法组词或英文回车确认
      defaultWorkspaceDir: "",
      switchToken: 0,     // 会话切换请求序号，用于丢弃过期的在途响应
      pollInterval: null,

      // 服务进程控制（状态 / 重启 / 停止）
      showServiceMenu: false,
      systemStatus: null,
      serviceBusy: false,
      serviceDown: false,
      resumeBusy: false,  // 恢复请求在途：按钮保持「恢复中…」，避免连点变成暂停
      interruptBusy: false, // 中断当前发言在途，避免连点重复杀进程
      toastText: "",      // 顶部轻提示（恢复结果/自动暂缓等）

      // 布局：左栏可收起、左右两栏宽度可拖拽、纪要可全屏
      sidebarWidth: 256,
      rightWidth: 440,
      sidebarCollapsed: false,
      docFullscreen: false,
      dragTarget: null,   // 'sidebar' | 'right'
      dragStartX: 0,
      dragStartW: 0
    };
  },
  computed: {
    // Prompt 按【区块标题】切段：大段材料（聊天上下文、方案纪要/草案）默认折叠，
    // 只留标题和体量提示，避免一屏全是历史正文、真正要看的要求被挤到很下面。
    promptSections() {
      const raw = String((this.inspectingMsg && this.inspectingMsg.sent_prompt) || "");
      if (!raw.trim()) return [];

      const COLLAPSED_BY_DEFAULT = ["上下文", "纪要", "草案", "正文", "回复的这条消息"];
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

    // 实际参加本场会议的员工列表（未勾选/未选择的不在此列表中）
    activeSessionEmployees() {
      if (!this.currentSession || !Array.isArray(this.currentSession.employee_ids)) return [];
      const empMap = new Map((this.settings.employees || []).filter(e => e.enabled).map(e => [e.id, e]));
      return this.currentSession.employee_ids
        .map(id => empMap.get(id))
        .filter(Boolean);
    },
    totalParticipantsCount() {
      // 仅统计：主持人 (1) + 本场实际参会员工数 (未选中的不被计数)
      return 1 + this.activeSessionEmployees.length;
    },
    // 被群主「跳过」的成员（CLI 报错后不再占发言位，可随时恢复）
    skippedCount() {
      return Array.isArray(this.currentSession?.skipped_employees)
        ? this.currentSession.skipped_employees.length
        : 0;
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
    renderedDoc() {
      const doc = this.currentSession?.document || "*(暂无方案文档)*";
      // 任一依赖 CDN 不可用时，绝不把原始 Markdown/HTML 直接交给 v-html（会执行脚本）
      if (!window.marked || !window.DOMPurify) {
        return `<pre style="white-space:pre-wrap">${this.escapeHtml(doc)}</pre>`;
      }
      return DOMPurify.sanitize(marked.parse(doc));
    },
    // 定向 @ 应答进行中：此时的「中断」语义是取消这次提问，而不是把成员移出发言位
    isDirectRunning() {
      return !!this.currentSession?.target_direct_mention;
    }
  },
  mounted() {
    this.restoreLayout();
    this.init();
    this.pollInterval = setInterval(this.pollActiveSession, 1500);
    // 流式预览：单独一条更快的轮询，只在有人正在发言时真正请求
    this.streamTimer = setInterval(this.pollStreamPreview, 700);
    // 全局点击关闭参会人下拉框
    document.addEventListener("click", this.handleGlobalClick);
    // 拖拽调宽 / Esc 退出全屏
    window.addEventListener("mousemove", this.onDragMove);
    window.addEventListener("mouseup", this.onDragEnd);
    window.addEventListener("resize", this.onWindowResize);
    window.addEventListener("keydown", this.onWindowKeydown);
  },
  beforeUnmount() {
    if (this.pollInterval) clearInterval(this.pollInterval);
    if (this.streamTimer) clearInterval(this.streamTimer);
    document.removeEventListener("click", this.handleGlobalClick);
    window.removeEventListener("mousemove", this.onDragMove);
    window.removeEventListener("mouseup", this.onDragEnd);
    window.removeEventListener("resize", this.onWindowResize);
    window.removeEventListener("keydown", this.onWindowKeydown);
  },
  methods: {
    // 窗口尺寸变化：既要把面板宽度钳回可用范围，也要重算哪些消息该折叠
    onWindowResize() {
      this.clampPanels();
      this.measureMsgOverflow();
    },

    handleGlobalClick(e) {
      if (!e.target.closest(".relative")) {
        this.showMemberDropdown = false;
      }
      if (!e.target.closest("#service-menu-wrap")) {
        this.showServiceMenu = false;
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
      if (target === "right" && this.docFullscreen) return;
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
    toggleDocFullscreen() {
      this.docFullscreen = !this.docFullscreen;
      if (this.docFullscreen) this.rightTab = "doc";
    },
    onWindowKeydown(e) {
      if (e.key === "Escape" && this.docFullscreen) {
        this.docFullscreen = false;
      }
    },

    async init() {
      await this.loadDefaultWorkspace();
      await this.loadSettings();
      await this.loadSessions();
      await this.triggerScanModels(false);
      this.fetchSystemStatus(); // 仅用于点亮服务入口，不阻塞首屏

      if (this.sessions.length > 0) {
        // 刷新页面（尤其是「正在回答」时刷新）要回到刚才那场会，而不是列表第一场：
        // 会话数据在服务端，刷新不影响会议进程，只有选错会话才会让人误以为「丢了」。
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
        this.settings = await res.json();
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
      // 引用与展开状态都属于具体会话的某条消息，切会话就清掉，避免引用到别的会议的发言
      this.replyTarget = null;
      this.expandedMsgs = {};
      this.liveStream = null;   // 实时文本属于上一场会议，切走要清
      try {
        const res = await fetch(`/api/sessions/${id}`);
        if (token !== this.switchToken) return;
        if (res.ok) {
          this.currentSession = await res.json();
          this.$nextTick(this.scrollToBottom);
          this.measureMsgOverflow();
          this.loadWorkspaceFiles();
        }
      } catch (e) {
        console.error("切换会话失败:", e);
      }
    },

    // ----------------- 新建议题 Modal -----------------
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
      const title = this.newSessionForm.title.trim() || "新方案研讨";
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
        alert("创建会议失败: " + e);
      }
    },

    // ----------------- 编辑会议信息 Modal (标题/目录/参会人) -----------------
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
        }
      } catch (e) {
        alert("保存会议配置失败: " + e);
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
      if (!confirm("确定要删除此会议记录吗？")) return;
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
          this.$nextTick(this.scrollToBottom);
          await this.loadSessions();
        }
      } catch (e) {
        console.error("更新参会成员失败:", e);
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
          // 匹配光标前最近的一个 @标签 (例如 "@所有人 " 或 "@DeepSeek-V4 (DS4) ")
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
          // 失败时保留输入内容与引用条，别让用户白打一遍
          alert(data.error || `发送失败（HTTP ${res.status}）`);
          return;
        }
        this.inputContent = "";
        this.replyTarget = null;
        this.currentSession = data;
        this.$nextTick(this.scrollToBottom);
        this.measureMsgOverflow();
        await this.loadSessions();
      } catch (e) {
        console.error("发送消息失败:", e);
        alert("发送失败：" + e);
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
        if (changed) this.overflowMsgs = next;
      });
    },

    // ----------------- 引用回复（微信式） -----------------
    canReplyMsg(msg) {
      if (!msg || !msg.id) return false;
      if (msg.sender === "user" || msg.sender === "system") return false;
      // 报错/超时/跳过的占位没有可引用的内容
      if (["TIMEOUT", "ERROR", "SKIPPED"].includes(msg.verdict)) return false;
      if (!String(msg.content || "").trim()) return false;
      if (this.isModerator(msg.sender)) return true;
      // 已移出本场会议的成员不再提供回复入口（后端也会拒绝）
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

    // Markdown 渲染（AI 发言 / 定时纪要共用）：CDN 不可用时退回安全纯文本
    renderMdText(text) {
      const src = String(text || "");
      if (!src.trim()) return "";
      if (!window.marked || !window.DOMPurify) {
        return `<pre style="white-space:pre-wrap">${this.escapeHtml(src)}</pre>`;
      }
      try {
        return DOMPurify.sanitize(marked.parse(src));
      } catch (e) {
        return `<pre style="white-space:pre-wrap">${this.escapeHtml(src)}</pre>`;
      }
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

    async pauseMeeting() {
      if (!this.currentSessionId) return;
      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}/pause`, { method: "POST" });
        if (res.ok) {
          this.currentSession.status = "paused";
          this.currentSession.current_speaker = null;
          const sRes = await fetch(`/api/sessions/${this.currentSessionId}`);
          this.currentSession = await sRes.json();
          this.$nextTick(this.scrollToBottom);
        }
      } catch (e) {
        console.error("暂停失败:", e);
      }
    },

    async resumeMeeting() {
      if (!this.currentSessionId || this.resumeBusy) return;
      const targetId = this.currentSessionId;
      this.resumeBusy = true;
      try {
        const res = await fetch(`/api/sessions/${targetId}/resume`, { method: "POST" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          // 常见：全员已被跳过、或只剩一个已被判定卡住的成员。
          // 过去这里不检查 ok，界面照样切成「进行中」，用户只能反复点、看不到任何原因。
          alert(data.error || `恢复失败（HTTP ${res.status}），请稍后再试。`);
          return;
        }

        // 用服务端真实状态覆盖本地，而不是先乐观改成 running：
        // 否则按钮会立刻变成「暂停会议」，用户再点一下就把刚恢复的会议又暂停了。
        if (targetId === this.currentSessionId) {
          try {
            const sRes = await fetch(`/api/sessions/${targetId}`, { cache: "no-store" });
            if (sRes.ok) {
              const latest = await sRes.json();
              if (latest.id === this.currentSessionId) this.currentSession = latest;
            }
          } catch (e) {
            /* 状态拉取失败不影响恢复本身 */
          }
        }
        if (data.message) this.showToast(data.message);
        this.$nextTick(this.scrollToBottom);
      } catch (e) {
        // 服务刚重启时请求会直接失败：一定要说出来，不能只写 console
        alert(
          "恢复请求没有送达服务端：\n" + e +
          "\n\n如果服务刚刚重启，请等 1~2 秒后再点一次。"
        );
      } finally {
        this.resumeBusy = false;
      }
    },

    // 轻量提示条：不打断操作，也不需要用户点掉
    showToast(text) {
      if (!text) return;
      this.toastText = text;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { this.toastText = ""; }, 4000);
    },

    // ---------------- 单个成员的异常恢复（重试 / 跳过 / 恢复） ----------------
    isSkipped(empId) {
      return Array.isArray(this.currentSession?.skipped_employees)
        && this.currentSession.skipped_employees.includes(empId);
    },

    // 被 @ 的定向应答不属于任何轮次，失败了只能同一个人重问（换人没意义）。
    // 阶段一的立论异常也支持原地重跑；阶段二的审阅异常靠「恢复讨论」重问同一人。
    canRetryMsg(msg) {
      if (!msg) return false;
      if (msg.verdict !== "ERROR" && msg.verdict !== "TIMEOUT") return false;
      if (!msg.sent_prompt) return false;
      if (this.isModerator(msg.sender) || msg.sender === "user" || msg.sender === "system") return false;
      if (msg.direct_mention) return true;
      if (msg.phase !== 1) return false;
      // 会议已经走到阶段二了，再回头补一条立论没有意义
      if ((this.currentSession?.phase || 1) !== 1) return false;
      return true;
    },

    canSkipMsg(msg) {
      if (!msg) return false;
      if (msg.verdict !== "ERROR" && msg.verdict !== "TIMEOUT") return false;
      // 群主点名要的答案，跳过这个人等于让这次提问没有下文 —— 只给「重试」
      if (msg.direct_mention) return false;
      return !this.isModerator(msg.sender) && msg.sender !== "user" && msg.sender !== "system";
    },

    async interruptCurrentSpeaker() {
      if (!this.currentSessionId || this.interruptBusy) return;
      // 定向应答时它是「取消这次提问」，不动这个人的发言位，提示语要跟着变
      const wasDirect = !!this.currentSession?.target_direct_mention;
      this.interruptBusy = true;
      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}/interrupt`, {
          method: "POST",
          headers: { "X-Meeting-Action": "1" }
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          alert(data.error || `中断失败: HTTP ${res.status}`);
        } else if (wasDirect) {
          this.toastText = `已取消 [${data.interrupted || "这次回答"}] 的提问，它仍在会上，可以随时再 @ 它。`;
          setTimeout(() => { this.toastText = ""; }, 6000);
          // 立刻拉一次最新状态，避免等下一轮轮询才有反馈
          const latest = await fetch(`/api/sessions/${this.currentSessionId}`).then(r => r.ok ? r.json() : null);
          if (latest && latest.id === this.currentSessionId) this.currentSession = latest;
        } else {
          this.toastText = `已中断 [${data.interrupted || "当前发言"}]，会议交由余下成员继续。`;
          setTimeout(() => { this.toastText = ""; }, 6000);
          // 立刻拉一次最新状态，避免等下一轮轮询才有反馈
          const latest = await fetch(`/api/sessions/${this.currentSessionId}`).then(r => r.ok ? r.json() : null);
          if (latest && latest.id === this.currentSessionId) this.currentSession = latest;
        }
      } catch (e) {
        alert("中断请求失败: " + e);
      } finally {
        this.interruptBusy = false;
      }
    },

    async retryMessage(msg) {
      if (!this.currentSessionId || !msg) return;
      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}/messages/${msg.id}/retry`, {
          method: "POST",
          headers: { "X-Meeting-Action": "1" }
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          alert(data.error || `重试失败: HTTP ${res.status}`);
          return;
        }
        if (data.session) this.currentSession = data.session;
      } catch (e) {
        alert("重试请求失败: " + e);
      }
    },

    async skipMember(msg) {
      if (!this.currentSessionId || !msg) return;
      const name = msg.sender_name || "该成员";
      if (!confirm(
        `跳过 [${name}] 并继续推进？\n\n` +
        "它不再占用本次会议的发言位，余下成员照常推进；\n" +
        "配置与历史发言都保留，可在「参会成员」面板点「恢复」拉回来。"
      )) return;
      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}/messages/${msg.id}/skip`, {
          method: "POST",
          headers: { "X-Meeting-Action": "1" }
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          alert(data.error || `跳过失败: HTTP ${res.status}`);
          return;
        }
        if (data.session) this.currentSession = data.session;
      } catch (e) {
        alert("跳过请求失败: " + e);
      }
    },

    async restoreMember(empId) {
      if (!this.currentSessionId || !empId) return;
      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}/members/${empId}/restore`, {
          method: "POST",
          headers: { "X-Meeting-Action": "1" }
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          alert(data.error || `恢复失败: HTTP ${res.status}`);
          return;
        }
        if (data.session) this.currentSession = data.session;
      } catch (e) {
        alert("恢复请求失败: " + e);
      }
    },

    // ---------------- 服务进程控制 ----------------
    formatUptime(sec) {
      const s = Math.max(0, Math.floor(sec || 0));
      const d = Math.floor(s / 86400);
      const h = Math.floor((s % 86400) / 3600);
      const m = Math.floor((s % 3600) / 60);
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

    toggleServiceMenu() {
      this.showServiceMenu = !this.showServiceMenu;
      if (this.showServiceMenu) this.fetchSystemStatus();
    },

    async restartService() {
      if (this.serviceBusy) return;
      if (!confirm("确定重启服务？\n\n正在执行的 AI 任务会被立即终止，会议自动转为暂停；重启后点「恢复讨论」即可继续。")) return;

      if (!this.systemStatus) await this.fetchSystemStatus();
      const oldStart = this.systemStatus?.started_at || 0;
      const oldPid = this.systemStatus?.pid || 0;

      this.serviceBusy = true;
      this.showServiceMenu = false;
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
        alert("重启后 20 秒内没能连上服务。请查看终端输出，或手动执行 ./run.sh。");
        this.serviceDown = true;
      }
    },

    async shutdownService() {
      if (this.serviceBusy) return;
      if (!confirm("确定停止服务？\n\n所有在跑的 AI 任务会立即终止，会议转为暂停。停止后需重新运行 ./run.sh 才能再次使用。")) return;

      this.serviceBusy = true;
      this.showServiceMenu = false;
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
        alert("停止指令已发出，但服务仍在响应。请查看终端是否有异常。");
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

    // 流式预览：有人正在发言时拉一次「正在生成的文本」。
    // 暂存区在服务端内存里，拉不到（204）就保持「等待中」的显示，不报错。
    async pollStreamPreview() {
      if (!this.currentSessionId) return;
      if (!this.currentSession?.current_speaker) {
        if (this.liveStream) this.liveStream = null;
        return;
      }
      const container = this.$refs.chatContainer;
      const atBottom = container
        ? container.scrollHeight - container.scrollTop - container.clientHeight < 140
        : true;
      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}/stream`, { cache: "no-store" });
        if (!res.ok || res.status === 204) return;
        const data = await res.json();
        if (!data || !data.text) return;
        this.liveStream = data;
        if (atBottom) this.$nextTick(this.scrollToBottom);
      } catch (e) {
        // 瞬时失败忽略，下一轮再取
      }
    },

    async pollActiveSession() {
      const targetId = this.currentSessionId;
      if (!targetId) return;
      // 单独重试期间 status 可能仍是 paused，只有 current_speaker 能反映「正在跑」
      const busy = this.currentSession
        && (this.currentSession.status === "running" || this.currentSession.current_speaker);
      if (busy) {
        try {
          const res = await fetch(`/api/sessions/${targetId}`);
          if (res.ok) {
            const latest = await res.json();
            // 防止在途请求返回时用户已切换会话导致数据覆盖
            if (latest.id !== this.currentSessionId) return;
            const hadFewerMsgs = (this.currentSession.messages?.length || 0) < (latest.messages?.length || 0);
            this.currentSession = latest;
            if (hadFewerMsgs) {
              this.$nextTick(this.scrollToBottom);
            }
            this.measureMsgOverflow();
          }
        } catch (e) {
          // 静默容错
        }
      }
    },

    insertMention(name) {
      this.inputContent = (this.inputContent ? this.inputContent + " " : "") + `@${name} `;
      this.$nextTick(() => {
        const textarea = this.$refs.chatInput;
        if (textarea) textarea.focus();
      });
    },

    isModerator(senderId) {
      return senderId === "moderator" || senderId === "antigravity" || senderId === this.settings.moderator?.id;
    },

    getEmployeeColor(senderId) {
      if (senderId === "system") return "#f59e0b";
      if (this.isModerator(senderId)) return this.settings.moderator?.avatar_color || "#d97706";
      const emp = (this.settings.employees || []).find(e => e.id === senderId);
      return emp?.avatar_color || "#3b82f6";
    },

    getEmployeeAvatar(senderId) {
      if (senderId === "system") return "📢";
      if (this.isModerator(senderId)) return this.settings.moderator?.avatar || "👑";
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
          this.selectedFileContent = data.content || "(空文件)";
        } catch (e) {
          alert("读取文件失败: " + e);
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
          alert("已复制到剪贴板！");
        } else {
          const ta = document.createElement("textarea");
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
          alert("已复制到剪贴板！");
        }
      } catch (e) {
        console.warn("复制失败:", e);
      }
    },

    // ----------------- 员工与主持人配置设置 -----------------
    openSettingsModal() {
      this.showSettingsModal = true;
    },

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

    openEditModeratorModal() {
      this.isEditingModerator = true;
      this.isEditingEmployee = false;
      this.editingEmpIdx = -1;
      this.editingEmp = JSON.parse(JSON.stringify(this.settings.moderator || {}));
      const match = this.scannedModels.find(m => m.cli_cmd === this.editingEmp.cli_cmd);
      this.selectedModelId = match ? this.modelKey(match) : "";
      this.showEmpEditModal = true;
    },

    openAddEmployeeModal() {
      this.isEditingModerator = false;
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
        timeout: 0
      };
      this.showEmpEditModal = true;
    },

    openEditEmployeeModal(emp, idx) {
      this.isEditingModerator = false;
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
        alert("请输入角色姓名！");
        return;
      }
      if (!this.editingEmp.cli_cmd.trim()) {
        alert("CLI 执行模板不能为空！");
        return;
      }

      // 先在副本上修改，保存成功后再落到 this.settings，避免失败时本地已被改脏
      const next = {
        ...this.settings,
        moderator: JSON.parse(JSON.stringify(this.settings.moderator || {})),
        employees: JSON.parse(JSON.stringify(this.settings.employees || []))
      };

      if (this.isEditingModerator) {
        next.moderator = JSON.parse(JSON.stringify(this.editingEmp));
      } else if (this.isEditingEmployee && this.editingEmpIdx >= 0) {
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
      if (confirm(`确定要删除角色 [${targetEmp.name}] 吗？`)) {
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
        const res = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(nextSettings || this.settings)
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          // 后端的 schema / 白名单校验失败原因必须回显，否则用户只会看到「什么都没发生」
          alert("保存配置失败: " + (data.error || `HTTP ${res.status}`));
          return false;
        }
        this.settings = data.settings || this.settings;
        this.showEmpEditModal = false;
        if (closeMainModal) {
          this.showSettingsModal = false;
          alert("配置已成功保存！");
        }
        return true;
      } catch (e) {
        alert("保存配置失败: " + e);
        return false;
      }
    },

    scrollToBottom() {
      const container = this.$refs.chatContainer;
      if (container) {
        container.scrollTop = container.scrollHeight;
      }
    }
  }
}).mount("#app");
