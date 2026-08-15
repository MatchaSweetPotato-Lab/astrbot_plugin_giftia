// Giftia Dashboard Modals - User & Group Profiles & Aliases Actions

// 1. Edit User Profile
window.openEditUserProfileModal = function(bot, group, user, relation, titleEncoded, structuredEncoded) {
    const title = decodeURIComponent(titleEncoded || "");
    let structured = {};
    if (structuredEncoded) {
        try {
            structured = JSON.parse(decodeURIComponent(structuredEncoded));
        } catch (e) {
            structured = {};
        }
    }
    document.getElementById("edit-user-prof-bot").value = bot;
    document.getElementById("edit-user-prof-group").value = group;
    document.getElementById("edit-user-prof-user").value = user;
    document.getElementById("edit-user-prof-relation").value = relation !== undefined ? relation : 0;
    document.getElementById("edit-user-prof-title").value = title;
    document.getElementById("edit-user-prof-call-name").value = structured.call_name || "";
    document.getElementById("edit-user-prof-personality").value = structured.personality || "";
    document.getElementById("edit-user-prof-interests").value = structured.interests || "";
    document.getElementById("edit-user-prof-attitude").value = structured.attitude || "";
    document.getElementById("edit-user-prof-agreements").value = structured.agreements || "";
    document.getElementById("edit-user-prof-extra").value = structured.extra || "";
    window.openModal("edit-user-profile-modal");
};

window.submitEditUserProfile = async function() {
    const bot = document.getElementById("edit-user-prof-bot").value;
    const group = document.getElementById("edit-user-prof-group").value;
    const user = document.getElementById("edit-user-prof-user").value;
    const relationVal = document.getElementById("edit-user-prof-relation").value;
    const title = document.getElementById("edit-user-prof-title").value.trim();
    const callName = document.getElementById("edit-user-prof-call-name").value.trim();
    const personality = document.getElementById("edit-user-prof-personality").value.trim();
    const interests = document.getElementById("edit-user-prof-interests").value.trim();
    const attitude = document.getElementById("edit-user-prof-attitude").value.trim();
    const agreements = document.getElementById("edit-user-prof-agreements").value.trim();
    const extra = document.getElementById("edit-user-prof-extra").value.trim();

    const relation = relationVal !== "" ? parseInt(relationVal) : 0;

    try {
        const res = await window.apiPost("/profiles/user/update", {
            bot_name: bot,
            group_or_user_id: group,
            user_id: user,
            relation: relation,
            title: title,
            call_name: callName,
            personality: personality,
            interests: interests,
            attitude: attitude,
            agreements: agreements,
            extra: extra
        });
        if (res.status === "success") {
            window.showToast("保存成功！");
            window.closeModal("edit-user-profile-modal");
            window.GiftiaApp.loadUserProfiles();
        } else {
            window.showToast(`更新失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

function getUserAliasScope() {
    return {
        bot_name: document.getElementById("user-alias-bot").value,
        group_or_user_id: document.getElementById("user-alias-group").value,
        user_id: document.getElementById("user-alias-user").value
    };
}

window.openUserAliasesModal = async function(bot, group, user) {
    document.getElementById("user-alias-bot").value = bot;
    document.getElementById("user-alias-group").value = group;
    document.getElementById("user-alias-user").value = user;
    document.getElementById("user-alias-user-display").value = user;
    document.getElementById("user-alias-new").value = "";
    window.openModal("user-aliases-modal");
    await window.loadUserAliases();
};

window.loadUserAliases = async function() {
    const scope = getUserAliasScope();
    const list = document.getElementById("user-alias-list");
    list.innerHTML = `<tr><td colspan="5" class="loading-row"><span class="loader"></span> 加载数据中...</td></tr>`;
    try {
        const res = await window.apiGet("/profiles/user/aliases", scope);
        if (res.status !== "success") {
            throw new Error(res.message || "请求失败");
        }
        const items = (res.data && res.data.items) || [];
        if (items.length === 0) {
            list.innerHTML = `<tr><td colspan="5" class="no-data-row">暂无外号记录</td></tr>`;
            return;
        }
        list.innerHTML = items.map(item => {
            const alias = item.alias || "";
            const encodedAlias = encodeURIComponent(alias);
            const aliasCount = Math.max(1, parseInt(item.alias_count) || 1);
            return `
                <tr>
                    <td data-label="外号">${window.escapeHtml(alias)}</td>
                    <td data-label="次数">
                        <input type="number" class="alias-count-input" min="1" step="1" value="${aliasCount}">
                    </td>
                    <td data-label="首次出现">${window.formatDate(item.first_seen_at)}</td>
                    <td data-label="最近出现">${window.formatDate(item.last_seen_at)}</td>
                    <td data-label="操作" class="text-right">
                        <button class="btn btn-secondary btn-small" onclick="window.saveUserAliasCount('${encodedAlias}', this)">保存</button>
                        <button class="btn btn-danger btn-small" onclick="window.deleteUserAlias('${encodedAlias}')">删除</button>
                    </td>
                </tr>
            `;
        }).join("");
    } catch (e) {
        list.innerHTML = `<tr><td colspan="5" class="no-data-row">加载数据失败: ${window.escapeHtml(e.message)}</td></tr>`;
    }
};

window.submitAddUserAlias = async function() {
    const aliasInput = document.getElementById("user-alias-new");
    const alias = aliasInput.value.trim();
    if (!alias) {
        window.showToast("外号不能为空");
        return;
    }
    try {
        const res = await window.apiPost("/profiles/user/aliases/add", {
            ...getUserAliasScope(),
            alias: alias
        });
        if (res.status === "success") {
            aliasInput.value = "";
            window.showToast("外号已新增");
            await window.loadUserAliases();
            window.GiftiaApp.loadUserProfiles();
        } else {
            window.showToast(`新增失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

window.saveUserAliasCount = async function(aliasEncoded, button) {
    const alias = decodeURIComponent(aliasEncoded || "");
    const input = button.closest("tr").querySelector(".alias-count-input");
    const aliasCount = Number(input.value);
    if (!Number.isInteger(aliasCount) || aliasCount < 1) {
        window.showToast("统计次数必须是正整数");
        return;
    }
    try {
        const res = await window.apiPost("/profiles/user/aliases/count", {
            ...getUserAliasScope(),
            alias: alias,
            alias_count: aliasCount
        });
        if (res.status === "success") {
            window.showToast("统计次数已保存");
            await window.loadUserAliases();
            window.GiftiaApp.loadUserProfiles();
        } else {
            window.showToast(`保存失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

window.deleteUserAlias = function(aliasEncoded) {
    const alias = decodeURIComponent(aliasEncoded || "");
    window.showConfirm("确认删除外号", `确定要删除外号「${alias}」吗？`, async () => {
        try {
            const res = await window.apiPost("/profiles/user/aliases/delete", {
                ...getUserAliasScope(),
                alias: alias
            });
            if (res.status === "success") {
                window.showToast("外号已删除");
                await window.loadUserAliases();
                window.GiftiaApp.loadUserProfiles();
            } else {
                window.showToast(`删除失败: ${res.message}`);
            }
        } catch (e) {
            window.showToast(`发生错误: ${e.message}`);
        }
    });
};

window.deleteUserProfile = function(bot, group, user) {
    window.showConfirm("确认删除用户画像", "确定要删除该用户的画像总结吗？此操作不可逆。", async () => {
        try {
            const res = await window.apiPost("/profiles/user/delete", {
                bot_name: bot,
                group_or_user_id: group,
                user_id: user
            });
            if (res.status === "success") {
                window.showToast("删除画像成功");
                window.GiftiaApp.loadUserProfiles();
            } else {
                window.showToast(`删除失败: ${res.message}`);
            }
        } catch (e) {
            window.showToast(`发生错误: ${e.message}`);
        }
    });
};

// 2. Edit Group Profile
window.openEditGroupProfileModal = function(bot, group, profileEncoded) {
    const profile = decodeURIComponent(profileEncoded);
    document.getElementById("edit-group-prof-bot").value = bot;
    document.getElementById("edit-group-prof-group").value = group;
    document.getElementById("edit-group-prof-text").value = profile;
    window.openModal("edit-group-profile-modal");
};

window.submitEditGroupProfile = async function() {
    const bot = document.getElementById("edit-group-prof-bot").value;
    const group = document.getElementById("edit-group-prof-group").value;
    const profile = document.getElementById("edit-group-prof-text").value.trim();

    if (!profile) {
        window.showToast("画像内容不能为空！");
        return;
    }

    try {
        const res = await window.apiPost("/profiles/group/update", {
            bot_name: bot,
            group_or_user_id: group,
            profile: profile
        });
        if (res.status === "success") {
            window.showToast("保存成功！");
            window.closeModal("edit-group-profile-modal");
            window.GiftiaApp.loadGroupProfiles();
        } else {
            window.showToast(`更新失败: ${res.message}`);
        }
    } catch (e) {
        window.showToast(`发生错误: ${e.message}`);
    }
};

window.deleteGroupProfile = function(bot, group) {
    window.showConfirm("确认删除群聊画像", "确定要删除该群聊的画像总结吗？此操作不可逆。", async () => {
        try {
            const res = await window.apiPost("/profiles/group/delete", {
                bot_name: bot,
                group_or_user_id: group
            });
            if (res.status === "success") {
                window.showToast("删除画像成功");
                window.GiftiaApp.loadGroupProfiles();
            } else {
                window.showToast(`删除失败: ${res.message}`);
            }
        } catch (e) {
            window.showToast(`发生错误: ${e.message}`);
        }
    });
};

// 3. Expired User Aliases Auto Clean Modal JS
window.openAutoCleanAliasesModal = async function() {
    await window.loadAutoCleanAliasesConfig();
    window.openModal("auto-clean-aliases-modal");
};

window.loadAutoCleanAliasesConfig = async function() {
    try {
        const res = await window.apiGet("/profiles/user/aliases/auto_clean/config");
        if (res.status === "success" && res.config) {
            const cfg = res.config;
            document.getElementById("auto-clean-aliases-enabled").checked = Boolean(cfg.enabled);
            document.getElementById("auto-clean-aliases-min-age-days").value = Math.max(1, parseInt(cfg.min_age_days) || 7);
            document.getElementById("auto-clean-aliases-count-threshold").value = Math.max(1, parseInt(cfg.count_threshold) || 3);
        }
    } catch (e) {
        window.showToast(`加载过期外号清理配置失败: ${e.message}`);
    }
};

window.saveAutoCleanAliasesConfig = async function() {
    const config = {
        enabled: document.getElementById("auto-clean-aliases-enabled").checked,
        min_age_days: Math.max(1, parseInt(document.getElementById("auto-clean-aliases-min-age-days").value) || 7),
        count_threshold: Math.max(1, parseInt(document.getElementById("auto-clean-aliases-count-threshold").value) || 3)
    };
    try {
        const res = await window.apiPost("/profiles/user/aliases/auto_clean/config", config);
        if (res.status === "success") {
            window.showToast("已保存过期外号自动清理配置！");
            window.closeModal("auto-clean-aliases-modal");
        } else {
            window.showToast(`保存配置失败: ${res.message || "请求出错"}`);
        }
    } catch (e) {
        window.showToast(`保存过期外号清理配置出错: ${e.message}`);
    }
};

window.triggerAutoCleanAliasesImmediately = function() {
    window.showConfirm("确认执行清理", "确认要按当前配置立即清理未达标的过期外号吗？此操作无法撤销。", async () => {
        try {
            await window.saveAutoCleanAliasesConfig();
            const res = await window.apiPost("/profiles/user/aliases/auto_clean/trigger", {});
            if (res.status === "success") {
                const count = res.deleted_count ?? 0;
                window.showToast(`过期外号清理完成，共删除 ${count} 条候选外号`);
                window.closeModal("auto-clean-aliases-modal");
                if (window.loadUserAliases) {
                    await window.loadUserAliases();
                }
                if (window.GiftiaApp && window.GiftiaApp.loadUserProfiles) {
                    window.GiftiaApp.loadUserProfiles();
                }
            } else {
                window.showToast(`执行失败: ${res.message || "请求出错"}`);
            }
        } catch (e) {
            window.showToast(`执行清理出错: ${e.message}`);
        }
    });
};
