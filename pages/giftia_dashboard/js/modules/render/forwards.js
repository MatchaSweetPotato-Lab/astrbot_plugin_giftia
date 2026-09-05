import { state } from '../state.js';
import { refreshScopedFilters } from '../filters.js';

export function formatForwardSource(source) {
    const labels = {
        remote: "远程",
        onebot: "内联",
        json: "JSON",
        component: "组件"
    };
    return labels[source] || source || "未知";
}

export function formatForwardNodeTime(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";

    const numeric = Number(raw);
    if (Number.isFinite(numeric) && numeric > 0) {
        const ms = numeric > 100000000000 ? numeric : numeric * 1000;
        return window.formatDate(new Date(ms).toISOString());
    }
    return window.formatDate(raw);
}

export function renderForwardBadges(item) {
    const flags = item.flags || {};
    const badges = [
        item.is_summarized
            ? `<span class="badge badge-success">已转述</span>`
            : `<span class="badge badge-secondary">待转述</span>`
    ];
    if (flags.unresolved) {
        badges.push(`<span class="badge badge-warning">未完整拉取</span>`);
    }
    if (flags.truncated) {
        badges.push(`<span class="badge badge-warning">已截断</span>`);
    }
    return `<div class="forward-badges">${badges.join("")}</div>`;
}

export function renderForwardStats(item) {
    return `
        <div class="forward-stats">
            <span>节点 ${item.node_count || 0}</span>
            <span>发送人 ${item.sender_count || 0}</span>
            <span>媒体 ${item.media_count || 0}</span>
            <span>嵌套 ${item.nested_count || 0}</span>
        </div>
    `;
}

export async function loadForwards() {
    const listContainer = document.getElementById("forward-list");
    listContainer.innerHTML = `<tr><td colspan="7" class="loading-row"><span class="loader"></span> 加载合并转发中...</td></tr>`;
    if (!document.getElementById("forward-bot-name").value) {
        state.pagination.forwards.total = 0;
        listContainer.innerHTML = `<tr><td colspan="7" class="no-data-row">暂无可用 Bot</td></tr>`;
        window.renderPagination("forward-pagination", state.pagination.forwards, () => {});
        return;
    }

    const params = {
        page: state.pagination.forwards.page,
        limit: state.pagination.forwards.limit,
        bot_name: document.getElementById("forward-bot-name").value,
        group_or_user_id: document.getElementById("forward-group-id").value,
        status: document.getElementById("forward-status").value,
        search: document.getElementById("forward-search").value
    };

    try {
        const res = await window.apiGet("/forwards", params);
        if (res.status === "success" && res.data) {
            state.pagination.forwards.total = res.data.total;
            renderForwards(res.data.items);
            window.renderPagination("forward-pagination", state.pagination.forwards, (page) => {
                state.pagination.forwards.page = page;
                loadForwards();
            });
        } else {
            throw new Error(res.message || "请求失败");
        }
    } catch (e) {
        listContainer.innerHTML = `<tr><td colspan="7" class="no-data-row">加载合并转发失败: ${window.escapeHtml(e.message)}</td></tr>`;
    }
}

export async function cleanOldForwards() {
    window.showConfirm(
        "确认清理合并转发",
        "确定要清理超过 24 小时的合并转发记录吗？这些记录通常只用于即时查看，清理后无法恢复。",
        async () => {
            const btn = document.getElementById("btn-clean-forwards");
            if (btn) {
                btn.disabled = true;
                btn.textContent = "清理中...";
            }
            try {
                const res = await window.apiPost("/forwards/clean", {});
                if (res && res.status === "success") {
                    const count = Number(res.count || 0);
                    window.showToast(`已清理 ${count} 条过期合并转发记录`);
                    state.pagination.forwards.page = 1;
                    await refreshScopedFilters("forwards", false);
                    await loadForwards();
                } else {
                    window.showToast(`清理失败: ${res.message || "请求出错"}`);
                }
            } catch (e) {
                window.showToast(`清理出错: ${e.message}`);
            } finally {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = "清理过期";
                }
            }
        }
    );
}

export function renderForwards(items) {
    const container = document.getElementById("forward-list");
    if (!items || items.length === 0) {
        container.innerHTML = `<tr><td colspan="7" class="no-data-row">暂无相关合并转发记录</td></tr>`;
        return;
    }

    const encodeArg = value => encodeURIComponent(value || "").replace(/'/g, "%27");
    container.innerHTML = items.map(item => {
        const botArg = encodeArg(item.bot_name);
        const groupArg = encodeArg(item.group_or_user_id);
        const forwardArg = encodeArg(item.forward_id);
        const sourceLabel = formatForwardSource(item.source);
        const preview = item.preview || item.summary || "暂无内容";
        const updatedAt = item.updated_at || item.created_at;

        return `
            <tr>
                <td data-label="时间" style="white-space: nowrap;">${window.formatDate(updatedAt)}</td>
                <td data-label="Forward ID">
                    <div class="forward-id-cell">
                        <code>${window.escapeHtml(item.forward_id || "")}</code>
                        <button class="btn-copy-icon" onclick="window.copyToClipboard(decodeURIComponent('${forwardArg}'), 'Forward ID')" title="复制 Forward ID">
                            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                        </button>
                    </div>
                </td>
                <td data-label="会话/来源">
                    <div class="forward-source-cell">
                        <span>${window.escapeHtml(item.group_or_user_id || "")}</span>
                        <span>${window.escapeHtml(sourceLabel)}${item.source_id ? ` · ${window.escapeHtml(item.source_id)}` : ""}</span>
                        <span>所属消息: ${window.escapeHtml(item.owner_message_id || "-")}</span>
                    </div>
                </td>
                <td data-label="结构">${renderForwardStats(item)}</td>
                <td data-label="状态">${renderForwardBadges(item)}</td>
                <td data-label="摘要/预览">
                    <div class="forward-summary-cell">${window.escapeHtml(preview)}</div>
                </td>
                <td data-label="操作" class="text-right">
                    <button class="btn btn-secondary btn-small" onclick="window.GiftiaApp.openForwardDetail('${botArg}', '${groupArg}', '${forwardArg}')">查看</button>
                </td>
            </tr>
        `;
    }).join("");
}

export async function openForwardDetail(botEncoded, groupEncoded, forwardEncoded) {
    const botName = decodeURIComponent(botEncoded || "");
    const groupId = decodeURIComponent(groupEncoded || "");
    const forwardId = decodeURIComponent(forwardEncoded || "");
    const title = document.getElementById("forward-detail-title");
    const body = document.getElementById("forward-detail-body");

    title.textContent = `合并转发详情 · ${forwardId || "未知"}`;
    body.innerHTML = `<div class="loading-row flex-grow"><span class="loader"></span> 加载详情中...</div>`;
    window.openModal("forward-detail-modal");

    try {
        const res = await window.apiGet("/forwards/detail", {
            bot_name: botName,
            group_or_user_id: groupId,
            forward_id: forwardId
        });
        if (res.status !== "success" || !res.data) {
            throw new Error(res.message || "请求失败");
        }
        renderForwardDetail(res.data);
    } catch (e) {
        body.innerHTML = `<div class="no-data-row flex-grow">加载详情失败: ${window.escapeHtml(e.message)}</div>`;
    }
}

export function renderForwardDetail(item) {
    const body = document.getElementById("forward-detail-body");
    const nodes = item.nodes || [];
    const summary = item.summary || "暂无缓存转述";
    const sourceLabel = formatForwardSource(item.source);

    const nodesHtml = nodes.length > 0 ? nodes.map(node => {
        const sender = node.sender_name || node.sender_id || "未知用户";
        const nodeTime = formatForwardNodeTime(node.time);
        const mediaHtml = node.media_ids && node.media_ids.length > 0
            ? `<div class="forward-node-refs">媒体: ${node.media_ids.map(id => `<code>${window.escapeHtml(id)}</code>`).join("")}</div>`
            : "";
        const nestedHtml = node.nested_ids && node.nested_ids.length > 0
            ? `<div class="forward-node-refs">嵌套转发: ${node.nested_ids.map(id => `<code>${window.escapeHtml(id)}</code>`).join("")}</div>`
            : "";

        return `
            <div class="forward-node">
                <div class="forward-node-header">
                    <span>#${window.escapeHtml(String(node.index || ""))}</span>
                    <strong>${window.escapeHtml(sender)}</strong>
                    ${nodeTime ? `<span>${window.escapeHtml(nodeTime)}</span>` : ""}
                </div>
                <div class="forward-node-content">${window.renderMessageWithMediaTags ? window.renderMessageWithMediaTags(node.content || "") : (window.escapeHtml(node.content || "") || "空消息")}</div>
                ${mediaHtml}
                ${nestedHtml}
            </div>
        `;
    }).join("") : `<div class="no-data-row flex-grow">暂无节点内容</div>`;

    body.innerHTML = `
        <div class="forward-detail-meta">
            <div class="forward-detail-meta-item">
                <span>Bot</span>
                <strong>${window.escapeHtml(item.bot_name || "-")}</strong>
            </div>
            <div class="forward-detail-meta-item">
                <span>会话</span>
                <strong>${window.escapeHtml(item.group_or_user_id || "-")}</strong>
            </div>
            <div class="forward-detail-meta-item">
                <span>所属消息</span>
                <strong>${window.escapeHtml(item.owner_message_id || "-")}</strong>
            </div>
            <div class="forward-detail-meta-item">
                <span>来源</span>
                <strong>${window.escapeHtml(sourceLabel)}${item.source_id ? ` · ${window.escapeHtml(item.source_id)}` : ""}</strong>
            </div>
        </div>
        <div class="forward-detail-strip">
            ${renderForwardStats(item)}
            ${renderForwardBadges(item)}
        </div>
        <div class="forward-detail-summary">
            <div class="forward-detail-section-title">缓存转述</div>
            <div>${window.escapeHtml(summary)}</div>
        </div>
        <div class="forward-detail-section-title">节点内容</div>
        <div class="forward-node-list">${nodesHtml}</div>
    `;
}
