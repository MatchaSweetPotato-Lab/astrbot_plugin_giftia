// Giftia Dashboard Modals - Chat History Clean Actions

// 1. Clear chat history for specific session
window.clearChatHistory = async function() {
    const botName = document.getElementById("history-bot-name").value;
    const groupOrUserId = document.getElementById("history-group-id").value;
    if (!botName || !groupOrUserId) {
        window.showToast("当前没有选中的会话");
        return;
    }

    window.showConfirm("确认清空会话消息", `确定要清空 Bot [${botName}] 在会话 [${groupOrUserId}] 中的所有决策审计消息吗？此操作无法撤销。`, async () => {
        try {
            const res = await window.apiPost("/chat_history/delete", {
                bot_name: botName,
                group_or_user_id: groupOrUserId
            });
            if (res && res.status === "success") {
                window.showToast("会话消息清空成功");
                // Reset page to 1
                window.GiftiaApp.resetPagination("history");
                // Refresh filters and reload data
                await window.GiftiaApp.initializeScopedView("history");
            } else {
                window.showToast(`清空失败: ${res.message || "未知错误"}`);
            }
        } catch (e) {
            console.error("Failed to clear chat history:", e);
            window.showToast("清空会话消息出错");
        }
    });
};

// 2. Chat History Auto Clean Modal JS
window.openChatHistoryAutoCleanModal = async function() {
    await window.loadAutoCleanChatHistoryConfig();
    window.openModal("chat-history-auto-clean-modal");
};

window.loadAutoCleanChatHistoryConfig = async function() {
    try {
        const res = await window.apiGet("/chat_history/auto_clean/config");
        if (res.status === "success" && res.config) {
            const cfg = res.config;
            document.getElementById("auto-clean-chat-enabled").checked = Boolean(cfg.enabled);
            document.getElementById("auto-clean-chat-max-count").value = Math.max(0, parseInt(cfg.max_count_per_session) || 0);
            document.getElementById("auto-clean-chat-max-days").value = Math.max(0, parseInt(cfg.max_age_days) || 0);
            document.getElementById("auto-clean-chat-min-keep").value = Math.max(1, parseInt(cfg.min_keep_per_session) || 20);
        }
    } catch (e) {
        window.showToast(`加载聊天记录自动清理配置失败: ${e.message}`);
    }
};

window.saveAutoCleanChatHistoryConfig = async function() {
    const config = {
        enabled: document.getElementById("auto-clean-chat-enabled").checked,
        max_count_per_session: Math.max(0, parseInt(document.getElementById("auto-clean-chat-max-count").value) || 0),
        max_age_days: Math.max(0, parseInt(document.getElementById("auto-clean-chat-max-days").value) || 0),
        min_keep_per_session: Math.max(1, parseInt(document.getElementById("auto-clean-chat-min-keep").value) || 20)
    };
    try {
        const res = await window.apiPost("/chat_history/auto_clean/config", config);
        if (res.status === "success") {
            window.showToast("已保存聊天记录自动清理配置！");
            window.closeModal("chat-history-auto-clean-modal");
        } else {
            window.showToast(`保存配置失败: ${res.message || "请求出错"}`);
        }
    } catch (e) {
        window.showToast(`保存聊天记录自动清理配置出错: ${e.message}`);
    }
};

window.triggerAutoCleanChatHistoryImmediately = function() {
    window.showConfirm("确认执行清理", "确认要按当前自动清理配置立即清理聊天记录吗？此操作无法撤销。", async () => {
        try {
            await window.saveAutoCleanChatHistoryConfig();
            const res = await window.apiPost("/chat_history/auto_clean/trigger", {});
            if (res.status === "success") {
                const count = res.deleted_count ?? 0;
                window.showToast(`聊天记录清理完成，共删除 ${count} 条历史消息`);
                window.closeModal("chat-history-auto-clean-modal");
                if (window.GiftiaApp && window.GiftiaApp.loadChatHistory) {
                    window.GiftiaApp.loadChatHistory();
                }
            } else {
                window.showToast(`执行失败: ${res.message || "请求出错"}`);
            }
        } catch (e) {
            window.showToast(`执行清理出错: ${e.message}`);
        }
    });
};
