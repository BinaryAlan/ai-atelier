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
        moderator_id: "antigravity",
        workspace_dir: "",
        status: "idle",
        phase: 1,
        current_speaker: null
      },
      settings: {
        default_moderator_id: "antigravity",
        employees: []
      },
      inputContent: "",
      rightTab: "doc", // 'doc' | 'workspace'
      workspaceItems: [],
      selectedWorkspaceFile: null,
      selectedFileContent: "",

      // 新建议题弹窗
      showNewSessionModal: false,
      newSessionForm: {
        title: "",
        workspace_dir: "",
        moderator_id: "",
        employee_ids: []
      },

      // 员工设置弹窗
      showSettingsModal: false,
      showEmpEditModal: false,
      isEditingEmployee: false,
      editingEmpIdx: -1,
      selectedModelId: "",
      scannedModels: [],
      editingEmp: {
        id: "",
        name: "",
        title: "",
        avatar: "🤖",
        avatar_color: "#3b82f6",
        type: "cli",
        cli_cmd: "",
        system_prompt: "",
        enabled: true
      },
      presetAvatars: ["🤖", "🧠", "⚡", "🦉", "🛡️", "🚀", "💻", "🔬", "🎯", "🎨", "👑", "🕵️", "🔧", "💡", "⚖️", "📊"],

      // 消息审查详情弹窗
      showInspectModal: false,
      inspectTab: "prompt", // 'prompt' | 'response'
      inspectingMsg: null,

      pollInterval: null
    };
  },
  computed: {
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
      return this.settings.employees.filter(e => e.enabled);
    },
    renderedDoc() {
      const doc = this.currentSession?.document || "*(暂无方案文档)*";
      if (window.marked) {
        return marked.parse(doc);
      }
      return doc;
    }
  },
  mounted() {
    this.init();
    this.pollInterval = setInterval(this.pollActiveSession, 1500);
  },
  beforeUnmount() {
    if (this.pollInterval) clearInterval(this.pollInterval);
  },
  methods: {
    async init() {
      await this.loadSettings();
      await this.loadSessions();
      await this.triggerScanModels(false);

      if (this.sessions.length > 0) {
        await this.switchSession(this.sessions[0].id);
      } else {
        this.openNewSessionModal();
      }
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
      this.selectedWorkspaceFile = null;
      this.selectedFileContent = "";
      try {
        const res = await fetch(`/api/sessions/${id}`);
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
      const defaultDir = this.currentSession?.workspace_dir || "/Users/lc/Code/ai-meeting";
      this.newSessionForm = {
        title: "",
        workspace_dir: defaultDir,
        moderator_id: this.settings.default_moderator_id || (this.availableEmployees[0]?.id || "antigravity"),
        employee_ids: this.availableEmployees.map(e => e.id)
      };
      this.showNewSessionModal = true;
    },

    async confirmCreateSession() {
      const title = this.newSessionForm.title.trim() || "新架构评审会议";
      try {
        const res = await fetch("/api/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: title,
            workspace_dir: this.newSessionForm.workspace_dir.trim(),
            moderator_id: this.newSessionForm.moderator_id,
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

    // ----------------- 收藏与重命名 -----------------
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

    async promptRename(session) {
      const newTitle = prompt("修改会议标题:", session.title);
      if (newTitle && newTitle.trim()) {
        try {
          await fetch(`/api/sessions/${session.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: newTitle.trim() })
          });
          await this.loadSessions();
          if (this.currentSessionId === session.id) {
            this.currentSession.title = newTitle.trim();
          }
        } catch (e) {
          console.error("重命名失败:", e);
        }
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
        await fetch(`/api/sessions/${this.currentSessionId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ employee_ids: this.currentSession.employee_ids })
        });
      } catch (e) {
        console.error("更新参会成员失败:", e);
      }
    },

    // ----------------- 消息发送与暂停 -----------------
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
      if (!this.currentSessionId) return;
      if (this.currentSession && this.currentSession.status === "running") {
        try {
          const res = await fetch(`/api/sessions/${this.currentSessionId}`);
          if (res.ok) {
            const latest = await res.json();
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
    },

    getEmployeeColor(senderId) {
      if (senderId === "system") return "#f59e0b";
      const emp = this.settings.employees.find(e => e.id === senderId);
      return emp?.avatar_color || "#3b82f6";
    },

    getEmployeeAvatar(senderId) {
      if (senderId === "system") return "📢";
      const emp = this.settings.employees.find(e => e.id === senderId);
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

    copyText(text) {
      if (!text) return;
      navigator.clipboard.writeText(text).then(() => {
        alert("已复制到剪贴板！");
      });
    },

    // ----------------- 员工与模型扫描设置 -----------------
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

    openAddEmployeeModal() {
      this.isEditingEmployee = false;
      this.editingEmpIdx = -1;
      this.selectedModelId = "";
      this.editingEmp = {
        id: "emp_" + Math.random().toString(36).substring(2, 7),
        name: "",
        title: "",
        avatar: "🤖",
        avatar_color: "#3b82f6",
        type: "cli",
        cli_cmd: "",
        system_prompt: "",
        enabled: true
      };
      this.showEmpEditModal = true;
    },

    openEditEmployeeModal(emp, idx) {
      this.isEditingEmployee = true;
      this.editingEmpIdx = idx;
      this.editingEmp = JSON.parse(JSON.stringify(emp));
      const match = this.scannedModels.find(m => m.cli_cmd === emp.cli_cmd);
      this.selectedModelId = match ? match.model_id : "";
      this.showEmpEditModal = true;
    },

    onModelSelectChanged() {
      const match = this.scannedModels.find(m => m.model_id === this.selectedModelId);
      if (match) {
        this.editingEmp.cli_cmd = match.cli_cmd;
        if (!this.editingEmp.name) {
          this.editingEmp.name = match.display_name.replace(/\[.*?\]\s*/g, "");
        }
        if (!this.editingEmp.title) {
          this.editingEmp.title = match.default_title;
        }
        if (!this.editingEmp.system_prompt) {
          this.editingEmp.system_prompt = match.default_prompt;
        }
      }
    },

    saveEmployeeItem() {
      if (!this.editingEmp.name.trim()) {
        alert("请输入员工姓名！");
        return;
      }
      if (!this.editingEmp.cli_cmd.trim()) {
        alert("CLI 执行模板不能为空！");
        return;
      }

      if (this.isEditingEmployee && this.editingEmpIdx >= 0) {
        this.settings.employees[this.editingEmpIdx] = JSON.parse(JSON.stringify(this.editingEmp));
      } else {
        this.settings.employees.push(JSON.parse(JSON.stringify(this.editingEmp)));
      }

      this.showEmpEditModal = false;
    },

    deleteEmployee(idx) {
      if (confirm(`确定要删除员工 [${this.settings.employees[idx].name}] 吗？`)) {
        this.settings.employees.splice(idx, 1);
      }
    },

    async saveSettingsToServer() {
      try {
        const res = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.settings)
        });
        if (res.ok) {
          this.showSettingsModal = false;
          alert("配置已成功保存！");
        }
      } catch (e) {
        console.error("保存配置失败:", e);
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
