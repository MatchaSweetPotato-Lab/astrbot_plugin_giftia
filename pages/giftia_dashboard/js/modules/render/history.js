import { state } from '../state.js';

export async function loadChatHistory() {
    const listContainer = document.getElementById("history-list");
    listContainer.innerHTML = `<tr><td colspan="6" class="loading-row"><span class="loader"></span> 加载数据中...</td></tr>`;
    if (!document.getElementById("history-bot-name").value) {
        state.pagination.history.total = 0;
        listContainer.innerHTML = `<tr><td colspan="6" class="no-data-row">暂无可用 Bot</td></tr>`;
        window.renderPagination("history-pagination", state.pagination.history, () => {});
        return;
    }

    const params = {
        page: state.pagination.history.page,
        limit: state.pagination.history.limit,
        bot_name: document.getElementById("history-bot-name").value,
        group_or_user_id: document.getElementById("history-group-id").value,
        user_id: document.getElementById("history-user-id").value,
        reply_decision: document.getElementById("history-decision").value,
        use_rag: document.getElementById("history-rag").value,
        search: document.getElementById("history-search").value
    };

    try {
        const res = await window.apiGet("/chat_history", params);
        if (res.status === "success" && res.data) {
            state.pagination.history.total = res.data.total;
            const lastSummarizedId = res.data.last_summarized_id || 0;
            const boundaryEl = document.getElementById("history-last-summarized-id");
            if (boundaryEl) {
                boundaryEl.textContent = lastSummarizedId > 0 ? `#${lastSummarizedId}` : "无";
            }
            renderChatHistory(res.data.items, lastSummarizedId);
            window.renderPagination("history-pagination", state.pagination.history, (page) => {
                state.pagination.history.page = page;
                loadChatHistory();
            });
        } else {
            throw new Error(res.message || "请求失败");
        }
    } catch (e) {
        listContainer.innerHTML = `<tr><td colspan="6" class="no-data-row">加载数据失败: ${e.message}</td></tr>`;
    }
}

export function renderChatHistory(items, lastSummarizedId = 0) {
    const container = document.getElementById("history-list");
    if (!items || items.length === 0) {
        container.innerHTML = `<tr><td colspan="6" class="no-data-row">暂无相关聊天记录</td></tr>`;
        return;
    }

    container.innerHTML = items.map(item => {
        let decisionBadge = "";
        if (item.reply_decision === 1) {
            decisionBadge = `<span class="badge badge-success">通过 (已回复)</span>`;
        } else if (item.reply_decision === 0) {
            decisionBadge = `<span class="badge badge-danger">忽略</span>`;
        } else if (item.reply_decision === 3) {
            decisionBadge = `<span class="badge badge-info">唤醒直接回复</span>`;
        } else {
            decisionBadge = `<span class="badge badge-secondary">未审查</span>`;
        }

        const ragBadge = item.use_rag === 1
            ? `<span class="badge badge-info">触发 RAG</span>`
            : `<span class="badge badge-secondary">未触发</span>`;

        const senderDisp = item.nickname ? `${item.nickname} (${item.user_id})` : item.user_id;

        const isSummarized = item.id <= lastSummarizedId;
        const summaryBadge = isSummarized
            ? `<span class="badge badge-success" style="font-size: 0.75rem; padding: 2px 6px;">已归档</span>`
            : `<span class="badge badge-secondary" style="font-size: 0.75rem; padding: 2px 6px;">待总结/跳过</span>`;

        return `
            <tr>
                <td data-label="ID">
                    <div style="display: flex; flex-direction: column; align-items: flex-start; gap: 4px;">
                        <code>#${item.id}</code>
                        ${summaryBadge}
                    </div>
                </td>
                <td data-label="时间" style="white-space: nowrap;">${window.formatDate(item.created_at)}</td>
                <td data-label="发送人">${senderDisp}</td>
                <td data-label="消息内容">
                    <div style="max-width: 480px; word-break: break-all;">${window.renderMessageWithMediaTags ? window.renderMessageWithMediaTags(item.content) : window.escapeHtml(item.content)}</div>
                </td>
                <td data-label="判定结果">${decisionBadge}</td>
                <td data-label="RAG状态">${ragBadge}</td>
            </tr>
        `;
    }).join("");
}
