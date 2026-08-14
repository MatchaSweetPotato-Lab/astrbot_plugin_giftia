// Giftia Dashboard Modals - Bot Status & Task Board Actions

// 1. Fill energy
window.fillEnergy = async function(bot, group) {
    bot = decodeURIComponent(bot);
    group = decodeURIComponent(group);
    try {
        const res = await window.apiPost("/status/fill_energy", {
            bot_name: bot,
            group_or_user_id: group
        });
        if (res.status === "success") {
            window.showToast(`成功为 ${bot} 充满能量！`);
            window.GiftiaApp.loadBotStatus();
        } else {
            window.showToast(`补充能量失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

// 2. Edit Bot Status
window.openEditStatusModal = function(bot, group, mood, state, memory, action) {
    bot = decodeURIComponent(bot);
    group = decodeURIComponent(group);
    mood = decodeURIComponent(mood || "");
    state = decodeURIComponent(state || "");
    memory = decodeURIComponent(memory || "");
    action = decodeURIComponent(action || "");
    document.getElementById("edit-status-bot").value = bot;
    document.getElementById("edit-status-group").value = group;
    document.getElementById("edit-status-mood").value = mood;
    document.getElementById("edit-status-state").value = state;
    document.getElementById("edit-status-memory").value = memory;
    document.getElementById("edit-status-action").value = action;
    window.openModal("edit-status-modal");
};

window.submitEditStatus = async function() {
    const bot = document.getElementById("edit-status-bot").value;
    const group = document.getElementById("edit-status-group").value;
    const mood = document.getElementById("edit-status-mood").value.trim();
    const state = document.getElementById("edit-status-state").value.trim();
    const memory = document.getElementById("edit-status-memory").value.trim();
    const action = document.getElementById("edit-status-action").value.trim();

    try {
        const res = await window.apiPost("/status/update", {
            bot_name: bot,
            group_or_user_id: group,
            mood: mood,
            state: state,
            memory: memory,
            action: action
        });
        if (res.status === "success") {
            window.showToast("Bot 状态调整成功！");
            window.closeModal("edit-status-modal");
            window.GiftiaApp.loadBotStatus();
        } else {
            window.showToast(`修改失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

// 3. Short Task Board
const TASK_BOARD_STATUS_LABELS = {
    active: "活跃",
    completed: "完成",
    canceled: "取消",
    expired: "过期"
};

function formatTaskDateInput(value) {
    if (!value) return "";
    return String(value).replace(" ", "T").slice(0, 16);
}

window.taskBoardMainTab = "short";
window.taskBoardActiveTab = "active";
window.taskBoardCachedData = null;
window.scheduledTasksCachedData = null;

window.setTaskBoardMainTab = function(mainTab) {
    window.taskBoardMainTab = mainTab;
    document.querySelectorAll(".task-board-main-tab").forEach(btn => {
        if (btn.getAttribute("data-main-tab") === mainTab) {
            btn.classList.add("active");
        } else {
            btn.classList.remove("active");
        }
    });

    const shortPanel = document.getElementById("short-task-board-panel");
    const scheduledPanel = document.getElementById("scheduled-task-board-panel");

    if (mainTab === "short") {
        if (shortPanel) shortPanel.style.display = "block";
        if (scheduledPanel) scheduledPanel.style.display = "none";
    } else {
        if (shortPanel) shortPanel.style.display = "none";
        if (scheduledPanel) scheduledPanel.style.display = "block";
        if (!window.scheduledTasksCachedData) {
            window.refreshScheduledTasks();
        }
    }
    window.updateClearButtonText();
};

window.setTaskBoardTab = function(tab) {
    window.taskBoardActiveTab = tab;
    document.querySelectorAll(".task-board-tab").forEach(btn => {
        if (btn.getAttribute("data-tab") === tab) {
            btn.classList.add("active");
        } else {
            btn.classList.remove("active");
        }
    });
    if (window.taskBoardCachedData) {
        window.renderTaskListOnly(window.taskBoardCachedData);
    }
    window.updateClearButtonText();
};

window.updateClearButtonText = function() {
    const clearTextEl = document.getElementById("task-board-clear-text");
    if (!clearTextEl) return;

    if (window.taskBoardMainTab === "scheduled") {
        clearTextEl.textContent = "清空所有定时任务";
    } else {
        const tab = window.taskBoardActiveTab || "active";
        if (tab === "active") clearTextEl.textContent = "清空进行中任务";
        else if (tab === "completed") clearTextEl.textContent = "清空已完成任务";
        else if (tab === "archived") clearTextEl.textContent = "清空已失效任务";
        else clearTextEl.textContent = "清空全部短期任务";
    }
};

window.openTaskBoardModal = async function(bot, group) {
    bot = decodeURIComponent(bot);
    group = decodeURIComponent(group);
    document.getElementById("task-board-bot").value = bot;
    document.getElementById("task-board-group").value = group;
    document.getElementById("task-board-title").textContent = `任务看板 · ${bot}`;
    
    // Reset active tabs on open
    window.taskBoardMainTab = "short";
    window.taskBoardActiveTab = "active";
    document.querySelectorAll(".task-board-main-tab").forEach(btn => {
        if (btn.getAttribute("data-main-tab") === "short") btn.classList.add("active");
        else btn.classList.remove("active");
    });
    document.querySelectorAll(".task-board-tab").forEach(btn => {
        if (btn.getAttribute("data-tab") === "active") btn.classList.add("active");
        else btn.classList.remove("active");
    });

    const shortPanel = document.getElementById("short-task-board-panel");
    const scheduledPanel = document.getElementById("scheduled-task-board-panel");
    if (shortPanel) shortPanel.style.display = "block";
    if (scheduledPanel) scheduledPanel.style.display = "none";

    document.getElementById("task-board-list").innerHTML = `<div class="loading-row flex-grow"><span class="loader"></span> 加载任务中...</div>`;
    document.getElementById("scheduled-task-list").innerHTML = `<div class="loading-row flex-grow"><span class="loader"></span> 加载定时任务中...</div>`;

    window.updateClearButtonText();
    window.openModal("task-board-modal");
    await window.refreshTaskBoardModal();
};

window.refreshTaskBoardModal = async function() {
    const bot = document.getElementById("task-board-bot").value;
    const group = document.getElementById("task-board-group").value;
    const shortList = document.getElementById("task-board-list");
    const scheduledList = document.getElementById("scheduled-task-list");

    if (!bot || !group) {
        if (shortList) shortList.innerHTML = `<div class="task-board-empty">未选择会话</div>`;
        if (scheduledList) scheduledList.innerHTML = `<div class="task-board-empty">未选择会话</div>`;
        return;
    }

    try {
        const [shortRes] = await Promise.all([
            window.apiGet("/task_board", { bot_name: bot, group_or_user_id: group }),
            window.fetchAndRenderScheduledTasks(bot, group, scheduledList)
        ]);

        if (shortRes.status === "success" && shortRes.data) {
            window.renderTaskBoardModal(shortRes.data);
        } else if (shortList) {
            shortList.innerHTML = `<div class="task-board-empty">加载短期任务失败: ${window.escapeHtml(shortRes.message || "")}</div>`;
        }
    } catch (e) {
        if (shortList) shortList.innerHTML = `<div class="task-board-empty">加载失败: ${window.escapeHtml(e.message)}</div>`;
        if (scheduledList) scheduledList.innerHTML = `<div class="task-board-empty">加载失败: ${window.escapeHtml(e.message)}</div>`;
    }
};

function updateTabCount(selector, count) {
    const tab = document.querySelector(selector);
    if (!tab) return;
    const countBadge = tab.querySelector('.task-tab-count');
    if (countBadge) {
        countBadge.textContent = count;
    } else {
        const span = document.createElement('span');
        span.className = 'task-tab-count';
        span.textContent = count;
        tab.appendChild(span);
    }
}

window.fetchAndRenderScheduledTasks = async function(bot, group, scheduledList) {
    scheduledList = scheduledList || document.getElementById("scheduled-task-list");
    if (!bot || !group || !scheduledList) return;
    try {
        const schedRes = await window.apiGet("/scheduled_tasks", { bot_name: bot, group_or_user_id: group });
        if (schedRes.status === "success" && schedRes.data) {
            window.scheduledTasksCachedData = schedRes.data;
            const items = schedRes.data.items || [];
            window.renderScheduledTaskList(items);
            updateTabCount('.task-board-main-tab[data-main-tab="scheduled"]', items.length);
        } else {
            scheduledList.innerHTML = `<div class="task-board-empty">加载定时任务失败: ${window.escapeHtml(schedRes.message || "")}</div>`;
        }
    } catch (e) {
        scheduledList.innerHTML = `<div class="task-board-empty">加载定时任务失败: ${window.escapeHtml(e.message)}</div>`;
    }
};

window.refreshScheduledTasks = async function() {
    const bot = document.getElementById("task-board-bot").value;
    const group = document.getElementById("task-board-group").value;
    await window.fetchAndRenderScheduledTasks(bot, group);
};

window.renderTaskBoardModal = function(data) {
    window.taskBoardCachedData = data;
    const stats = data.stats || {};
    const limit = data.limit || 0;

    const activeCount = stats.active || 0;
    const completedCount = stats.completed || 0;
    const archivedCount = (stats.canceled || 0) + (stats.expired || 0);
    const totalCount = stats.total || 0;

    updateTabCount('.task-board-main-tab[data-main-tab="short"]', activeCount);

    const tabElements = document.querySelectorAll(".task-board-tab");
    tabElements.forEach(tab => {
        const tabType = tab.getAttribute("data-tab");
        let baseText = "";
        let svgHtml = "";
        if (tabType === "active") {
            baseText = "进行中";
            svgHtml = `<svg class="tab-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle></svg>`;
            tab.innerHTML = `${svgHtml} <span class="task-tab-title">${baseText}</span> <span class="task-tab-count">${activeCount}/${limit}</span>`;
        } else if (tabType === "completed") {
            baseText = "已完成";
            svgHtml = `<svg class="tab-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>`;
            tab.innerHTML = `${svgHtml} <span class="task-tab-title">${baseText}</span> <span class="task-tab-count">${completedCount}</span>`;
        } else if (tabType === "archived") {
            baseText = "已失效";
            svgHtml = `<svg class="tab-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><line x1="9" y1="9" x2="15" y2="9"></line><line x1="9" y1="13" x2="15" y2="13"></line><line x1="9" y1="17" x2="15" y2="17"></line></svg>`;
            tab.innerHTML = `${svgHtml} <span class="task-tab-title">${baseText}</span> <span class="task-tab-count">${archivedCount}</span>`;
        } else if (tabType === "all") {
            baseText = "全部任务";
            svgHtml = `<svg class="tab-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"></line><line x1="8" y1="12" x2="21" y2="12"></line><line x1="8" y1="18" x2="21" y2="18"></line><line x1="3" y1="6" x2="3.01" y2="6"></line><line x1="3" y1="12" x2="3.01" y2="12"></line><line x1="3" y1="18" x2="3.01" y2="18"></line></svg>`;
            tab.innerHTML = `${svgHtml} <span class="task-tab-title">${baseText}</span> <span class="task-tab-count">${totalCount}</span>`;
        }
    });

    window.renderTaskListOnly(data);
};

window.renderTaskListOnly = function(data) {
    const list = document.getElementById("task-board-list");
    const items = data.items || [];
    const activeTab = window.taskBoardActiveTab || "active";
    const statuses = ["active", "completed", "canceled", "expired"];

    let filteredItems = [];
    if (activeTab === "active") {
        filteredItems = items.filter(item => item.status === "active");
    } else if (activeTab === "completed") {
        filteredItems = items.filter(item => item.status === "completed");
    } else if (activeTab === "archived") {
        filteredItems = items.filter(item => item.status === "canceled" || item.status === "expired");
    } else {
        filteredItems = items;
    }

    if (filteredItems.length === 0) {
        let emptyTitle = "暂无任务";
        let emptyDesc = "当前分类下没有任何短期任务";
        if (activeTab === "active") {
            emptyTitle = "太棒了，暂无待办任务";
            emptyDesc = "当前没有任何进行中的短期任务，您可以让 Bot 自动帮您规划或在此记录新任务。";
        } else if (activeTab === "completed") {
            emptyTitle = "暂无已完成任务";
            emptyDesc = "已完成的短期任务记录将会归档在此处。";
        } else if (activeTab === "archived") {
            emptyTitle = "暂无失效任务";
            emptyDesc = "被取消或已过期的短期任务记录会归档在此处。";
        }
        
        list.innerHTML = `
            <div class="task-board-empty-state">
                <svg class="task-board-empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
                </svg>
                <div class="task-board-empty-title">${emptyTitle}</div>
                <div class="task-board-empty-desc">${emptyDesc}</div>
            </div>
        `;
        return;
    }

    list.innerHTML = filteredItems.map(task => {
        const taskIdArg = encodeURIComponent(task.task_id || "");
        const options = statuses.map(option => {
            const selected = option === task.status ? "selected" : "";
            return `<option value="${option}" ${selected}>${TASK_BOARD_STATUS_LABELS[option]}</option>`;
        }).join("");
        const creator = task.creator_nickname || task.creator_user_id || "未知";
        
        let badgeClass = "badge-secondary";
        if (task.status === "active") badgeClass = "badge-info";
        else if (task.status === "completed") badgeClass = "badge-success";
        else if (task.status === "canceled") badgeClass = "badge-danger";
        else if (task.status === "expired") badgeClass = "badge-warning";
        
        return `
            <div class="task-board-card short-task-card" data-task-id="${window.escapeHtml(task.task_id || "")}">
                <div class="task-card-header">
                    <div class="task-card-status-badge">
                        <span class="badge ${badgeClass}">${TASK_BOARD_STATUS_LABELS[task.status] || task.status}</span>
                    </div>
                    <div class="task-card-meta-right">
                        <span>ID: ${window.escapeHtml(task.task_id || "")}</span>
                        <span>创建人: ${window.escapeHtml(creator)}</span>
                    </div>
                </div>
                <div class="task-card-body">
                    <textarea class="task-card-textarea short-task-content-input" placeholder="任务内容...">${window.escapeHtml(task.content || "")}</textarea>
                </div>
                <div class="task-card-footer">
                    <div class="task-card-footer-left">
                        <div class="task-card-control-group">
                            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"></path><path d="M12 6v6l4 2"></path></svg>
                            状态:
                            <select class="task-card-select short-task-status-select">
                                ${options}
                            </select>
                        </div>
                        <div class="task-card-control-group">
                            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>
                            截止:
                            <input class="task-card-input-date short-task-expires-input" type="datetime-local" value="${window.escapeHtml(formatTaskDateInput(task.expires_at))}">
                        </div>
                        <div class="task-card-dates">
                            <span>创建: ${window.formatDate(task.created_at)}</span>
                            <span>更新: ${window.formatDate(task.updated_at)}</span>
                        </div>
                        ${task.close_reason ? `<div class="task-card-reason" title="${window.escapeHtml(task.close_reason)}">失效原因: ${window.escapeHtml(task.close_reason)}</div>` : ""}
                    </div>
                    <div class="task-card-actions">
                        <button class="task-card-btn task-card-btn-primary" onclick="window.saveTaskBoardItem(this, '${taskIdArg}')">
                            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg>
                            保存
                        </button>
                        <button class="task-card-btn task-card-btn-danger" onclick="window.deleteTaskBoardItem('${taskIdArg}')">
                            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>
                            删除
                        </button>
                    </div>
                </div>
            </div>
        `;
    }).join("");
};

window.renderScheduledTaskList = function(items) {
    const list = document.getElementById("scheduled-task-list");
    if (!list) return;

    if (!items || items.length === 0) {
        list.innerHTML = `
            <div class="task-board-empty-state">
                <svg class="task-board-empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                    <circle cx="12" cy="12" r="10"></circle>
                    <polyline points="12 6 12 12 16 14"></polyline>
                </svg>
                <div class="task-board-empty-title">暂无定时任务</div>
                <div class="task-board-empty-desc">当前会话没有任何正在排队的定时提醒或定时任务。</div>
            </div>
        `;
        return;
    }

    list.innerHTML = items.map(job => {
        const taskIdArg = encodeURIComponent(job.task_id || "");
        const creator = job.user_name || job.user_id || "系统/LLM";
        const contentText = job.remind_message || job.func_name || "";

        return `
            <div class="task-board-card scheduled-task-card" data-task-id="${window.escapeHtml(job.task_id || "")}">
                <div class="task-card-header">
                    <div class="task-card-status-badge">
                        <span class="badge badge-info">定时任务</span>
                    </div>
                    <div class="task-card-meta-right">
                        <span>ID: ${window.escapeHtml(job.task_id || "")}</span>
                        <span>触发发起人: ${window.escapeHtml(creator)}</span>
                    </div>
                </div>
                <div class="task-card-body">
                    <textarea class="task-card-textarea scheduled-task-content-input" placeholder="提醒/任务内容...">${window.escapeHtml(contentText)}</textarea>
                </div>
                <div class="task-card-footer">
                    <div class="task-card-footer-left">
                        <div class="task-card-control-group">
                            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                            时间规则:
                            <input class="task-card-input-text scheduled-time-input" type="text" value="${window.escapeHtml(job.time_expr || '')}" placeholder="Cron (如 0 8 * * *) 或 ISO时间">
                        </div>
                        <div class="task-card-dates">
                            <span>下次执行: ${window.formatDate(job.next_run_time)}</span>
                        </div>
                    </div>
                    <div class="task-card-actions">
                        <button class="task-card-btn task-card-btn-primary" onclick="window.saveScheduledTaskItem(this, '${taskIdArg}')">
                            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg>
                            保存
                        </button>
                        <button class="task-card-btn task-card-btn-danger" onclick="window.deleteScheduledTaskItem('${taskIdArg}')">
                            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>
                            删除
                        </button>
                    </div>
                </div>
            </div>
        `;
    }).join("");
};

function findTaskCard(target, taskIdArg, cardSelector = ".task-board-card") {
    let card = null;
    let taskId = "";
    if (target && target.nodeType) {
        card = target.closest(cardSelector);
        taskId = card ? (card.getAttribute("data-task-id") || "") : decodeURIComponent(taskIdArg || "");
    } else if (typeof target === "string") {
        taskId = decodeURIComponent(target);
        card = document.querySelector(`${cardSelector}[data-task-id="${CSS.escape(taskId)}"]`);
    } else if (typeof target === "number") {
        taskId = decodeURIComponent(taskIdArg || "");
        const legacyId = cardSelector.includes("scheduled") ? `scheduled-task-content-${target}` : `task-board-content-${target}`;
        const legacyEl = document.getElementById(legacyId);
        if (legacyEl) card = legacyEl.closest(cardSelector);
    }
    if (!card && taskId) {
        card = document.querySelector(`${cardSelector}[data-task-id="${CSS.escape(taskId)}"]`);
    }
    return { card, taskId };
}

window.saveScheduledTaskItem = async function(target, taskIdArg) {
    const bot = document.getElementById("task-board-bot").value;
    const group = document.getElementById("task-board-group").value;
    
    const { card, taskId } = findTaskCard(target, taskIdArg, ".scheduled-task-card");
    if (!card) return;

    const contentEl = card.querySelector(".scheduled-task-content-input") || card.querySelector(".task-card-textarea");
    const timeEl = card.querySelector(".scheduled-time-input");
    if (!contentEl || !timeEl) return;

    const remindMessage = contentEl.value.trim();
    const timeExpr = timeEl.value.trim();

    if (!remindMessage) {
        window.showToast("定时任务提醒内容不能为空");
        return;
    }
    if (!timeExpr) {
        window.showToast("时间规则不能为空");
        return;
    }

    try {
        const res = await window.apiPost("/scheduled_tasks/update", {
            bot_name: bot,
            group_or_user_id: group,
            task_id: taskId,
            remind_message: remindMessage,
            time_expr: timeExpr
        });
        if (res.status === "success") {
            window.showToast("定时任务已更新");
            await window.refreshScheduledTasks();
        } else {
            window.showToast(`更新失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

window.saveTaskBoardItem = async function(target, taskIdArg) {
    const bot = document.getElementById("task-board-bot").value;
    const group = document.getElementById("task-board-group").value;
    
    const { card, taskId } = findTaskCard(target, taskIdArg, ".short-task-card");
    if (!card) return;

    const contentEl = card.querySelector(".short-task-content-input") || card.querySelector(".task-card-textarea");
    const statusEl = card.querySelector(".short-task-status-select") || card.querySelector(".task-card-select");
    const expiresEl = card.querySelector(".short-task-expires-input") || card.querySelector(".task-card-input-date");

    const content = contentEl ? contentEl.value.trim() : "";
    const status = statusEl ? statusEl.value : "active";
    const expiresAt = expiresEl ? expiresEl.value : "";

    if (!content) {
        window.showToast("任务内容不能为空");
        return;
    }

    try {
        const res = await window.apiPost("/task_board/update", {
            bot_name: bot,
            group_or_user_id: group,
            task_id: taskId,
            content: content,
            status: status,
            expires_at: expiresAt
        });
        if (res.status === "success") {
            window.showToast("短期任务已更新");
            await window.refreshTaskBoardModal();
            window.GiftiaApp.loadBotStatus();
        } else {
            window.showToast(`更新失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

window.deleteTaskBoardItem = function(taskIdEncoded) {
    const bot = document.getElementById("task-board-bot").value;
    const group = document.getElementById("task-board-group").value;
    const taskId = decodeURIComponent(taskIdEncoded);

    window.showConfirm("确认删除短期任务", "确定要删除这条短期任务吗？此操作无法撤销。", async () => {
        try {
            const res = await window.apiPost("/task_board/delete", {
                bot_name: bot,
                group_or_user_id: group,
                task_id: taskId
            });
            if (res.status === "success") {
                window.showToast("短期任务已删除");
                await window.refreshTaskBoardModal();
                window.GiftiaApp.loadBotStatus();
            } else {
                window.showToast(`删除失败: ${res.message}`);
            }
        } catch (e) {
            window.showToast(`发生错误: ${e.message}`);
        }
    });
};

window.deleteScheduledTaskItem = function(taskIdEncoded) {
    const bot = document.getElementById("task-board-bot").value;
    const group = document.getElementById("task-board-group").value;
    const taskId = decodeURIComponent(taskIdEncoded);

    window.showConfirm("确认删除定时任务", "确定要删除这条定时任务吗？此操作无法撤销。", async () => {
        try {
            const res = await window.apiPost("/scheduled_tasks/delete", {
                bot_name: bot,
                group_or_user_id: group,
                task_id: taskId
            });
            if (res.status === "success") {
                window.showToast("定时任务已删除");
                await window.refreshTaskBoardModal();
            } else {
                window.showToast(`删除失败: ${res.message}`);
            }
        } catch (e) {
            window.showToast(`发生错误: ${e.message}`);
        }
    });
};

window.clearCurrentTaskBoard = function() {
    const bot = document.getElementById("task-board-bot").value;
    const group = document.getElementById("task-board-group").value;

    if (!bot || !group) return;

    if (window.taskBoardMainTab === "scheduled") {
        window.showConfirm("确认清空定时任务", "确定要清空当前会话下的所有定时任务吗？此操作无法撤销。", async () => {
            try {
                const res = await window.apiPost("/scheduled_tasks/clear", {
                    bot_name: bot,
                    group_or_user_id: group
                });
                if (res.status === "success") {
                    window.showToast(res.message || "定时任务已清空");
                    await window.refreshTaskBoardModal();
                } else {
                    window.showToast(`清空失败: ${res.message}`);
                }
            } catch (e) {
                window.showToast(`发生错误: ${e.message}`);
            }
        });
    } else {
        const tab = window.taskBoardActiveTab || "active";
        let statusText = "进行中";
        if (tab === "completed") statusText = "已完成";
        else if (tab === "archived") statusText = "已失效";
        else if (tab === "all") statusText = "全部";

        window.showConfirm(`确认清空短期任务`, `确定要清空当前会话【${statusText}】分类下的所有短期任务吗？此操作无法撤销。`, async () => {
            try {
                const res = await window.apiPost("/task_board/clear", {
                    bot_name: bot,
                    group_or_user_id: group,
                    status: tab
                });
                if (res.status === "success") {
                    window.showToast(res.message || "短期任务已清空");
                    await window.refreshTaskBoardModal();
                    window.GiftiaApp.loadBotStatus();
                } else {
                    window.showToast(`清空失败: ${res.message}`);
                }
            } catch (e) {
                window.showToast(`发生错误: ${e.message}`);
            }
        });
    }
};
