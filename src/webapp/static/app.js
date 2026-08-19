// Telegram Mini App workbench interaction layer.
(() => {
  "use strict";

  const telegram = window.Telegram?.WebApp;
  const state = {
    bootstrap: null,
    activeConversation: null,
    runs: [],
    selectedAgent: "codex",
    selectedPermission: loadPermissionMode(),
    selectedModels: {
      codex: loadRunSetting("model", "codex"),
      claude: loadRunSetting("model", "claude"),
    },
    selectedEfforts: {
      codex: loadRunSetting("effort", "codex"),
      claude: loadRunSetting("effort", "claude"),
    },
    pollTimer: null,
    taskFilter: "",
    maxViewportHeight: 0,
    fileCache: new Map(),
    fileBlobCache: new Map(),
    runEventCache: new Map(),
    runEventRequests: new Map(),
    expandedProcessRuns: new Set(),
    objectUrls: [],
  };

  const elements = {
    app: document.querySelector("#app"),
    sidebar: document.querySelector("#sidebar"),
    sidebarBackdrop: document.querySelector("#sidebarBackdrop"),
    openSidebar: document.querySelector("#openSidebar"),
    closeSidebar: document.querySelector("#closeSidebar"),
    conversationList: document.querySelector("#conversationList"),
    taskSearch: document.querySelector("#taskSearch"),
    newTaskButton: document.querySelector("#newTaskButton"),
    refreshButton: document.querySelector("#refreshButton"),
    connectionLabel: document.querySelector("#connectionLabel"),
    projectButton: document.querySelector("#projectButton"),
    projectName: document.querySelector("#projectName"),
    agentOptions: [...document.querySelectorAll(".agent-option")],
    conversationView: document.querySelector("#conversationView"),
    emptyState: document.querySelector("#emptyState"),
    emptyProjectButton: document.querySelector("#emptyProjectButton"),
    messageStream: document.querySelector("#messageStream"),
    composerForm: document.querySelector("#composerForm"),
    composerInput: document.querySelector("#composerInput"),
    composerContext: document.querySelector("#composerContext"),
    permissionButton: document.querySelector("#permissionButton"),
    permissionLabel: document.querySelector("#permissionLabel"),
    modelSelect: document.querySelector("#modelSelect"),
    effortSelect: document.querySelector("#effortSelect"),
    sendButton: document.querySelector("#sendButton"),
    projectDialog: document.querySelector("#projectDialog"),
    projectSearch: document.querySelector("#projectSearch"),
    generalConversationButton: document.querySelector("#generalConversationButton"),
    projectList: document.querySelector("#projectList"),
    permissionDialog: document.querySelector("#permissionDialog"),
    permissionOptions: [...document.querySelectorAll(".permission-option")],
    fullAccessConfirm: document.querySelector("#fullAccessConfirm"),
    cancelFullAccess: document.querySelector("#cancelFullAccess"),
    confirmFullAccess: document.querySelector("#confirmFullAccess"),
    toastRegion: document.querySelector("#toastRegion"),
  };

  function loadPermissionMode() {
    try {
      const value = window.localStorage.getItem("agent-relay.permission-mode");
      if (["request_approval", "workspace_auto", "full_access"].includes(value)) return value;
    } catch (_) {
      // Storage may be unavailable inside privacy-restricted webviews.
    }
    return "request_approval";
  }

  function persistPermissionMode(value) {
    try {
      window.localStorage.setItem("agent-relay.permission-mode", value);
    } catch (_) {
      // The in-memory selection still works for the current Mini App session.
    }
  }

  function loadRunSetting(kind, agent) {
    try {
      return window.localStorage.getItem(`agent-relay.${kind}.${agent}`) || "";
    } catch (_) {
      return "";
    }
  }

  function persistRunSetting(kind, agent, value) {
    try {
      window.localStorage.setItem(`agent-relay.${kind}.${agent}`, value);
    } catch (_) {
      // The current selection still works without persistent browser storage.
    }
  }

  function configureTelegram() {
    if (!telegram) return;
    telegram.ready();
    telegram.expand();
    telegram.setHeaderColor?.("#111313");
    telegram.setBackgroundColor?.("#111313");
  }

  function syncViewportHeight() {
    const viewportHeight = window.visualViewport?.height || window.innerHeight;
    if (!viewportHeight) return;
    state.maxViewportHeight = Math.max(state.maxViewportHeight, viewportHeight);
    document.documentElement.style.setProperty("--app-height", `${Math.round(viewportHeight)}px`);
    const keyboardVisible =
      document.activeElement === elements.composerInput && viewportHeight < state.maxViewportHeight - 100;
    elements.app.classList.toggle("keyboard-visible", keyboardVisible);
    if (document.activeElement === elements.composerInput) requestAnimationFrame(scrollToBottom);
  }

  function configureViewport() {
    syncViewportHeight();
    window.addEventListener("resize", syncViewportHeight, { passive: true });
    window.visualViewport?.addEventListener("resize", syncViewportHeight, { passive: true });
    window.visualViewport?.addEventListener("scroll", syncViewportHeight, { passive: true });
    telegram?.onEvent?.("viewportChanged", syncViewportHeight);
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (telegram?.initData) headers.set("X-Telegram-Init-Data", telegram.initData);
    if (options.body) headers.set("Content-Type", "application/json");
    const response = await fetch(`/app/api${path}`, { ...options, headers });
    if (!response.ok) {
      let message = `请求失败 (${response.status})`;
      try {
        const payload = await response.json();
        if (payload.detail) message = payload.detail;
      } catch (_) {
        // Keep the generic status message.
      }
      throw new Error(message);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  async function apiBlob(path) {
    const headers = new Headers();
    if (telegram?.initData) headers.set("X-Telegram-Init-Data", telegram.initData);
    const response = await fetch(`/app/api${path}`, { headers });
    if (!response.ok) {
      let message = `文件读取失败 (${response.status})`;
      try {
        const payload = await response.json();
        if (payload.detail) message = payload.detail;
      } catch (_) {
        // Keep the generic status message.
      }
      throw new Error(message);
    }
    return response.blob();
  }

  function cachedApiBlob(path) {
    if (!state.fileBlobCache.has(path)) state.fileBlobCache.set(path, apiBlob(path));
    return state.fileBlobCache.get(path);
  }

  async function loadBootstrap({ preserveConversation = true } = {}) {
    setConnection("正在同步");
    try {
      state.bootstrap = await api("/bootstrap");
      renderProjectList();
      renderConversationList();
      const preferredId = preserveConversation
        ? state.activeConversation?.id || state.bootstrap.active_conversation_id
        : state.bootstrap.active_conversation_id;
      const conversation = state.bootstrap.conversations.find((item) => item.id === preferredId);
      if (conversation) {
        await openConversation(conversation, { activate: false });
      } else {
        state.activeConversation = null;
        state.runs = [];
        renderContext();
        renderMessages();
      }
      setConnection("已连接");
    } catch (error) {
      setConnection("连接失败");
      showToast(error.message);
    }
  }

  function setConnection(label) {
    elements.connectionLabel.textContent = label;
  }

  function renderConversationList() {
    const conversations = state.bootstrap?.conversations || [];
    const filter = state.taskFilter.trim().toLowerCase();
    const visible = conversations.filter((conversation) => {
      const run = conversation.latest_run;
      const haystack = `${conversation.project_name} ${conversation.title} ${run?.prompt || ""}`.toLowerCase();
      return !filter || haystack.includes(filter);
    });
    elements.conversationList.replaceChildren();
    if (!visible.length) {
      const empty = node("div", "sidebar-empty", filter ? "没有匹配的任务" : "还没有任务\n点击“新建任务”开始");
      elements.conversationList.append(empty);
      return;
    }
    visible.forEach((conversation) => {
      const latest = conversation.latest_run;
      const item = node("button", "conversation-item");
      item.type = "button";
      if (conversation.id === state.activeConversation?.id) item.classList.add("is-active");
      const titleRow = node("div", "conversation-title-row");
      titleRow.append(
        node("span", "conversation-title", latest?.prompt || conversation.title || "新任务"),
        node("time", "conversation-time", relativeTime(latest?.created_at || conversation.updated_at)),
      );
      const meta = node("div", "conversation-meta");
      const status = node("span", "mini-status");
      if (latest && isActiveStatus(latest.status)) status.classList.add("active");
      const scope = conversation.project_selected ? conversation.project_name : "通用对话";
      const activity = latest && isActiveStatus(latest.status) ? "运行中 · " : "";
      meta.append(status, document.createTextNode(`${activity}${scope} · ${agentLabel(conversation.active_agent)}`));
      item.append(titleRow, meta);
      item.addEventListener("click", () => openConversation(conversation));
      elements.conversationList.append(item);
    });
  }

  function hasAnyActiveRun() {
    return (
      state.runs.some((run) => isActiveStatus(run.status)) ||
      (state.bootstrap?.conversations || []).some(
        (conversation) => conversation.latest_run && isActiveStatus(conversation.latest_run.status),
      )
    );
  }

  async function openConversation(conversation, { activate = true } = {}) {
    closeSidebar();
    stopPolling();
    try {
      if (activate && state.bootstrap.active_conversation_id !== conversation.id) {
        await api(`/conversations/${conversation.id}/activate`, { method: "POST" });
        state.bootstrap.active_conversation_id = conversation.id;
      }
      state.activeConversation = conversation;
      state.selectedAgent = conversation.active_agent;
      state.runs = await api(`/conversations/${conversation.id}/runs`);
      const latestPermission = state.runs.at(-1)?.permission_mode;
      if (latestPermission) state.selectedPermission = latestPermission;
      renderContext();
      renderConversationList();
      renderMessages();
      if (hasAnyActiveRun()) startPolling();
    } catch (error) {
      showToast(error.message);
    }
  }

  function renderContext() {
    const conversation = state.activeConversation;
    elements.projectName.textContent = conversation?.project_name || "无项目";
    elements.composerContext.textContent = conversation
      ? conversation.project_selected
        ? `${conversation.project_name} · ${agentLabel(conversation.active_agent)}`
        : `通用对话 · ${agentLabel(conversation.active_agent)} · 只读`
      : `通用对话 · ${agentLabel(state.selectedAgent)} · 只读`;
    elements.agentOptions.forEach((button) => {
      button.classList.toggle("is-active", button.dataset.agent === (conversation?.active_agent || state.selectedAgent));
    });
    const general = !conversation || !conversation.project_selected;
    elements.permissionButton.disabled = general;
    elements.permissionLabel.textContent = general ? "只读" : permissionLabel(state.selectedPermission);
    elements.permissionButton.title = general ? "选择项目后可调整操作权限" : "调整 Agent 操作权限";
    renderAgentSettings();
    renderPermissionOptions();
    updateComposerState();
  }

  function renderAgentSettings() {
    const agent = state.activeConversation?.active_agent || state.selectedAgent;
    const options = state.bootstrap?.agent_options?.[agent];
    if (!options) return;
    const selectedModel = state.selectedModels[agent] || options.default_model || options.models[0];
    elements.modelSelect.replaceChildren();
    options.models.forEach((model) => elements.modelSelect.append(new Option(model, model)));
    elements.modelSelect.value = options.models.includes(selectedModel) ? selectedModel : options.models[0];
    state.selectedModels[agent] = elements.modelSelect.value;

    const selectedEffort = state.selectedEfforts[agent] || options.default_reasoning_effort;
    elements.effortSelect.replaceChildren();
    options.reasoning_efforts.forEach((effort) => {
      elements.effortSelect.append(new Option(reasoningLabel(effort), effort));
    });
    elements.effortSelect.value = options.reasoning_efforts.includes(selectedEffort)
      ? selectedEffort
      : options.default_reasoning_effort;
    state.selectedEfforts[agent] = elements.effortSelect.value;
  }

  function renderMessages() {
    state.objectUrls.forEach((url) => URL.revokeObjectURL(url));
    state.objectUrls = [];
    elements.messageStream.replaceChildren();
    const hasConversation = Boolean(state.activeConversation);
    const hasRuns = state.runs.length > 0;
    elements.emptyState.classList.toggle("hidden", hasConversation && hasRuns);
    elements.messageStream.classList.toggle("hidden", !hasConversation || !hasRuns);
    if (!hasConversation || !hasRuns) {
      elements.emptyState.querySelector("h1").textContent = hasConversation
        ? state.activeConversation.project_selected
          ? `开始处理 ${state.activeConversation.project_name}`
          : "开始通用对话"
        : "想让 Agent 做什么？";
      elements.emptyState.querySelector("p").textContent = hasConversation
        ? state.activeConversation.project_selected
          ? "直接描述任务。只读问题会立即回答，变更任务会按当前操作权限处理。"
          : "直接提问即可。通用对话不绑定项目目录，也不会执行写入。"
        : "直接在下方开始通用对话，或者选择项目处理代码。通用对话保持只读。";
      elements.emptyProjectButton.classList.toggle("hidden", hasConversation && state.activeConversation.project_selected);
      return;
    }
    state.runs.forEach((run) => renderRun(run));
    requestAnimationFrame(scrollToBottom);
  }

  function renderRun(run) {
    elements.messageStream.append(messageBlock("user", "你", "YOU", run.prompt));
    const assistant = messageBlock("assistant", agentLabel(run.agent), run.agent === "codex" ? "CX" : "CL", "");
    const body = assistant.querySelector(".message-body");
    const content = assistant.querySelector(".message-content");
    body.insertBefore(
      node(
        "div",
        "run-config-meta",
        `${run.model || "默认模型"} · ${run.reasoning_effort ? reasoningLabel(run.reasoning_effort) : "默认推理"}`,
      ),
      content,
    );

    if (run.status === "completed") {
      renderMarkdown(content, run.result || "任务已完成。");
      content.append(processPanel(run));
      const previews = node("section", "file-previews");
      content.append(previews);
      renderArtifacts(run, content, previews);
    } else if (run.status === "awaiting_approval") {
      content.textContent = "已经完成只读检查。执行前需要你的确认。";
      content.append(processPanel(run));
      content.append(approvalCard(run));
    } else if (["failed", "timed_out", "interrupted"].includes(run.status)) {
      content.textContent = "";
      content.append(node("div", "error-card", run.error || statusLabel(run.status)));
      content.append(processPanel(run));
    } else if (run.status === "rejected") {
      content.textContent = "计划已拒绝，没有执行任何写入操作。";
    } else if (run.status === "cancelled") {
      content.textContent = "任务已停止。已经产生的文件改动不会自动回滚。";
      content.append(processPanel(run));
    } else {
      content.append(activeRunCard(run));
    }
    elements.messageStream.append(assistant);
  }

  function messageBlock(type, author, avatar, text) {
    const block = node("article", `message-block ${type}`);
    block.append(node("div", "message-avatar", avatar));
    const body = node("div", "message-body");
    body.append(node("div", "message-author", author), node("div", "message-content", text));
    block.append(body);
    return block;
  }

  function activeRunCard(run) {
    const card = node("div", "run-card");
    const header = node("div", "run-card-header");
    const general = state.activeConversation && !state.activeConversation.project_selected;
    const queuedRuns = state.runs.filter((item) => item.status === "queued");
    const queueIndex = queuedRuns.findIndex((item) => item.id === run.id);
    const label =
      run.status === "queued" && queueIndex >= 0
        ? queueIndex === 0
          ? "已排队，等待当前任务结束"
          : `已排队，前面还有 ${queueIndex + 1} 条消息`
        : statusLabel(run.status, general, run.permission_mode);
    header.append(
      node("span", "activity-indicator"),
      document.createTextNode(label),
    );
    card.append(header);
    card.append(processPanel(run));
    if (["queued", "planning", "running", "cancel_requested"].includes(run.status)) {
      const actions = node("div", "card-actions");
      const stop = node("button", "danger-button", run.status === "cancel_requested" ? "正在停止" : "停止");
      stop.type = "button";
      stop.disabled = run.status === "cancel_requested";
      stop.addEventListener("click", () => cancelRun(run.id));
      actions.append(stop);
      card.append(actions);
    }
    return card;
  }

  function processPanel(run, expanded = false) {
    const details = node("details", "process-panel");
    details.dataset.runId = run.id;
    details.open = expanded || state.expandedProcessRuns.has(run.id);
    const summary = node("summary", "process-summary");
    summary.append(node("span", "process-chevron", "›"), document.createTextNode("执行过程"));
    const timeline = node("div", "process-timeline");
    timeline.append(node("div", "process-loading", "正在读取过程…"));
    details.append(summary, timeline);

    const cached = state.runEventCache.get(run.id);
    if (cached) paintRunEvents(timeline, cached);
    if (details.open) refreshRunEvents(run.id, timeline);
    details.addEventListener("toggle", () => {
      if (details.open) {
        state.expandedProcessRuns.add(run.id);
        refreshRunEvents(run.id, timeline);
      } else {
        state.expandedProcessRuns.delete(run.id);
      }
    });
    return details;
  }

  async function refreshRunEvents(runId, timeline) {
    try {
      if (!state.runEventRequests.has(runId)) {
        const request = api(`/runs/${runId}/events`).finally(() => state.runEventRequests.delete(runId));
        state.runEventRequests.set(runId, request);
      }
      const events = await state.runEventRequests.get(runId);
      state.runEventCache.set(runId, events);
      if (timeline.isConnected) paintRunEvents(timeline, events);
    } catch (error) {
      if (timeline.isConnected) timeline.replaceChildren(node("div", "process-loading", error.message));
    }
  }

  function paintRunEvents(timeline, events) {
    const signature = JSON.stringify(events);
    if (timeline.dataset.eventSignature === signature) return;
    timeline.dataset.eventSignature = signature;
    timeline.replaceChildren();
    const activity = events.filter((event) =>
      ["agent.started", "agent.status", "tool.started", "tool.completed", "approval.required"].includes(event.kind),
    );
    activity.slice(-40).forEach((event) => timeline.append(processEvent(event)));
    const output = events
      .filter((event) => event.kind === "output.delta")
      .map((event) => event.payload.text || "")
      .join("")
      .slice(-6000);
    if (output) {
      const outputBlock = node("div", "process-output");
      outputBlock.append(node("div", "process-output-label", "实时输出"), node("pre", "", output));
      timeline.append(outputBlock);
    }
    if (!timeline.childElementCount) timeline.append(node("div", "process-loading", "Agent 正在启动…"));
  }

  function processEvent(event) {
    const item = node("div", `process-event ${event.kind.replaceAll(".", "-")}`);
    const payload = event.payload || {};
    let title = "状态更新";
    let detail = "";
    if (event.kind === "agent.started") {
      if (payload.phase) {
        title = payload.phase === "execute" ? "开始执行" : "开始分析";
      } else {
        title = `${agentLabel(payload.agent)} 已连接`;
      }
      detail = payload.model ? `模型：${payload.model}` : "";
    } else if (event.kind === "agent.status") {
      title = activityLabel(payload.status || payload.message);
      detail = payload.message && payload.message !== payload.status ? payload.message : "";
    } else if (event.kind === "tool.started") {
      title = `调用工具 · ${payload.tool || payload.name || "Tool"}`;
      detail = compactPayload(payload, ["tool_call_id", "status", "tool", "name"]);
    } else if (event.kind === "tool.completed") {
      title = `工具完成 · ${payload.tool || payload.name || "Tool"}`;
      detail = compactPayload(payload, ["tool_call_id", "status", "tool", "name"]);
    } else if (event.kind === "approval.required") {
      title = "等待批准执行计划";
    }
    item.append(node("span", "process-dot"), node("strong", "", title));
    if (detail) item.append(node("pre", "process-detail", detail));
    return item;
  }

  function compactPayload(payload, omitted) {
    const visible = Object.fromEntries(Object.entries(payload).filter(([key]) => !omitted.includes(key)));
    if (!Object.keys(visible).length) return "";
    try {
      return JSON.stringify(visible, null, 2).slice(0, 1600);
    } catch (_) {
      return "";
    }
  }

  function approvalCard(run) {
    const card = node("div", "approval-card");
    card.append(
      node("div", "approval-label", "执行计划"),
      node("div", "approval-plan", run.plan || "Agent 没有返回可显示的计划。"),
    );
    const actions = node("div", "card-actions");
    const approve = node("button", "primary-button", "批准并执行");
    const reject = node("button", "secondary-button", "不执行");
    approve.type = "button";
    reject.type = "button";
    approve.addEventListener("click", () => decideRun(run.id, "approve"));
    reject.addEventListener("click", () => decideRun(run.id, "reject"));
    actions.append(approve, reject);
    card.append(actions);
    return card;
  }

  async function sendMessage(prompt) {
    if (!prompt.trim()) return;
    elements.sendButton.disabled = true;
    try {
      if (!state.activeConversation) await createConversation(null);
      if (!state.activeConversation) return;
      const run = await api(`/conversations/${state.activeConversation.id}/runs`, {
        method: "POST",
        body: JSON.stringify({
          prompt: prompt.trim(),
          permission_mode: state.selectedPermission,
          model: elements.modelSelect.value || null,
          reasoning_effort: elements.effortSelect.value || null,
        }),
      });
      state.runs.push(run);
      elements.composerInput.value = "";
      resizeComposer();
      renderMessages();
      startPolling();
      await refreshSidebarOnly();
    } catch (error) {
      showToast(error.message);
    } finally {
      updateComposerState();
    }
  }

  async function decideRun(runId, decision) {
    try {
      await api(`/runs/${runId}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      });
      await pollRun(runId);
      if (decision === "approve") startPolling();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function cancelRun(runId) {
    try {
      await api(`/runs/${runId}/cancel`, { method: "POST" });
      await pollRun(runId);
      startPolling();
    } catch (error) {
      showToast(error.message);
    }
  }

  function startPolling() {
    stopPolling();
    const tick = async () => {
      const active = await pollActiveRuns();
      if (active) state.pollTimer = window.setTimeout(tick, 1100);
    };
    state.pollTimer = window.setTimeout(tick, 500);
  }

  function stopPolling() {
    if (state.pollTimer) window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  async function pollRun(runId) {
    try {
      const updated = await api(`/runs/${runId}`);
      const index = state.runs.findIndex((run) => run.id === runId);
      const changed = index < 0 || runPresentationSignature(state.runs[index]) !== runPresentationSignature(updated);
      if (index >= 0) state.runs[index] = updated;
      if (changed) renderMessages();
      else refreshExpandedProcessPanels();
      const active = isActiveStatus(updated.status);
      if (!active) await refreshSidebarOnly();
      return active;
    } catch (error) {
      showToast(error.message);
      return false;
    }
  }

  async function pollActiveRuns() {
    if (!hasAnyActiveRun()) return false;
    try {
      const viewedConversationIsRunning =
        state.activeConversation && state.runs.some((run) => isActiveStatus(run.status));
      const [bootstrap, updates] = await Promise.all([
        api("/bootstrap"),
        viewedConversationIsRunning
          ? api(`/conversations/${state.activeConversation.id}/runs`)
          : Promise.resolve(null),
      ]);
      const bootstrapChanged = JSON.stringify(state.bootstrap) !== JSON.stringify(bootstrap);
      const previousContext = conversationContextSignature(state.activeConversation);
      state.bootstrap = bootstrap;
      if (updates) {
        const changed = runPresentationSignature(state.runs) !== runPresentationSignature(updates);
        state.runs = updates;
        if (changed) {
          renderMessages();
          updateComposerState();
        }
        else refreshExpandedProcessPanels();
      }
      const current = bootstrap.conversations.find((item) => item.id === state.activeConversation?.id);
      if (current) state.activeConversation = current;
      if (bootstrapChanged) {
        renderConversationList();
        renderProjectList();
      }
      if (previousContext !== conversationContextSignature(state.activeConversation)) renderContext();
      return hasAnyActiveRun();
    } catch (error) {
      showToast(error.message);
      return false;
    }
  }

  function renderMarkdown(container, markdown) {
    container.replaceChildren();
    container.classList.add("markdown-body");
    const lines = String(markdown).replaceAll("\r\n", "\n").split("\n");
    let paragraph = [];
    let list = null;
    let code = null;
    const flushParagraph = () => {
      if (!paragraph.length) return;
      const block = node("p");
      appendInlineMarkdown(block, paragraph.join("\n"));
      container.append(block);
      paragraph = [];
    };
    const closeList = () => {
      if (!list) return;
      container.append(list);
      list = null;
    };
    lines.forEach((line) => {
      const fence = line.match(/^\s*```([^`]*)$/);
      if (fence) {
        flushParagraph();
        closeList();
        if (code) {
          const pre = node("pre", "markdown-code-block");
          const codeNode = node("code", fence[1] ? `language-${fence[1].trim()}` : "", code.lines.join("\n"));
          pre.append(codeNode);
          container.append(pre);
          code = null;
        } else {
          code = { language: fence[1].trim(), lines: [] };
        }
        return;
      }
      if (code) {
        code.lines.push(line);
        return;
      }
      const heading = line.match(/^(#{1,4})\s+(.+)$/);
      if (heading) {
        flushParagraph();
        closeList();
        const title = node(`h${heading[1].length}`);
        appendInlineMarkdown(title, heading[2]);
        container.append(title);
        return;
      }
      const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (unordered || ordered) {
        flushParagraph();
        const tag = ordered ? "ol" : "ul";
        if (!list || list.tagName.toLowerCase() !== tag) {
          closeList();
          list = node(tag);
        }
        const item = node("li");
        appendInlineMarkdown(item, (ordered || unordered)[1]);
        list.append(item);
        return;
      }
      if (/^\s*>\s?/.test(line)) {
        flushParagraph();
        closeList();
        const quote = node("blockquote");
        appendInlineMarkdown(quote, line.replace(/^\s*>\s?/, ""));
        container.append(quote);
        return;
      }
      if (/^\s*(-{3,}|_{3,}|\*{3,})\s*$/.test(line)) {
        flushParagraph();
        closeList();
        container.append(node("hr"));
        return;
      }
      if (!line.trim()) {
        flushParagraph();
        closeList();
        return;
      }
      closeList();
      paragraph.push(line);
    });
    flushParagraph();
    closeList();
    if (code) {
      const pre = node("pre", "markdown-code-block");
      pre.append(node("code", code.language ? `language-${code.language}` : "", code.lines.join("\n")));
      container.append(pre);
    }
  }

  function appendInlineMarkdown(parent, text) {
    const pattern = /(!?\[[^\]]*\]\([^)]+\)|`[^`]+`|\*\*[^*]+\*\*|__[^_]+__)/g;
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      parent.append(document.createTextNode(text.slice(cursor, match.index)));
      const token = match[0];
      const link = token.match(/^(!?)\[([^\]]*)\]\(([^)]+)\)$/);
      if (link) {
        const [, imageMarker, label, targetValue] = link;
        const target = targetValue.trim();
        if (imageMarker) {
          const placeholder = node("span", "markdown-local-image", `正在加载图片：${label || fileName(target)}`);
          placeholder.dataset.target = target;
          placeholder.dataset.alt = label;
          parent.append(placeholder);
        } else if (/^https?:\/\//i.test(target)) {
          const anchor = node("a", "", label || target);
          anchor.href = target;
          anchor.target = "_blank";
          anchor.rel = "noopener noreferrer";
          parent.append(anchor);
        } else {
          const fileLink = node("button", "markdown-file-link", label || fileName(target));
          fileLink.type = "button";
          fileLink.dataset.target = target;
          parent.append(fileLink);
        }
      } else if (token.startsWith("`")) {
        parent.append(node("code", "markdown-inline-code", token.slice(1, -1)));
      } else {
        parent.append(node("strong", "", token.slice(2, -2)));
      }
      cursor = (match.index || 0) + token.length;
    }
    parent.append(document.createTextNode(text.slice(cursor)));
  }

  async function renderArtifacts(run, markdownRoot, container) {
    try {
      const files = await loadRunFiles(run.id);
      if (!markdownRoot.isConnected) return;
      const embedded = await hydrateMarkdownFiles(run, markdownRoot, files);
      await renderFilePreviews(run, container, files.filter((file) => !embedded.has(file.id)));
    } catch (error) {
      if (container.isConnected) container.append(node("div", "file-preview-error", error.message));
    }
  }

  async function hydrateMarkdownFiles(run, root, files) {
    const embedded = new Set();
    for (const placeholder of root.querySelectorAll(".markdown-local-image")) {
      const file = referencedFile(files, placeholder.dataset.target);
      if (!file || file.kind !== "image" || !file.available) continue;
      const blob = await cachedApiBlob(`/runs/${run.id}/files/${file.id}`);
      if (!placeholder.isConnected) continue;
      const url = URL.createObjectURL(blob);
      state.objectUrls.push(url);
      const figure = node("figure", "markdown-image");
      const image = node("img");
      image.src = url;
      image.alt = placeholder.dataset.alt || file.name;
      image.loading = "lazy";
      figure.append(image, node("figcaption", "", file.name));
      placeholder.replaceWith(figure);
      embedded.add(file.id);
    }
    for (const link of root.querySelectorAll(".markdown-file-link")) {
      const file = referencedFile(files, link.dataset.target);
      if (!file) {
        link.disabled = true;
        continue;
      }
      if (file.kind === "image" && file.available) {
        const blob = await cachedApiBlob(`/runs/${run.id}/files/${file.id}`);
        if (!link.isConnected) continue;
        const url = URL.createObjectURL(blob);
        state.objectUrls.push(url);
        const figure = node("figure", "markdown-image");
        const image = node("img");
        image.src = url;
        image.alt = link.textContent || file.name;
        image.loading = "lazy";
        figure.append(image, node("figcaption", "", file.name));
        link.replaceWith(figure);
        embedded.add(file.id);
        continue;
      }
      link.title = `下载 ${file.path}`;
      link.addEventListener("click", () => downloadFile(run.id, file));
    }
    return embedded;
  }

  function referencedFile(files, rawTarget = "") {
    let target = rawTarget.replace(/^file:\/\//, "").split(/[?#]/, 1)[0].replace(/^\.\//, "");
    try {
      target = decodeURIComponent(target);
    } catch (_) {
      // Keep the literal target when it is not URI encoded.
    }
    const exact = files.find((file) => file.path === target || target.endsWith(`/${file.path}`));
    if (exact) return exact;
    const name = fileName(target);
    const matches = files.filter((file) => file.name === name);
    return matches.length === 1 ? matches[0] : null;
  }

  function fileName(path) {
    return path.split(/[\\/]/).at(-1) || path;
  }

  function loadRunFiles(runId) {
    if (!state.fileCache.has(runId)) state.fileCache.set(runId, api(`/runs/${runId}/files`));
    return state.fileCache.get(runId);
  }

  async function renderFilePreviews(run, container, discoveredFiles = null) {
    try {
      const files = discoveredFiles || (await loadRunFiles(run.id));
      if (!container.isConnected || !files.length) {
        container.remove();
        return;
      }
      container.append(node("div", "file-previews-title", `文件预览 · ${files.length}`));
      for (const file of files) container.append(await filePreviewCard(run, file));
    } catch (error) {
      if (container.isConnected) container.append(node("div", "file-preview-error", error.message));
    }
  }

  function runPresentationSignature(value) {
    const runs = Array.isArray(value) ? value : [value];
    return JSON.stringify(
      runs.map((run) => ({
        id: run.id,
        agent: run.agent,
        status: run.status,
        permission_mode: run.permission_mode,
        model: run.model,
        reasoning_effort: run.reasoning_effort,
        prompt: run.prompt,
        plan: run.plan,
        result: run.result,
        error: run.error,
        completed_at: run.completed_at,
      })),
    );
  }

  function conversationContextSignature(conversation) {
    if (!conversation) return "";
    return JSON.stringify({
      id: conversation.id,
      workspace: conversation.workspace,
      project_name: conversation.project_name,
      project_selected: conversation.project_selected,
      active_agent: conversation.active_agent,
    });
  }

  function refreshExpandedProcessPanels() {
    elements.messageStream.querySelectorAll(".process-panel[open]").forEach((details) => {
      const timeline = details.querySelector(".process-timeline");
      if (details.dataset.runId && timeline) refreshRunEvents(details.dataset.runId, timeline);
    });
  }

  async function filePreviewCard(run, file) {
    const card = node("article", "file-preview-card");
    const header = node("div", "file-preview-header");
    const copy = node("div", "file-preview-copy");
    copy.append(node("strong", "", file.name), node("span", "", `${file.path} · ${formatBytes(file.size)}`));
    const download = node("button", "file-download", "下载");
    download.type = "button";
    download.disabled = !file.available;
    download.addEventListener("click", () => downloadFile(run.id, file));
    header.append(node("span", `file-kind ${file.kind}`, fileKindLabel(file.kind)), copy, download);
    card.append(header);
    if (!file.available) {
      card.append(node("div", "file-preview-message", "文件过大，无法在线预览。"));
      return card;
    }
    if (file.kind === "file") {
      card.append(node("div", "file-preview-message", "此格式不能直接显示，可下载后使用本地应用打开。"));
      return card;
    }
    const body = node("div", `file-preview-body ${file.kind}`);
    body.append(node("div", "file-preview-loading", "正在加载预览…"));
    card.append(body);
    try {
      const blobPath = `/runs/${run.id}/files/${file.id}`;
      const blob = await cachedApiBlob(blobPath);
      body.replaceChildren();
      if (file.kind === "text") {
        const textContent = await blob.text();
        body.append(node("pre", "file-text-preview", textContent.slice(0, 200000)));
        if (textContent.length > 200000) body.append(node("div", "file-preview-message", "预览已截断，请下载查看全文。"));
      } else {
        const url = URL.createObjectURL(blob);
        state.objectUrls.push(url);
        if (file.kind === "image") {
          const image = node("img", "file-image-preview");
          image.src = url;
          image.alt = file.name;
          image.loading = "lazy";
          body.append(image);
        } else if (file.kind === "pdf") {
          const frame = node("iframe", "file-pdf-preview");
          frame.src = url;
          frame.title = `${file.name} 预览`;
          body.append(frame);
        } else {
          const media = node(file.kind, `file-${file.kind}-preview`);
          media.src = url;
          media.controls = true;
          media.preload = "metadata";
          body.append(media);
        }
      }
    } catch (error) {
      body.replaceChildren(node("div", "file-preview-error", error.message));
    }
    return card;
  }

  async function downloadFile(runId, file) {
    try {
      const blob = await cachedApiBlob(`/runs/${runId}/files/${file.id}`);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = file.name;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function refreshSidebarOnly() {
    try {
      const bootstrap = await api("/bootstrap");
      state.bootstrap = bootstrap;
      const current = bootstrap.conversations.find((item) => item.id === state.activeConversation?.id);
      if (current) state.activeConversation = current;
      renderConversationList();
      renderProjectList();
      renderContext();
    } catch (_) {
      // Polling the active run remains the primary path.
    }
  }

  async function selectProject(project) {
    await createConversation(project.path);
  }

  async function selectGeneralConversation() {
    await createConversation(null);
  }

  async function createConversation(workspace) {
    try {
      const conversation = await api("/conversations", {
        method: "POST",
        body: JSON.stringify({ workspace, agent: state.selectedAgent }),
      });
      if (elements.projectDialog.open) elements.projectDialog.close();
      await loadBootstrap({ preserveConversation: false });
      const created = state.bootstrap.conversations.find((item) => item.id === conversation.id);
      if (created && state.activeConversation?.id !== created.id) await openConversation(created, { activate: false });
      elements.composerInput.focus();
      return created || conversation;
    } catch (error) {
      showToast(error.message);
      return null;
    }
  }

  function startNewTask() {
    stopPolling();
    state.activeConversation = null;
    state.runs = [];
    closeSidebar();
    renderContext();
    renderConversationList();
    renderMessages();
    if (hasAnyActiveRun()) startPolling();
    elements.composerInput.focus();
  }

  function renderProjectList() {
    const projects = state.bootstrap?.projects || [];
    const query = elements.projectSearch.value.trim().toLowerCase();
    elements.generalConversationButton.classList.toggle("hidden", Boolean(query));
    const visible = projects.filter((project) => `${project.name} ${project.path}`.toLowerCase().includes(query));
    elements.projectList.replaceChildren();
    let previousGroup = null;
    visible.forEach((project) => {
      if (project.group !== previousGroup) {
        elements.projectList.append(node("div", "project-group", project.group));
        previousGroup = project.group;
      }
      const item = node("button", "project-item");
      item.type = "button";
      item.append(node("span", "project-badge", project.name.slice(0, 2).toUpperCase()));
      const copy = node("span", "project-copy");
      copy.append(node("strong", "", project.name), node("span", "", project.path));
      item.append(copy);
      item.addEventListener("click", () => selectProject(project));
      elements.projectList.append(item);
    });
    if (!visible.length) elements.projectList.append(node("div", "sidebar-empty", "没有匹配的项目"));
  }

  function openProjectDialog() {
    if (!state.bootstrap) return;
    elements.projectSearch.value = "";
    renderProjectList();
    elements.projectDialog.showModal();
    window.setTimeout(() => elements.projectSearch.focus(), 30);
  }

  function openPermissionDialog() {
    if (!state.activeConversation?.project_selected) {
      showToast("请先选择项目，再调整操作权限");
      return;
    }
    elements.fullAccessConfirm.classList.add("hidden");
    renderPermissionOptions();
    elements.permissionDialog.showModal();
  }

  function renderPermissionOptions() {
    elements.permissionOptions.forEach((button) => {
      const selected = button.dataset.permission === state.selectedPermission;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-checked", String(selected));
    });
  }

  function choosePermission(permission) {
    if (permission === "full_access") {
      elements.fullAccessConfirm.classList.remove("hidden");
      elements.fullAccessConfirm.scrollIntoView({ behavior: "smooth", block: "nearest" });
      return;
    }
    applyPermission(permission);
  }

  function applyPermission(permission) {
    state.selectedPermission = permission;
    persistPermissionMode(permission);
    elements.fullAccessConfirm.classList.add("hidden");
    if (elements.permissionDialog.open) elements.permissionDialog.close();
    renderContext();
  }

  async function switchAgent(agent) {
    state.selectedAgent = agent;
    if (!state.activeConversation) {
      renderContext();
      return;
    }
    try {
      const conversation = await api(`/conversations/${state.activeConversation.id}/agent`, {
        method: "POST",
        body: JSON.stringify({ agent }),
      });
      state.activeConversation = { ...state.activeConversation, ...conversation };
      const listItem = state.bootstrap.conversations.find((item) => item.id === conversation.id);
      if (listItem) Object.assign(listItem, conversation);
      renderContext();
      renderConversationList();
    } catch (error) {
      state.selectedAgent = state.activeConversation.active_agent;
      renderContext();
      showToast(error.message);
    }
  }

  function updateComposerState() {
    const hasText = Boolean(elements.composerInput.value.trim());
    const hasActiveRun = state.runs.some((run) => isActiveStatus(run.status));
    elements.composerInput.disabled = false;
    elements.composerInput.placeholder = hasActiveRun
      ? "继续发送，消息会排队处理"
      : state.activeConversation?.project_selected
        ? "描述任务，或询问有关代码的问题"
        : "直接提问，或先选择项目处理代码";
    elements.sendButton.disabled = !hasText;
  }

  function resizeComposer() {
    elements.composerInput.style.height = "auto";
    elements.composerInput.style.height = `${Math.min(elements.composerInput.scrollHeight, 180)}px`;
    updateComposerState();
  }

  function scrollToBottom() {
    elements.conversationView.scrollTop = elements.conversationView.scrollHeight;
  }

  function openSidebar() {
    elements.app.classList.add("sidebar-open");
  }

  function closeSidebar() {
    elements.app.classList.remove("sidebar-open");
  }

  function showToast(message) {
    const toast = node("div", "toast", message);
    elements.toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), 4200);
  }

  function node(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
  }

  function relativeTime(timestamp) {
    if (!timestamp) return "";
    const seconds = Math.max(0, Date.now() / 1000 - timestamp);
    if (seconds < 60) return "刚刚";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时`;
    return new Date(timestamp * 1000).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
  }

  function agentLabel(agent) {
    return agent === "claude" ? "Claude" : "Codex";
  }

  function permissionLabel(permission) {
    return {
      request_approval: "请求批准",
      workspace_auto: "工作区自动",
      full_access: "完全访问",
    }[permission] || "请求批准";
  }

  function reasoningLabel(effort) {
    return {
      low: "低",
      medium: "中",
      high: "高",
      xhigh: "很高",
      max: "最大",
      ultra: "Ultra",
    }[effort] || effort;
  }

  function activityLabel(status) {
    return {
      thinking: "正在思考",
      working: "正在处理",
      executing: "正在执行",
      warning: "运行提醒",
      error: "运行错误",
    }[status] || status || "状态更新";
  }

  function fileKindLabel(kind) {
    return { image: "图片", text: "文本", pdf: "PDF", audio: "音频", video: "视频", file: "文件" }[kind] || "文件";
  }

  function formatBytes(value) {
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }

  function isActiveStatus(status) {
    return ["queued", "planning", "awaiting_approval", "running", "cancel_requested"].includes(status);
  }

  function statusLabel(status, general = false, permission = "request_approval") {
    return {
      queued: "正在准备任务",
      planning: general ? "正在思考" : "正在阅读项目并分析",
      awaiting_approval: "等待批准",
      running: permission === "request_approval" ? "正在执行已批准的计划" : "正在按当前权限自动执行计划",
      cancel_requested: "正在停止",
      failed: "任务失败",
      timed_out: "任务超时",
      interrupted: "任务因服务重启中断",
    }[status] || status;
  }

  function bindEvents() {
    elements.newTaskButton.addEventListener("click", startNewTask);
    elements.projectButton.addEventListener("click", openProjectDialog);
    elements.emptyProjectButton.addEventListener("click", openProjectDialog);
    elements.projectSearch.addEventListener("input", renderProjectList);
    elements.generalConversationButton.addEventListener("click", selectGeneralConversation);
    elements.permissionButton.addEventListener("click", openPermissionDialog);
    elements.modelSelect.addEventListener("change", () => {
      const agent = state.activeConversation?.active_agent || state.selectedAgent;
      state.selectedModels[agent] = elements.modelSelect.value;
      persistRunSetting("model", agent, elements.modelSelect.value);
    });
    elements.effortSelect.addEventListener("change", () => {
      const agent = state.activeConversation?.active_agent || state.selectedAgent;
      state.selectedEfforts[agent] = elements.effortSelect.value;
      persistRunSetting("effort", agent, elements.effortSelect.value);
    });
    elements.permissionOptions.forEach((button) => {
      button.addEventListener("click", () => choosePermission(button.dataset.permission));
    });
    elements.cancelFullAccess.addEventListener("click", () => elements.fullAccessConfirm.classList.add("hidden"));
    elements.confirmFullAccess.addEventListener("click", () => applyPermission("full_access"));
    elements.taskSearch.addEventListener("input", () => {
      state.taskFilter = elements.taskSearch.value;
      renderConversationList();
    });
    elements.refreshButton.addEventListener("click", () => loadBootstrap());
    elements.agentOptions.forEach((button) => {
      button.addEventListener("click", () => switchAgent(button.dataset.agent));
    });
    elements.composerForm.addEventListener("submit", (event) => {
      event.preventDefault();
      sendMessage(elements.composerInput.value);
    });
    elements.composerInput.addEventListener("input", resizeComposer);
    elements.composerInput.addEventListener("focus", () => {
      elements.app.classList.add("composer-focused");
      syncViewportHeight();
      window.setTimeout(syncViewportHeight, 120);
      window.setTimeout(scrollToBottom, 160);
    });
    elements.composerInput.addEventListener("blur", () => {
      elements.app.classList.remove("composer-focused", "keyboard-visible");
      window.setTimeout(syncViewportHeight, 80);
    });
    elements.composerInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        elements.composerForm.requestSubmit();
      }
    });
    document.querySelector("[data-close-dialog]").addEventListener("click", () => elements.projectDialog.close());
    document
      .querySelector("[data-close-permission]")
      .addEventListener("click", () => elements.permissionDialog.close());
    elements.openSidebar.addEventListener("click", openSidebar);
    elements.closeSidebar.addEventListener("click", closeSidebar);
    elements.sidebarBackdrop.addEventListener("click", closeSidebar);
    document.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        startNewTask();
      }
      if (event.key === "Escape") closeSidebar();
    });
  }

  configureTelegram();
  configureViewport();
  bindEvents();
  loadBootstrap();
})();
