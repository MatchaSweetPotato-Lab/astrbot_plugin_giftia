// Giftia State Module

export const state = {
    activeTab: "chat-history",
    activeSubTab: "user-profiles",

    // Pagination states
    pagination: {
        history: { page: 1, limit: 15, total: 0 },
        memories: { page: 1, limit: 15, total: 0 },
        media: { page: 1, limit: 12, total: 0 },
        stickers: { page: 1, limit: 12, total: 0 },
        forwards: { page: 1, limit: 15, total: 0 },
        userProfiles: { page: 1, limit: 15, total: 0 },
        groupProfiles: { page: 1, limit: 15, total: 0 },
        tokenLogs: { page: 1, limit: 15, total: 0 }
    },

    loadedOriginalMediaG: new Set(),
    filterOptions: {},

    // 表情包管理：多选集合、已加载原图集合、筛选项缓存
    selectedStickers: new Set(),
    loadedOriginalStickers: new Set(),
    stickerOptions: { categories: [], tags: [], bots: [], ai_available: false },
};
