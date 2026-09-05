// Giftia Dashboard Frontend Logic - Main Coordinator (ESM Orchestrator)

import { state } from './modules/state.js';
import * as filters from './modules/filters.js';
import * as history from './modules/render/history.js';
import * as memories from './modules/render/memories.js';
import * as status from './modules/render/status.js';
import * as media from './modules/render/media.js';
import * as stickers from './modules/render/stickers.js';
import * as forwards from './modules/render/forwards.js';
import * as profiles from './modules/render/profiles.js';
import * as token from './modules/render/token.js';
import * as bots from './modules/bots.js';
import { initGlobalTooltip } from './components/tooltip.js';

// Assemble window.GiftiaApp keeping identical structure for backward compatibility
window.GiftiaApp = {
    loadBotsData: bots.loadBotsData,
    // Current state variables (mapped to modules/state)
    get activeTab() { return state.activeTab; },
    set activeTab(val) { state.activeTab = val; },
    get activeSubTab() { return state.activeSubTab; },
    set activeSubTab(val) { state.activeSubTab = val; },
    get pagination() { return state.pagination; },
    get loadedOriginalMediaG() { return state.loadedOriginalMediaG; },
    get filterOptions() { return state.filterOptions; },
    set filterOptions(val) { state.filterOptions = val; },

    // Filters and common view helpers
    getScopedViewConfig: filters.getScopedViewConfig,
    getScopedFilterParams: filters.getScopedFilterParams,
    populateBotSelect: filters.populateBotSelect,
    populateSessionSelect: filters.populateSessionSelect,
    refreshScopedFilters: filters.refreshScopedFilters,
    initializeScopedView: filters.initializeScopedView,
    loadScopedViewData: filters.loadScopedViewData,
    resetPagination: filters.resetPagination,
    loadActiveTabData: filters.loadActiveTabData,

    // Chat History
    loadChatHistory: history.loadChatHistory,
    renderChatHistory: history.renderChatHistory,
    get deleteChatMessage() { return window.deleteChatMessage; },

    // Memories
    loadMemories: memories.loadMemories,
    renderMemories: memories.renderMemories,

    // Bot Status
    loadBotStatus: status.loadBotStatus,
    renderBotStatus: status.renderBotStatus,

    // Media
    loadMedia: media.loadMedia,
    renderMedia: media.renderMedia,
    isClientPlayableAudio: media.isClientPlayableAudio,
    isPcPlayableAudio: media.isPcPlayableAudio,
    renderAudioUnsupportedNotice: media.renderAudioUnsupportedNotice,
    loadMediaFileB64: media.loadMediaFileB64,

    // Stickers
    initializeStickersTab: stickers.initializeStickersTab,
    loadStickers: stickers.loadStickers,
    renderStickers: stickers.renderStickers,
    loadStickerFilterOptions: stickers.loadStickerFilterOptions,
    loadStickerGifConfig: stickers.loadStickerGifConfig,
    loadStickerImage: stickers.loadStickerImage,
    updateStickerSelectionUI: stickers.updateStickerSelectionUI,
    getCurrentSticker: stickers.getCurrentSticker,

    // Forwards
    loadForwards: forwards.loadForwards,
    renderForwards: forwards.renderForwards,
    cleanOldForwards: forwards.cleanOldForwards,
    openForwardDetail: forwards.openForwardDetail,
    renderForwardDetail: forwards.renderForwardDetail,
    formatForwardSource: forwards.formatForwardSource,
    formatForwardNodeTime: forwards.formatForwardNodeTime,
    renderForwardBadges: forwards.renderForwardBadges,
    renderForwardStats: forwards.renderForwardStats,

    // Profiles
    loadProfilesData: profiles.loadProfilesData,
    updateProfileFilterVisibility: profiles.updateProfileFilterVisibility,
    loadUserProfiles: profiles.loadUserProfiles,
    renderUserProfiles: profiles.renderUserProfiles,
    loadGroupProfiles: profiles.loadGroupProfiles,
    renderGroupProfiles: profiles.renderGroupProfiles,

    // Token
    initializeTokenStatsTab: token.initializeTokenStatsTab,
    loadTokenStatsSummary: token.loadTokenStatsSummary,
    renderTokenStatsSummary: token.renderTokenStatsSummary,
};

// Global handlers bound to window for inline HTML onclick/etc.
window.toggleMemoryText = function(memoryId) {
    const textContainer = document.getElementById(`memory-text-${memoryId}`);
    const btnToggle = document.getElementById(`btn-toggle-${memoryId}`);
    if (textContainer && btnToggle) {
        const isCollapsed = textContainer.classList.contains("collapsed");
        if (isCollapsed) {
            textContainer.classList.remove("collapsed");
            textContainer.classList.add("expanded");
            btnToggle.textContent = "收起全部";
        } else {
            textContainer.classList.remove("expanded");
            textContainer.classList.add("collapsed");
            btnToggle.textContent = "展开全部";
        }
    }
};

window.filterMemoryByUser = async function(userId) {
    const input = document.getElementById("memory-associated-user-id");
    if (input) {
        input.value = userId;
        const app = window.GiftiaApp;
        app.resetPagination("memories");
        await app.loadMemories();
    }
};

window.openTokenAutoCleanModal = token.openTokenAutoCleanModal;
window.submitTokenAutoCleanConfig = token.submitTokenAutoCleanConfig;
window.triggerAutoCleanTokenImmediately = token.triggerAutoCleanTokenImmediately;
window.openTokenClearModal = token.openTokenClearModal;

// DOM Setup
document.addEventListener("DOMContentLoaded", () => {
    // Initialize Global Floating Tooltip Controller
    initGlobalTooltip();

    // Initialize AstrBot Bridge SDK
    if (window.AstrBotPluginPage) {
        window.AstrBotPluginPage.ready().then((context) => {
            console.log("AstrBot Plugin Bridge Ready. Context:", context);
            if (context && typeof context.isDark === "boolean") {
                document.documentElement.setAttribute(
                    "data-theme",
                    context.isDark ? "dark" : "light"
                );
                token.updateChartTheme();
            }
            window.GiftiaApp.loadActiveTabData();
        }).catch((err) => {
            console.error("Failed to initialize AstrBot Bridge SDK:", err);
            window.GiftiaApp.loadActiveTabData();
        });

        window.AstrBotPluginPage.onContext((ctx) => {
            if (ctx && typeof ctx.isDark === "boolean") {
                document.documentElement.setAttribute(
                    "data-theme",
                    ctx.isDark ? "dark" : "light"
                );
                token.updateChartTheme();
            }
        });
    } else {
        window.GiftiaApp.loadActiveTabData();
    }

    // Tab Navigation setup
    const tabButtons = document.querySelectorAll(".nav-tab");
    const tabPanels = document.querySelectorAll(".tab-panel");

    tabButtons.forEach(button => {
        button.addEventListener("click", () => {
            const targetTab = button.getAttribute("data-tab");

            tabButtons.forEach(btn => btn.classList.remove("active"));
            tabPanels.forEach(panel => panel.classList.remove("active"));

            button.classList.add("active");
            const targetPanel = document.getElementById(`tab-${targetTab}`);
            if (targetPanel) {
                targetPanel.classList.add("active");
            }

            window.GiftiaApp.activeTab = targetTab;
            window.GiftiaApp.loadActiveTabData();
        });
    });

    // Tab switching for Edit Media Modal and Clean Cache Modal
    document.addEventListener("click", (e) => {
        const btn = e.target.closest(".media-tab-btn");
        if (btn) {
            const tabName = btn.getAttribute("data-mediatab");
            const memoryCleanModal = btn.closest("#memory-clean-modal");
            if (
                memoryCleanModal &&
                typeof window.GiftiaModalActions?.setMemoryCleanMode === "function"
            ) {
                window.GiftiaModalActions.setMemoryCleanMode(tabName);
                return;
            }

            const parent = btn.closest(".edit-media-tabs");
            if (parent) {
                parent.querySelectorAll(".media-tab-btn").forEach(b => b.classList.remove("active"));
                parent.querySelectorAll(".media-tab-panel").forEach(p => p.classList.remove("active"));

                btn.classList.add("active");
                const targetPanel = parent.querySelector(`#mediatab-${tabName}`);
                if (targetPanel) {
                    targetPanel.classList.add("active");
                }

                // Toggle clean-cache-modal footer buttons visibility
                const modal = btn.closest("#clean-cache-modal");
                if (modal) {
                    const btnManualCalc = modal.querySelector("#btn-manual-calc");
                    const btnManualSubmit = modal.querySelector("#btn-manual-submit");
                    const btnAutoTrigger = modal.querySelector("#btn-auto-trigger");
                    const btnAutoSave = modal.querySelector("#btn-auto-save");
                    if (tabName === "clean-manual") {
                        if (btnManualCalc) btnManualCalc.style.display = "inline-block";
                        if (btnManualSubmit) btnManualSubmit.style.display = "inline-block";
                        if (btnAutoTrigger) btnAutoTrigger.style.display = "none";
                        if (btnAutoSave) btnAutoSave.style.display = "none";
                    } else if (tabName === "clean-auto") {
                        if (btnManualCalc) btnManualCalc.style.display = "none";
                        if (btnManualSubmit) btnManualSubmit.style.display = "none";
                        if (btnAutoTrigger) btnAutoTrigger.style.display = "inline-block";
                        if (btnAutoSave) btnAutoSave.style.display = "inline-block";
                    }
                }
            }
        }
    });

    // Filter Listeners (Debounced)
    let filterTimeout;
    const filterInputIds = [
        "history-bot-name", "history-group-id", "history-user-id", "history-decision", "history-rag", "history-search",
        "memory-bot-name", "memory-group-id", "memory-associated-user-id", "memory-search",
        "media-type", "media-search",
        "sticker-bot-name", "sticker-category", "sticker-tag", "sticker-search",
        "forward-bot-name", "forward-group-id", "forward-status", "forward-search",
        "profile-type", "profile-bot-name", "profile-group-id-select", "profile-group-id-input", "profile-user-id",
        "token-bot-name", "token-group-id", "token-time-range"
    ];

    filterInputIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            const eventType = el.tagName === "SELECT" ? "change" : "input";
            el.addEventListener(eventType, () => {
                clearTimeout(filterTimeout);
                filterTimeout = setTimeout(async () => {
                    const app = window.GiftiaApp;
                    const preserveSession = ![
                        "history-bot-name",
                        "memory-bot-name",
                        "forward-bot-name",
                        "profile-bot-name",
                        "profile-type",
                        "token-bot-name"
                    ].includes(id);
                    if (app.activeTab === "chat-history") {
                        app.resetPagination("history");
                        await app.refreshScopedFilters("history", preserveSession);
                        await app.loadChatHistory();
                    } else if (app.activeTab === "memories") {
                        app.resetPagination("memories");
                        await app.refreshScopedFilters("memories", preserveSession);
                        await app.loadMemories();
                    } else if (app.activeTab === "media-captions") {
                        app.pagination.media.page = 1;
                        app.loadMedia();
                    } else if (app.activeTab === "stickers") {
                        app.pagination.stickers.page = 1;
                        await app.loadStickers();
                    } else if (app.activeTab === "forward-messages") {
                        app.resetPagination("forwards");
                        await app.refreshScopedFilters("forwards", preserveSession);
                        await app.loadForwards();
                    } else if (app.activeTab === "profiles") {
                        app.activeSubTab = document.getElementById("profile-type")?.value || "user-profiles";
                        app.updateProfileFilterVisibility();
                        if (app.activeSubTab === "user-profiles") {
                            app.resetPagination("userProfiles");
                            await app.refreshScopedFilters("userProfiles", preserveSession);
                            await app.loadUserProfiles();
                        } else {
                            app.resetPagination("groupProfiles");
                            await app.refreshScopedFilters("groupProfiles", preserveSession);
                            await app.loadGroupProfiles();
                        }
                    } else if (app.activeTab === "token-stats") {
                        await app.refreshScopedFilters("tokenLogs", preserveSession);
                        await app.loadScopedViewData("tokenLogs");
                    }
                }, 300);
            });
        }
    });
});
