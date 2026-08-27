import { state } from '../state.js';
import { initializeScopedView } from '../filters.js';

export function loadProfilesData() {
    state.activeSubTab = document.getElementById("profile-type")?.value || "user-profiles";
    updateProfileFilterVisibility();
    if (state.activeSubTab === "user-profiles") {
        initializeScopedView("userProfiles");
    } else if (state.activeSubTab === "group-profiles") {
        initializeScopedView("groupProfiles");
    }
}

export function updateProfileFilterVisibility() {
    const userFilterGroup = document.getElementById("profile-user-filter-group");
    const groupSelectGroup = document.getElementById("profile-group-select-group");
    const groupInputGroup = document.getElementById("profile-group-input-group");
    const userPanel = document.getElementById("subpanel-user-profiles");
    const groupPanel = document.getElementById("subpanel-group-profiles");
    const isUserProfiles = state.activeSubTab === "user-profiles";
    if (userFilterGroup) {
        userFilterGroup.style.display = isUserProfiles ? "" : "none";
    }
    if (groupSelectGroup) {
        groupSelectGroup.style.display = isUserProfiles ? "" : "none";
    }
    if (groupInputGroup) {
        groupInputGroup.style.display = isUserProfiles ? "none" : "";
    }
    if (userPanel) {
        userPanel.classList.toggle("active", isUserProfiles);
    }
    if (groupPanel) {
        groupPanel.classList.toggle("active", !isUserProfiles);
    }
}

export async function loadUserProfiles() {
    const listContainer = document.getElementById("user-profile-list");
    listContainer.innerHTML = `<div class="loading-row" style="grid-column: 1 / -1; text-align: center; padding: 40px 0;"><span class="loader"></span> 加载数据中...</div>`;
    if (!document.getElementById("profile-bot-name").value) {
        state.pagination.userProfiles.total = 0;
        listContainer.innerHTML = `<div class="no-data-row" style="grid-column: 1 / -1; text-align: center; padding: 40px 0; color: var(--font-secondary);">暂无可用 Bot</div>`;
        window.renderPagination("user-profile-pagination", state.pagination.userProfiles, () => {});
        return;
    }

    const params = {
        page: state.pagination.userProfiles.page,
        limit: state.pagination.userProfiles.limit,
        bot_name: document.getElementById("profile-bot-name").value,
        group_or_user_id: document.getElementById("profile-group-id-select").value,
        user_id: document.getElementById("profile-user-id").value
    };

    try {
        const res = await window.apiGet("/profiles/user", params);
        if (res.status === "success" && res.data) {
            state.pagination.userProfiles.total = res.data.total;
            renderUserProfiles(res.data.items);
            window.renderPagination("user-profile-pagination", state.pagination.userProfiles, (page) => {
                state.pagination.userProfiles.page = page;
                loadUserProfiles();
            });
        } else {
            throw new Error(res.message || "请求失败");
        }
    } catch (e) {
        listContainer.innerHTML = `<div class="no-data-row" style="grid-column: 1 / -1; text-align: center; padding: 40px 0; color: var(--font-secondary);">加载数据失败: ${e.message}</div>`;
    }
}

export function renderUserProfiles(items) {
    const container = document.getElementById("user-profile-list");
    if (!items || items.length === 0) {
        container.innerHTML = `<div class="no-data-row" style="grid-column: 1 / -1; text-align: center; padding: 40px 0; color: var(--font-secondary);">暂无相关用户画像记录</div>`;
        return;
    }

    container.innerHTML = items.map(item => {
        const structured = {
            call_name: item.call_name || "",
            personality: item.personality || "",
            interests: item.interests || "",
            attitude: item.attitude || "",
            agreements: item.agreements || "",
            extra: item.extra || "",
            avatar_description: item.avatar_description || ""
        };
        const encodedStructured = encodeURIComponent(JSON.stringify(structured));
        const profileHtml = window.renderProfileCard("", {
            call_name: item.call_name,
            aliases: item.aliases,
            personality: item.personality,
            interests: item.interests,
            attitude: item.attitude,
            agreements: item.agreements,
            extra: item.extra,
            avatar_description: item.avatar_description
        });

        let relationBadge = "";
        const rel = parseInt(item.relation) || 0;
        if (rel > 0) {
            relationBadge = `<span class="badge badge-success">${rel} 好感度</span>`;
        } else if (rel < 0) {
            relationBadge = `<span class="badge badge-danger">${rel} 好感度</span>`;
        } else {
            relationBadge = `<span class="badge badge-secondary">0 好感度</span>`;
        }
        const titleHtml = item.title ? `<span class="badge badge-info">${window.escapeHtml(item.title)}</span>` : "";
        const encodedTitle = encodeURIComponent(item.title || "");

        return `
            <div class="profile-item-card card">
                <div class="profile-card-header">
                    <div class="profile-card-id-section">
                        <span class="profile-card-id-label">用户:</span>
                        <span class="profile-card-id-value">${item.user_id}</span>
                    </div>
                    <div class="profile-card-badges">
                        ${relationBadge}
                        ${titleHtml}
                    </div>
                </div>
                <div class="profile-card-body">
                    ${profileHtml}
                </div>
                <div class="profile-card-footer">
                    <div class="profile-card-time">
                        更新时间: ${window.formatDate(item.updated_at || item.created_at)}
                    </div>
                    <div class="profile-card-actions">
                        <button class="btn btn-secondary btn-small" onclick="window.openEditUserProfileModal('${item.bot_name}', '${item.group_or_user_id}', '${item.user_id}', ${rel}, '${encodedTitle}', '${encodedStructured}')">编辑</button>
                        <button class="btn btn-secondary btn-small" onclick="window.openUserAliasesModal('${item.bot_name}', '${item.group_or_user_id}', '${item.user_id}')">外号</button>
                        <button class="btn btn-danger btn-small" onclick="window.deleteUserProfile('${item.bot_name}', '${item.group_or_user_id}', '${item.user_id}')">删除</button>
                    </div>
                </div>
            </div>
        `;
    }).join("");
}

export async function loadGroupProfiles() {
    const listContainer = document.getElementById("group-profile-list");
    listContainer.innerHTML = `<div class="loading-row" style="grid-column: 1 / -1; text-align: center; padding: 40px 0;"><span class="loader"></span> 加载数据中...</div>`;
    if (!document.getElementById("profile-bot-name").value) {
        state.pagination.groupProfiles.total = 0;
        listContainer.innerHTML = `<div class="no-data-row" style="grid-column: 1 / -1; text-align: center; padding: 40px 0; color: var(--font-secondary);">暂无可用 Bot</div>`;
        window.renderPagination("group-profile-pagination", state.pagination.groupProfiles, () => {});
        return;
    }

    const params = {
        page: state.pagination.groupProfiles.page,
        limit: state.pagination.groupProfiles.limit,
        bot_name: document.getElementById("profile-bot-name").value,
        group_or_user_id: document.getElementById("profile-group-id-input").value
    };

    try {
        const res = await window.apiGet("/profiles/group", params);
        if (res.status === "success" && res.data) {
            state.pagination.groupProfiles.total = res.data.total;
            renderGroupProfiles(res.data.items);
            window.renderPagination("group-profile-pagination", state.pagination.groupProfiles, (page) => {
                state.pagination.groupProfiles.page = page;
                loadGroupProfiles();
            });
        } else {
            throw new Error(res.message || "请求失败");
        }
    } catch (e) {
        listContainer.innerHTML = `<div class="no-data-row" style="grid-column: 1 / -1; text-align: center; padding: 40px 0; color: var(--font-secondary);">加载数据失败: ${e.message}</div>`;
    }
}

export function renderGroupProfiles(items) {
    const container = document.getElementById("group-profile-list");
    if (!items || items.length === 0) {
        container.innerHTML = `<div class="no-data-row" style="grid-column: 1 / -1; text-align: center; padding: 40px 0; color: var(--font-secondary);">暂无相关群聊画像记录</div>`;
        return;
    }

    container.innerHTML = items.map(item => {
        const encodedProfile = encodeURIComponent(item.profile || "");
        const profileHtml = window.renderProfileCard(item.profile || "", null, true);

        return `
            <div class="profile-item-card card">
                <div class="profile-card-header">
                    <div class="profile-card-id-section">
                        <span class="profile-card-id-label">群聊:</span>
                        <span class="profile-card-id-value">${item.group_or_user_id}</span>
                    </div>
                </div>
                <div class="profile-card-body">
                    ${profileHtml}
                </div>
                <div class="profile-card-footer">
                    <div class="profile-card-time">
                        更新时间: ${window.formatDate(item.updated_at || item.created_at)}
                    </div>
                    <div class="profile-card-actions">
                        <button class="btn btn-secondary btn-small" onclick="window.openEditGroupProfileModal('${item.bot_name}', '${item.group_or_user_id}', '${encodedProfile}')">编辑</button>
                        <button class="btn btn-danger btn-small" onclick="window.deleteGroupProfile('${item.bot_name}', '${item.group_or_user_id}')">删除</button>
                    </div>
                </div>
            </div>
        `;
    }).join("");
}
