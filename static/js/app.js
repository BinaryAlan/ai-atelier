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
          timeout: 180
        },
        employees: []
      },
      inputContent: "",
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
        timeout: 180
      },
      presetAvatars: ["🤖", "🧠", "⚡", "🦉", "🛡️", "🚀", "💻", "🔬", "🎯", "🎨", "👑", "🕵️", "🔧", "💡", "⚖️", "📊"],

      // 消息审查详情弹窗
      showInspectModal: false,
      inspectTab: "prompt",
      inspectingMsg: null,

      isComposing: false, // 正在使用输入法组词或英文回车确认
      defaultWorkspaceDir: "",
      switchToken: 0,     // 会话切换请求序号，用于丢弃过期的在途响应
      pollInterval: null
    };
  },
  computed: {
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
    }
  },
  mounted() {
    this.init();
    this.pollInterval = setInterval(this.pollActiveSession, 1500);
    // 全局点击关闭参会人下拉框
    document.addEventListener("click", this.handleGlobalClick);
  },
  beforeUnmount() {
    if (this.pollInterval) clearInterval(this.pollInterval);
    document.removeEventListener("click", this.handleGlobalClick);
  },
  methods: {
    handleGlobalClick(e) {
      if (!e.target.closest(".relative")) {
        this.showMemberDropdown = false;
      }
    },

    async init() {
      await this.loadDefaultWorkspace();
      await this.loadSettings();
      await this.loadSessions();
      await this.triggerScanModels(false);

      if (this.sessions.length > 0) {
        await this.switchSession(this.sessions[0].id);
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

    async switchSession(id) {
      this.currentSessionId = id;
      // 请求序号：快速连切会话时，丢弃已过期的在途响应，避免界面显示 A、操作发往 B
      const token = this.switchToken + 1;
      this.switchToken = token;
      this.selectedWorkspaceFile = null;
      this.selectedFileContent = "";
      this.showMemberDropdown = false;
      try {
        const res = await fetch(`/api/sessions/${id}`);
        if (token !== this.switchToken) return;
        if (res.ok) {
          this.currentSession = await res.json();
          this.$nextTick(this.scrollToBottom);
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

      this.inputContent = "";
      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}/message`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: text })
        });
        if (res.ok) {
          this.currentSession = await res.json();
          this.$nextTick(this.scrollToBottom);
          await this.loadSessions();
        }
      } catch (e) {
        console.error("发送消息失败:", e);
      }
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
      if (!this.currentSessionId) return;
      try {
        await fetch(`/api/sessions/${this.currentSessionId}/resume`, { method: "POST" });
        this.currentSession.status = "running";
      } catch (e) {
        console.error("恢复失败:", e);
      }
    },

    async pollActiveSession() {
      const targetId = this.currentSessionId;
      if (!targetId) return;
      if (this.currentSession && this.currentSession.status === "running") {
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
      this.showInspectModal = true;
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
        timeout: 180
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

      const saved = await this.saveSettingsToServer(next);
      if (saved) {
        this.showEmpEditModal = false;
      }
    },

    async deleteEmployee(idx) {
      const targetEmp = this.settings.employees[idx];
      if (confirm(`确定要删除角色 [${targetEmp.name}] 吗？`)) {
        this.settings.employees.splice(idx, 1);
        const saved = await this.saveSettingsToServer();
        if (saved && this.currentSession?.employee_ids) {
          this.currentSession.employee_ids = this.currentSession.employee_ids.filter(id => id !== targetEmp.id);
          await this.saveSessionEmployees();
        }
      }
    },

    async saveSettingsToServer(nextSettings) {
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
        this.showSettingsModal = false;
        alert("配置已成功保存！");
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
