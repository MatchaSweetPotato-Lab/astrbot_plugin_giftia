import { state } from './state.js';

export function getScopedViewConfig(viewKey) {
    const configs = {
        history: {
            endpoint: "/chat_history",
            filterEndpoint: "/chat_history/filter_options",
            botInputId: "history-bot-name",
            groupInputId: "history-group-id",
            paginationKey: "history",
        },
        memories: {
            endpoint: "/memories",
            filterEndpoint: "/memories/filter_options",
            botInputId: "memory-bot-name",
            groupInputId: "memory-group-id",
            paginationKey: "memories",
        },
        forwards: {
            endpoint: "/forwards",
            filterEndpoint: "/forwards/filter_options",
            botInputId: "forward-bot-name",
            groupInputId: "forward-group-id",
            paginationKey: "forwards",
        },
        userProfiles: {
            endpoint: "/profiles/user",
            filterEndpoint: "/profiles/user/filter_options",
            botInputId: "profile-bot-name",
            groupInputId: "profile-group-id-select",
            paginationKey: "userProfiles",
        },
        groupProfiles: {
            endpoint: "/profiles/group",
            filterEndpoint: "/profiles/group/filter_options",
            botInputId: "profile-bot-name",
            groupInputId: "profile-group-id-input",
            paginationKey: "groupProfiles",
        },
        tokenLogs: {
            endpoint: "/token/logs",
            filterEndpoint: "/chat_history/filter_options",
            botInputId: "token-bot-name",
            groupInputId: "token-group-id",
            paginationKey: "tokenLogs",
        }
    };
    return configs[viewKey];
}

export function getScopedFilterParams(viewKey) {
    switch (viewKey) {
        case "history":
            return {
                bot_name: document.getElementById("history-bot-name").value,
                user_id: document.getElementById("history-user-id").value,
                reply_decision: document.getElementById("history-decision").value,
                use_rag: document.getElementById("history-rag").value,
                search: document.getElementById("history-search").value
            };
        case "memories":
            return {
                bot_name: document.getElementById("memory-bot-name").value,
                associated_user_id: document.getElementById("memory-associated-user-id").value,
                search: document.getElementById("memory-search").value
            };
        case "forwards":
            return {
                bot_name: document.getElementById("forward-bot-name").value,
                status: document.getElementById("forward-status").value,
                search: document.getElementById("forward-search").value
            };
        case "userProfiles":
            return {
                bot_name: document.getElementById("profile-bot-name").value,
                user_id: document.getElementById("profile-user-id").value
            };
        case "groupProfiles":
            return {
                bot_name: document.getElementById("profile-bot-name").value
            };
        case "tokenLogs":
            return {
                bot_name: document.getElementById("token-bot-name")?.value || ""
            };
        default:
            return {};
    }
}

export function populateBotSelect(selectEl, bots, selectedBotName) {
    if (!selectEl) return;
    selectEl.innerHTML = "";
    if (!bots || bots.length === 0) {
        selectEl.append(new Option("暂无 Bot", ""));
        selectEl.disabled = true;
        return;
    }

    bots.forEach(bot => {
        selectEl.append(new Option(bot, bot));
    });
    selectEl.disabled = false;
    selectEl.value = selectedBotName || bots[0];
}

export function populateSessionSelect(selectEl, sessions, selectedSession) {
    if (!selectEl) return;
    if (selectEl.tagName !== "SELECT") return;
    selectEl.innerHTML = "";
    if (!sessions || sessions.length === 0) {
        selectEl.append(new Option("暂无会话", ""));
        selectEl.disabled = true;
        return;
    }

    sessions.forEach(session => {
        const sessionId = session.group_or_user_id || "";
        const total = session.total || 0;
        selectEl.append(new Option(`${sessionId} (${total})`, sessionId));
    });
    selectEl.disabled = false;
    selectEl.value = selectedSession || sessions[0].group_or_user_id || "";
}

export async function refreshScopedFilters(viewKey, preserveSession = true) {
    const config = getScopedViewConfig(viewKey);
    if (!config) return;

    const botEl = document.getElementById(config.botInputId);
    const groupEl = document.getElementById(config.groupInputId);
    const currentSession = groupEl ? groupEl.value : "";
    const params = getScopedFilterParams(viewKey);

    try {
        const res = await window.apiGet(config.filterEndpoint, params);
        const data = res.status === "success" && res.data ? res.data : { bots: [], sessions: [], selected_bot_name: "" };
        const bots = data.bots || [];
        const selectedBotName = data.selected_bot_name || "";
        const sessions = data.sessions || [];

        populateBotSelect(botEl, bots, selectedBotName);

        if (groupEl && groupEl.tagName === "SELECT") {
            if (viewKey === "tokenLogs") {
                const nextSession = preserveSession && (currentSession === "" || sessions.some(item => item.group_or_user_id === currentSession))
                    ? currentSession
                    : "";
                groupEl.innerHTML = "";
                groupEl.append(new Option("全部会话/群聊", ""));
                sessions.forEach(session => {
                    const sessionId = session.group_or_user_id || "";
                    const total = session.total || 0;
                    groupEl.append(new Option(`${sessionId} (${total})`, sessionId));
                });
                groupEl.disabled = false;
                groupEl.value = nextSession;
            } else {
                const nextSession = preserveSession && sessions.some(item => item.group_or_user_id === currentSession)
                    ? currentSession
                    : (sessions[0] ? sessions[0].group_or_user_id : "");
                groupEl.value = nextSession;
                populateSessionSelect(groupEl, sessions, nextSession);
            }
        }
    } catch (e) {
        if (groupEl && groupEl.tagName === "SELECT") {
            groupEl.value = "";
            if (viewKey === "tokenLogs") {
                groupEl.innerHTML = "";
                groupEl.append(new Option("全部会话/群聊", ""));
                groupEl.disabled = false;
            } else {
                populateSessionSelect(groupEl, [], "");
            }
        }
    }
}

export async function initializeScopedView(viewKey) {
    await refreshScopedFilters(viewKey);
    await loadScopedViewData(viewKey);
}

export async function loadScopedViewData(viewKey) {
    switch (viewKey) {
        case "history":
            await window.GiftiaApp.loadChatHistory();
            break;
        case "memories":
            await window.GiftiaApp.loadMemories();
            break;
        case "forwards":
            await window.GiftiaApp.loadForwards();
            break;
        case "userProfiles":
            await window.GiftiaApp.loadUserProfiles();
            break;
        case "groupProfiles":
            await window.GiftiaApp.loadGroupProfiles();
            break;
        case "tokenLogs":
            await window.GiftiaApp.loadTokenStatsSummary();
            break;
        default:
            break;
    }
}

export function resetPagination(viewKey) {
    const config = getScopedViewConfig(viewKey);
    if (config && state.pagination[config.paginationKey]) {
        state.pagination[config.paginationKey].page = 1;
    }
}

export function loadActiveTabData() {
    const app = window.GiftiaApp;
    if (state.activeTab === "chat-history") {
        initializeScopedView("history");
    } else if (state.activeTab === "memories") {
        initializeScopedView("memories");
    } else if (state.activeTab === "bot-status") {
        app.loadBotStatus();
    } else if (state.activeTab === "media-captions") {
        app.loadMedia();
    } else if (state.activeTab === "forward-messages") {
        initializeScopedView("forwards");
    } else if (state.activeTab === "profiles") {
        app.loadProfilesData();
    } else if (state.activeTab === "token-stats") {
        app.initializeTokenStatsTab();
    } else if (state.activeTab === "bots") {
        if (typeof window.loadBotsData === "function") {
            window.loadBotsData();
        }
    }
}
