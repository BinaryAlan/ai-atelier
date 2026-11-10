const { createApp } = Vue;

createApp({
  data() {
    return {
      sessions: [],
      currentSessionId: null,
      currentSession: {
        id: "",
        title: "",
        messages: [],
        document: "",
        employee_ids: [],
        status: "idle",
        phase: 1,
        current_speaker: null
      },
      settings: {
        moderator_id: "antigravity",
        employees: []
      },
      inputContent: "",
      isNewDirection: false,
      docViewMode: "preview",
      showSettingsModal: false,
      pollInterval: null
    };
  },
  computed: {
    favoriteSessions() {
      return this.sessions.filter(s => s.is_favorite);
    },
    normalSessions() {
      return this.sessions.filter(s => !s.is_favorite);
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
    // 启动全局轮询器（当会议 running 时实时同步状态）
    this.pollInterval = setInterval(this.pollActiveSession, 1500);
  },
  beforeUnmount() {
    if (this.pollInterval) clearInterval(this.pollInterval);
  },
  methods: {
    async init() {
      await this.loadSettings();
      await this.loadSessions();
      if (this.sessions.length > 0) {
        await this.switchSession(this.sessions[0].id);
      } else {
        await this.createNewSession();
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
      try {
        const res = await fetch(`/api/sessions/${id}`);
        if (res.ok) {
          this.currentSession = await res.json();
          this.$nextTick(this.scrollToBottom);
        }
      } catch (e) {
        console.error("切换会话失败:", e);
      }
    },

    async createNewSession() {
      try {
        const res = await fetch("/api/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: "新架构评审会议",
            employee_ids: this.availableEmployees.map(e => e.id)
          })
        });
        const newSession = await res.json();
        await this.loadSessions();
        await this.switchSession(newSession.id);
      } catch (e) {
        console.error("新建会话失败:", e);
      }
    },

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
            await this.createNewSession();
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

    async sendMessage() {
      const text = this.inputContent.trim();
      if (!text || !this.currentSessionId) return;

      const isNew = this.isNewDirection;
      this.inputContent = "";
      this.isNewDirection = false;

      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}/message`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            content: text,
            is_new_direction: isNew
          })
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
        await fetch(`/api/sessions/${this.currentSessionId}/pause`, { method: "POST" });
        this.currentSession.status = "paused";
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

    async stepMeeting() {
      if (!this.currentSessionId) return;
      try {
        const res = await fetch(`/api/sessions/${this.currentSessionId}/step`, { method: "POST" });
        const data = await res.json();
        if (data.session) {
          this.currentSession = data.session;
          this.$nextTick(this.scrollToBottom);
        }
      } catch (e) {
        console.error("单步推进失败:", e);
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

    copyDoc() {
      const doc = this.currentSession?.document || "";
      navigator.clipboard.writeText(doc).then(() => {
        alert("Markdown 方案已复制到剪贴板！");
      });
    },

    exportDoc() {
      if (!this.currentSessionId) return;
      window.open(`/api/sessions/${this.currentSessionId}/export`, "_blank");
    },

    async saveDocManual() {
      if (!this.currentSessionId) return;
      try {
        await fetch(`/api/sessions/${this.currentSessionId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ document: this.currentSession.document })
        });
      } catch (e) {
        console.error("保存文档失败:", e);
      }
    },

    openSettingsModal() {
      this.showSettingsModal = true;
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
          alert("员工配置保存成功！");
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
