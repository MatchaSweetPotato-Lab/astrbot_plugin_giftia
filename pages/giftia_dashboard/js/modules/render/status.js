export async function loadBotStatus() {
    const container = document.getElementById("status-grid");
    container.innerHTML = `<div class="loading-row flex-grow"><span class="loader"></span> 加载状态中...</div>`;

    try {
        const res = await window.apiGet("/status");
        if (res.status === "success" && res.data) {
            renderBotStatus(res.data);
        } else {
            throw new Error(res.message || "请求失败");
        }
    } catch (e) {
        container.innerHTML = `<div class="no-data-row flex-grow">加载状态失败: ${e.message}</div>`;
    }
}

export function renderBotStatus(items) {
    const container = document.getElementById("status-grid");
    if (!items || items.length === 0) {
        container.innerHTML = `<div class="no-data-row flex-grow">目前暂无活动的会话状态，请让机器人先和群友聊天试试吧！</div>`;
        return;
    }

    container.innerHTML = items.map(item => {
        const encodeStatusArg = value => encodeURIComponent(value || "").replace(/'/g, "%27");
        const energy = Math.max(0, Math.min(100, parseFloat(item.energy) || 0));
        const energyClass = energy < 20 ? "low-energy" : "";
        const mood = item.mood || "平静";
        const state = item.state || "发呆";
        const memory = item.memory || "无";
        const action = item.action || "无";
        const botArg = encodeStatusArg(item.bot_name);
        const groupArg = encodeStatusArg(item.group_or_user_id);
        const taskBoard = item.task_board || null;
        const taskStats = taskBoard?.stats || {};
        const taskLimit = taskBoard?.limit || 0;
        const showTaskBoard = taskBoard?.enabled && (taskLimit > 0 || (taskStats.total || 0) > 0);
        const taskBoardHtml = showTaskBoard ? `
                <button class="status-task-board" onclick="window.openTaskBoardModal('${botArg}', '${groupArg}')" title="查看短期任务">
                    <div class="status-task-header">
                        <span class="status-task-title">短期任务</span>
                        <span class="status-task-count">${taskStats.active || 0}/${taskLimit}</span>
                    </div>
                    <div class="status-task-stats">
                        <span>活跃 ${taskStats.active || 0}</span>
                        <span>完成 ${taskStats.completed || 0}</span>
                        <span>取消 ${taskStats.canceled || 0}</span>
                        <span>过期 ${taskStats.expired || 0}</span>
                    </div>
                </button>
        ` : "";

        const customStatus = item.custom_status || {};
        const customStatusEntries = Object.entries(customStatus).filter(([k, v]) => k && v);
        const customStatusHtml = customStatusEntries.length > 0 ? `
            <div class="status-custom-container" style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px;">
                ${customStatusEntries.map(([k, v]) => `
                    <span class="status-pill status-pill-custom" style="background: rgba(99, 102, 241, 0.12); border: 1px solid rgba(99, 102, 241, 0.25);">
                        <span class="pill-label" style="color: var(--primary, #6366f1); font-weight: 600;">${window.escapeHtml(k)}</span>
                        <span class="pill-value">${window.escapeHtml(v)}</span>
                    </span>
                `).join("")}
            </div>
        ` : "";

        return `
            <div class="status-card card">
                <div class="status-card-header">
                    <div class="status-card-titles">
                        <h3 class="status-card-title">${window.escapeHtml(item.bot_name)}</h3>
                        <div class="status-card-subtitle-row">
                            <span class="status-card-subtitle" title="${window.escapeHtml(item.group_or_user_id)}">
                                会话: ${window.escapeHtml(item.group_or_user_id)}
                            </span>
                            <button class="btn-copy-icon" onclick="window.copyToClipboard(decodeURIComponent('${groupArg}'), '会话 ID')" title="复制会话 ID">
                                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                            </button>
                        </div>
                    </div>
                    <div class="status-energy-badge ${energyClass}">
                        <span class="energy-icon">
                            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"></path></svg>
                        </span>
                        <strong>${energy.toFixed(1)}%</strong>
                    </div>
                </div>

                <div class="status-badges-row">
                    <span class="status-pill status-pill-mood">
                        <span class="pill-label">心情</span>
                        <span class="pill-value">${window.escapeHtml(mood)}</span>
                    </span>
                    <span class="status-pill status-pill-state">
                        <span class="pill-label">状态</span>
                        <span class="pill-value">${window.escapeHtml(state)}</span>
                    </span>
                    <span class="status-pill status-pill-action">
                        <span class="pill-label">动作</span>
                        <span class="pill-value">${window.escapeHtml(action)}</span>
                    </span>
                </div>

                ${customStatusHtml}

                <div class="status-thought-box">
                    <div class="status-thought-header">
                        <span class="thought-icon">
                            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A5 5 0 0 0 8 8c0 1 .3 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"></path><line x1="9" y1="18" x2="15" y2="18"></line><line x1="10" y1="22" x2="14" y2="22"></line></svg>
                        </span>
                        <span class="thought-label">思考</span>
                    </div>
                    <div class="status-thought-content">${window.escapeHtml(memory)}</div>
                </div>

                ${taskBoardHtml}

                <div class="status-actions">
                    <button class="btn btn-secondary btn-small" onclick="window.openEditStatusModal('${botArg}', '${groupArg}', '${encodeStatusArg(mood)}', '${encodeStatusArg(state)}', '${encodeStatusArg(memory)}', '${encodeStatusArg(action)}', '${encodeStatusArg(JSON.stringify(customStatus))}')">调整状态</button>
                    <button class="btn btn-primary btn-small" onclick="window.fillEnergy('${botArg}', '${groupArg}')">补满能量</button>
                </div>
            </div>
        `;
    }).join("");
}
