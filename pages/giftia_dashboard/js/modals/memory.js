// Giftia Dashboard Modals - Memory Actions & Clean

function normalizeMemoryImportance(value) {
    let importance = Number(value || 5);
    if (!Number.isFinite(importance)) {
        importance = 5;
    }
    return Math.min(10, Math.max(1, importance));
}

let editingMemory = null;

// 1. Add Memory
window.openAddMemoryModal = function() {
    document.getElementById('add-mem-bot').value = document.getElementById('memory-bot-name').value;
    document.getElementById('add-mem-group').value = document.getElementById('memory-group-id').value;
    document.getElementById('add-mem-associated-ids').value = document.getElementById('memory-associated-user-id').value.trim();
    for (const field of ['topic', 'text']) {
        document.getElementById(`add-mem-${field}`).value = '';
    }
    document.getElementById('add-mem-importance').value = '5';
    window.openModal('add-memory-modal');
};

window.submitAddMemory = async function() {
    const body = {
        bot_name: document.getElementById('add-mem-bot').value.trim(),
        group_or_user_id: document.getElementById('add-mem-group').value.trim(),
        topic: document.getElementById('add-mem-topic').value.trim(),
        associated_user_ids: document.getElementById('add-mem-associated-ids').value.trim(),
        importance: normalizeMemoryImportance(document.getElementById('add-mem-importance').value),
        text: document.getElementById('add-mem-text').value.trim()
    };
    if (!body.bot_name || !body.group_or_user_id || !body.associated_user_ids || !body.topic || !body.text) {
        window.showToast('请填写 Bot、会话、关联群友、主题和记忆文本');
        return;
    }
    const button = document.getElementById('add-mem-save');
    button.disabled = true;
    try {
        const res = await window.apiPost('/memories/add', body);
        if (res.status !== 'success') throw new Error(res.message || '保存失败');
        window.showToast('保存成功，已定位到人工录入的记忆');
        window.closeModal('add-memory-modal');
        for (const [field, value] of [['bot-name', body.bot_name], ['group-id', body.group_or_user_id]]) {
            const select = document.getElementById(`memory-${field}`);
            if (!Array.from(select.options).some(option => option.value === value)) select.append(new Option(value, value));
            select.value = value;
        }
        document.getElementById('memory-search').value = '';
        document.getElementById('memory-associated-user-id').value = '';
        window.GiftiaApp.resetPagination('memories');
        await window.GiftiaApp.refreshScopedFilters('memories', true);
        await window.GiftiaApp.loadMemories();
    } catch (e) {
        window.showToast(`保存失败: ${e.message}`);
    } finally {
        button.disabled = false;
    }
};

// 2. Edit Memory
window.openEditMemoryModal = function(item) {
    editingMemory = item;
    const meta = item.metadata || {};
    document.getElementById('edit-mem-id').value = item.memory_id;
    document.getElementById('edit-mem-revision').value = item.revision;
    document.getElementById('edit-mem-bot').value = item.bot_name;
    document.getElementById('edit-mem-group').value = item.group_or_user_id;
    document.getElementById('edit-mem-identity').textContent = [
        meta.manual_entry ? '人工录入' : '',
        meta.topic || ''
    ].filter(Boolean).join(' · ');
    document.getElementById('edit-mem-associated-ids').value = Array.isArray(meta.associated_user_ids)
        ? meta.associated_user_ids.join(',') : (meta.user_id || '');
    document.getElementById('edit-mem-importance').value = normalizeMemoryImportance(item.importance);
    document.getElementById('edit-mem-text').value = item.text;
    window.openModal('edit-memory-modal');
};

window.submitEditMemory = async function() {
    if (!editingMemory) return;
    const body = {
        memory_id: document.getElementById('edit-mem-id').value,
        bot_name: document.getElementById('edit-mem-bot').value,
        group_or_user_id: document.getElementById('edit-mem-group').value,
        revision: Number(document.getElementById('edit-mem-revision').value),
        associated_user_ids: document.getElementById('edit-mem-associated-ids').value.trim(),
        importance: normalizeMemoryImportance(document.getElementById('edit-mem-importance').value),
        text: document.getElementById('edit-mem-text').value.trim()
    };
    if (!body.text) {
        window.showToast('记忆文本不能为空');
        return;
    }
    const button = document.getElementById('edit-mem-save');
    button.disabled = true;
    try {
        const res = await window.apiPost('/memories/update', body);
        if (res.status !== 'success') throw new Error(res.message || '更新失败');
        window.showToast('保存成功');
        window.closeModal('edit-memory-modal');
        editingMemory = null;
        await window.GiftiaApp.refreshScopedFilters('memories', true);
        await window.GiftiaApp.loadMemories();
    } catch (e) {
        window.showToast(`更新失败: ${e.message}`);
    } finally {
        button.disabled = false;
    }
};

// 3. Delete Memory
window.deleteMemory = function(id, bot, group) {
    window.showConfirm("确认删除长期记忆", "确定要删除这条长期记忆吗？此操作无法撤销。", async () => {
        try {
            const res = await window.apiPost("/memories/delete", { memory_id: id, bot_name: bot, group_or_user_id: group });
            if (res.status === "success") {
                window.showToast("记忆删除成功");
                await window.GiftiaApp.refreshScopedFilters('memories', true);
                await window.GiftiaApp.loadMemories();
            } else {
                window.showToast(`删除失败: ${res.message}`);
            }
        } catch (e) {
            window.showToast(`发生错误: ${e.message}`);
        }
    });
};

window.openMemoryCleanModal = function() {
    const botSelect = document.getElementById("memory-bot-name");
    const groupSelect = document.getElementById("memory-group-id");
    document.getElementById("clean-mem-bot").value = botSelect ? botSelect.value : "";
    document.getElementById("clean-mem-group").value = groupSelect ? groupSelect.value : "";
    document.getElementById("clean-mem-associated-user-id").value = document.getElementById("memory-associated-user-id")?.value || "";
    document.getElementById("clean-mem-search").value = document.getElementById("memory-search")?.value || "";
    document.getElementById("clean-mem-max-importance").value = "3";
    document.getElementById("clean-mem-max-hit-count").value = "1";
    document.getElementById("clean-mem-min-age-days").value = "60";
    document.getElementById("clean-mem-last-hit-before-days").value = "30";
    document.getElementById("clean-mem-include-never-hit").checked = true;

    const summary = document.getElementById("memory-clean-summary");
    summary.innerHTML = "设置条件后点击“筛选候选记忆”。";
    summary.style.borderLeftColor = "var(--border-color)";
    document.getElementById("memory-clean-candidate-list").innerHTML = "";
    document.getElementById("memory-clean-selection-tools").style.display = "none";
    document.getElementById("btn-clean-selected-memories").disabled = true;
    setMemoryCleanMode("memory-clean-manual");
    window.openModal("memory-clean-modal");
};

function normalizeMemoryCleanNumber(value, fallback, minValue, maxValue) {
    let result = Number(value);
    if (!Number.isFinite(result)) {
        result = fallback;
    }
    result = Math.trunc(result);
    if (typeof minValue === "number") {
        result = Math.max(minValue, result);
    }
    if (typeof maxValue === "number") {
        result = Math.min(maxValue, result);
    }
    return result;
}

function setMemoryCleanMode(mode) {
    const modal = document.getElementById("memory-clean-modal");
    if (!modal) return;

    modal.querySelectorAll(".media-tab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-mediatab") === mode);
    });
    modal.querySelectorAll(".media-tab-panel").forEach(panel => {
        panel.classList.toggle("active", panel.id === `mediatab-${mode}`);
    });

    const manualMode = mode === "memory-clean-manual";
    const manualFilter = document.getElementById("btn-memory-manual-filter");
    const manualClean = document.getElementById("btn-clean-selected-memories");
    const autoTrigger = document.getElementById("btn-auto-clean-mem-trigger");
    const autoSave = document.getElementById("btn-auto-clean-mem-save");
    if (manualFilter) manualFilter.style.display = manualMode ? "inline-block" : "none";
    if (manualClean) manualClean.style.display = manualMode ? "inline-block" : "none";
    if (autoTrigger) autoTrigger.style.display = manualMode ? "none" : "inline-block";
    if (autoSave) autoSave.style.display = manualMode ? "none" : "inline-block";
}
window.GiftiaModalActions = window.GiftiaModalActions || {};
window.GiftiaModalActions.setMemoryCleanMode = setMemoryCleanMode;

function collectMemoryCleanCriteria() {
    return {
        bot_name: document.getElementById("clean-mem-bot").value.trim(),
        group_or_user_id: document.getElementById("clean-mem-group").value.trim(),
        associated_user_id: document.getElementById("clean-mem-associated-user-id").value.trim(),
        search: document.getElementById("clean-mem-search").value.trim(),
        max_importance: normalizeMemoryCleanNumber(document.getElementById("clean-mem-max-importance").value, 3, 1, 10),
        max_hit_count: normalizeMemoryCleanNumber(document.getElementById("clean-mem-max-hit-count").value, 1, 0),
        min_age_days: normalizeMemoryCleanNumber(document.getElementById("clean-mem-min-age-days").value, 60, 0),
        last_hit_before_days: normalizeMemoryCleanNumber(document.getElementById("clean-mem-last-hit-before-days").value, 30, 0),
        include_never_hit: document.getElementById("clean-mem-include-never-hit").checked,
        limit: 500
    };
}

function renderMemoryCleanCandidates(items, total, truncated) {
    const list = document.getElementById("memory-clean-candidate-list");
    const tools = document.getElementById("memory-clean-selection-tools");
    const summary = document.getElementById("memory-clean-summary");

    if (!items || items.length === 0) {
        list.innerHTML = "";
        tools.style.display = "none";
        summary.innerHTML = "没有筛选到符合条件的长期记忆。";
        summary.style.borderLeftColor = "var(--success)";
        window.updateMemoryCleanSelectionState();
        return;
    }

    const truncationText = truncated ? `，仅显示前 ${items.length} 条` : "";
    summary.innerHTML = `筛选到 <strong>${total}</strong> 条候选记忆${truncationText}。请取消勾选需要保留的记忆后再清理。`;
    summary.style.borderLeftColor = "var(--primary)";
    tools.style.display = "flex";

    list.innerHTML = items.map(item => {
        const reasons = Array.isArray(item.clean_reasons) ? item.clean_reasons : [];
        const reasonHtml = reasons.length > 0
            ? reasons.map(reason => `<span>${window.escapeHtml(reason)}</span>`).join("")
            : "<span>符合当前筛选条件</span>";
        const lastHitAt = item.last_hit_at ? window.formatDate(item.last_hit_at) : "从未命中";
        return `
            <div class="memory-clean-candidate">
                <input type="checkbox" class="memory-clean-checkbox" data-memory-id="${window.escapeHtml(item.memory_id)}" onchange="updateMemoryCleanSelectionState()" checked>
                <div>
                    <div class="memory-clean-candidate-text">${window.escapeHtml(item.text)}</div>
                    <div class="memory-clean-candidate-meta">
                        ${item.metadata?.manual_entry ? '<span>人工录入</span>' : ''}
                        <span>会话 ${window.escapeHtml(item.group_or_user_id || "-")}</span>
                        <span>重要度 ${item.importance}</span>
                        <span>命中 ${item.hit_count || 0} 次</span>
                        <span>创建 ${window.formatDate(item.created_at)}</span>
                        <span>最近命中 ${lastHitAt}</span>
                    </div>
                    <div class="memory-clean-candidate-reasons">${reasonHtml}</div>
                </div>
            </div>
        `;
    }).join("");
    window.updateMemoryCleanSelectionState();
}

window.loadMemoryCleanCandidates = async function() {
    const criteria = collectMemoryCleanCriteria();
    const summary = document.getElementById("memory-clean-summary");
    if (!criteria.bot_name) {
        window.showToast("请选择或填写 Bot 名称");
        return;
    }

    summary.innerHTML = "正在筛选候选记忆...";
    summary.style.borderLeftColor = "var(--primary)";
    document.getElementById("memory-clean-candidate-list").innerHTML = "";
    document.getElementById("memory-clean-selection-tools").style.display = "none";
    document.getElementById("btn-clean-selected-memories").disabled = true;

    try {
        const res = await window.apiPost("/memories/clean/candidates", criteria);
        if (res.status === "success" && res.data) {
            renderMemoryCleanCandidates(res.data.items, res.data.total, res.data.truncated);
        } else {
            summary.innerHTML = `筛选失败: ${res.message || "请求出错"}`;
            summary.style.borderLeftColor = "var(--danger)";
        }
    } catch (e) {
        summary.innerHTML = `筛选出错: ${e.message}`;
        summary.style.borderLeftColor = "var(--danger)";
    }
};

window.updateMemoryCleanSelectionState = function() {
    const checkboxes = Array.from(document.querySelectorAll("#memory-clean-candidate-list .memory-clean-checkbox"));
    const selected = checkboxes.filter(chk => chk.checked).length;
    const selectedText = document.getElementById("memory-clean-selected-count");
    if (selectedText) {
        selectedText.textContent = `已选择 ${selected} / ${checkboxes.length} 条`;
    }
    const cleanBtn = document.getElementById("btn-clean-selected-memories");
    if (cleanBtn) {
        cleanBtn.disabled = selected === 0;
    }
};

window.toggleAllMemoryCleanCandidates = function(checked) {
    document.querySelectorAll("#memory-clean-candidate-list .memory-clean-checkbox").forEach(chk => {
        chk.checked = checked;
    });
    window.updateMemoryCleanSelectionState();
};

window.invertMemoryCleanCandidates = function() {
    document.querySelectorAll("#memory-clean-candidate-list .memory-clean-checkbox").forEach(chk => {
        chk.checked = !chk.checked;
    });
    window.updateMemoryCleanSelectionState();
};

window.cleanSelectedMemories = function() {
    const selectedIds = Array.from(document.querySelectorAll("#memory-clean-candidate-list .memory-clean-checkbox:checked"))
        .map(chk => chk.getAttribute("data-memory-id"))
        .filter(Boolean);

    if (selectedIds.length === 0) {
        window.showToast("没有选中的记忆");
        return;
    }

    window.showConfirm("确认清理长期记忆", `确定要清理选中的 ${selectedIds.length} 条长期记忆吗？此操作无法撤销。`, async () => {
        try {
            const res = await window.apiPost("/memories/clean", { memory_ids: selectedIds });
            if (res.status === "success") {
                const failedCount = Array.isArray(res.failed_ids) ? res.failed_ids.length : 0;
                const suffix = failedCount > 0 ? `，${failedCount} 条未能删除` : "";
                window.showToast(`已清理 ${res.deleted_count || selectedIds.length} 条长期记忆${suffix}`);
                window.closeModal("memory-clean-modal");
                window.GiftiaApp.resetPagination("memories");
                await window.GiftiaApp.refreshScopedFilters("memories", true);
                await window.GiftiaApp.loadMemories();
            } else {
                window.showToast(`清理失败: ${res.message || "请求出错"}`);
            }
        } catch (e) {
            window.showToast(`发生错误: ${e.message}`);
        }
    });
};

window.loadAutoCleanMemoryConfig = async function() {
    const summary = document.getElementById("auto-clean-memory-summary");
    summary.innerHTML = "正在加载自动清理配置...";
    summary.style.borderLeftColor = "var(--primary)";

    try {
        const res = await window.apiGet("/memories/auto_clean/config");
        if (res.status !== "success" || !res.config) {
            throw new Error(res.message || "加载配置失败");
        }

        const cfg = res.config;
        document.getElementById("auto-clean-mem-enabled").checked = Boolean(cfg.enabled);
        document.getElementById("auto-clean-mem-max-importance").value = normalizeMemoryCleanNumber(cfg.max_importance, 3, 1, 7);
        document.getElementById("auto-clean-mem-max-hit-count").value = normalizeMemoryCleanNumber(cfg.max_hit_count, 1, 0);
        document.getElementById("auto-clean-mem-min-age-days").value = normalizeMemoryCleanNumber(cfg.min_age_days, 60, 7);
        document.getElementById("auto-clean-mem-last-hit-before-days").value = normalizeMemoryCleanNumber(cfg.last_hit_before_days, 30, 7);
        document.getElementById("auto-clean-mem-max-delete-per-run").value = normalizeMemoryCleanNumber(cfg.max_delete_per_run, 20, 1, 200);
        document.getElementById("auto-clean-mem-include-never-hit").checked = cfg.include_never_hit !== false;
        summary.innerHTML = "自动清理不会删除重要度 8 以上、创建不足 7 天、最近 7 天命中过的记忆。";
        summary.style.borderLeftColor = "var(--primary)";
    } catch (e) {
        summary.innerHTML = `加载配置失败: ${e.message}`;
        summary.style.borderLeftColor = "var(--danger)";
    }
};

function collectAutoCleanMemoryConfig() {
    return {
        enabled: document.getElementById("auto-clean-mem-enabled").checked,
        max_importance: normalizeMemoryCleanNumber(document.getElementById("auto-clean-mem-max-importance").value, 3, 1, 7),
        max_hit_count: normalizeMemoryCleanNumber(document.getElementById("auto-clean-mem-max-hit-count").value, 1, 0),
        min_age_days: normalizeMemoryCleanNumber(document.getElementById("auto-clean-mem-min-age-days").value, 60, 7),
        last_hit_before_days: normalizeMemoryCleanNumber(document.getElementById("auto-clean-mem-last-hit-before-days").value, 30, 7),
        include_never_hit: document.getElementById("auto-clean-mem-include-never-hit").checked,
        max_delete_per_run: normalizeMemoryCleanNumber(document.getElementById("auto-clean-mem-max-delete-per-run").value, 20, 1, 200),
        cron: "30 3 * * *"
    };
}

window.saveAutoCleanMemoryConfig = async function() {
    const summary = document.getElementById("auto-clean-memory-summary");
    const config = collectAutoCleanMemoryConfig();
    try {
        const res = await window.apiPost("/memories/auto_clean/config", config);
        if (res.status === "success") {
            window.showToast("自动清理配置保存成功！");
            summary.innerHTML = "配置已保存。启用后每天凌晨 03:00 执行自动清理（作为每日统一清理任务执行）。";
            summary.style.borderLeftColor = "var(--success)";
        } else {
            window.showToast(`保存失败: ${res.message || "请求出错"}`);
        }
    } catch (e) {
        window.showToast(`保存配置出错: ${e.message}`);
    }
};

window.triggerAutoCleanMemoriesImmediately = function() {
    window.showConfirm("确认执行自动清理", "确认要立即按当前自动清理配置清理长期记忆吗？此操作无法撤销。", async () => {
        try {
            await window.apiPost("/memories/auto_clean/config", collectAutoCleanMemoryConfig());
            const res = await window.apiPost("/memories/auto_clean/trigger", {});
            if (res.status === "success") {
                const count = res.deleted_count ?? res.count ?? 0;
                window.showToast(`自动清理完成，共删除 ${count} 条长期记忆`);
                window.closeModal("memory-clean-modal");
                window.GiftiaApp.resetPagination("memories");
                await window.GiftiaApp.refreshScopedFilters("memories", true);
                await window.GiftiaApp.loadMemories();
            } else {
                window.showToast(`执行失败: ${res.message || "请求出错"}`);
            }
        } catch (e) {
            window.showToast(`执行自动清理出错: ${e.message}`);
        }
    });
};
