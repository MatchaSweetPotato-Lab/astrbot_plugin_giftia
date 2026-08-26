import { state } from '../state.js';

/** 当前页的表情包数据，供事件委托读取（键为 sticker_id） */
let currentStickers = new Map();

/** 供其他模块（弹窗）读取当前页某张表情包的完整数据 */
export function getCurrentSticker(stickerId) {
    return currentStickers.get(stickerId) || null;
}

/** 读取表情包筛选栏的当前取值 */
function getStickerFilterParams() {
    return {
        page: state.pagination.stickers.page,
        limit: state.pagination.stickers.limit,
        bot_name: document.getElementById("sticker-bot-name")?.value || "",
        category: document.getElementById("sticker-category")?.value || "",
        tag: document.getElementById("sticker-tag")?.value || "",
        search: document.getElementById("sticker-search")?.value || "",
    };
}

/** 把选项列表填进 select，保留当前选中值 */
function fillSelect(selectEl, values, allLabel) {
    if (!selectEl) return;
    const previous = selectEl.value;
    selectEl.innerHTML = "";
    selectEl.append(new Option(allLabel, ""));
    values.forEach(v => selectEl.append(new Option(v, v)));
    if (previous && values.includes(previous)) {
        selectEl.value = previous;
    }
}

/** 加载分类/标签/机器人筛选项，供筛选栏与各弹窗复用 */
export async function loadStickerFilterOptions() {
    try {
        const res = await window.apiGet("/stickers/filter_options");
        if (res.status === "success" && res.data) {
            state.stickerOptions = {
                categories: res.data.categories || [],
                tags: res.data.tags || [],
                bots: res.data.bots || [],
                ai_available: !!res.data.ai_available,
            };

            fillSelect(document.getElementById("sticker-category"), state.stickerOptions.categories, "全部分类");
            fillSelect(document.getElementById("sticker-tag"), state.stickerOptions.tags, "全部标签");

            const botSelect = document.getElementById("sticker-bot-name");
            if (botSelect) {
                const previous = botSelect.value;
                botSelect.innerHTML = "";
                botSelect.append(new Option("全部机器人", ""));
                state.stickerOptions.bots.forEach(b => botSelect.append(new Option(b, b)));
                if (previous && state.stickerOptions.bots.includes(previous)) {
                    botSelect.value = previous;
                }
            }
        }
    } catch (e) {
        console.error("加载表情包筛选项失败:", e);
    }
    return state.stickerOptions;
}

export async function loadStickers() {
    const container = document.getElementById("sticker-list");
    if (!container) return;
    container.innerHTML = `<div class="loading-row flex-grow"><span class="loader"></span> 加载表情包中...</div>`;

    try {
        const res = await window.apiGet("/stickers", getStickerFilterParams());
        if (res.status === "success" && res.data) {
            state.pagination.stickers.total = res.data.total;
            renderStickers(res.data.items);
            window.renderPagination("sticker-pagination", state.pagination.stickers, (page) => {
                state.pagination.stickers.page = page;
                loadStickers();
            });
        } else {
            throw new Error(res.message || "请求失败");
        }
    } catch (e) {
        container.innerHTML = `<div class="no-data-row flex-grow">加载失败: ${window.escapeHtml(e.message)}</div>`;
    }
}

/** 初始化表情包 tab：先拉筛选项，再拉列表 */
export async function initializeStickersTab() {
    await loadStickerFilterOptions();
    await loadStickerGifConfig();
    await loadStickers();
}

/* ── 「以 GIF 格式发送」per-bot 开关 ──────────────────────────────────────
 *
 * 官方 QQ 不支持小图表情包外显，表情包会以大图发出占屏；开关打开后后端发送前
 * 会把表情包转成 GIF，客户端便按表情包渲染。开关按机器人独立存在
 * bots_config.json 里，切换即时生效，无需重载插件。
 */

/** DOM id 安全化：机器人名可以是任意字符，不能直接拼进 id */
function sanitizeDomId(str) {
    return String(str || "").replace(/[^a-zA-Z0-9_-]/g, "_");
}

export async function loadStickerGifConfig() {
    const container = document.getElementById("sticker-gif-switches");
    if (!container) return;

    setupStickerGifDelegation();

    try {
        const res = await window.apiGet("/stickers/gif_config");
        if (res.status !== "success" || !res.data) {
            throw new Error(res.message || "请求失败");
        }

        const bots = res.data.bots || [];
        if (!bots.length) {
            container.innerHTML = `<div class="sticker-gif-empty">还没有配置机器人。到「机器人管理」页签创建一个，再回来设置。</div>`;
            return;
        }

        container.innerHTML = bots.map(bot => {
            const domId = `sticker-gif-toggle-${sanitizeDomId(bot.name)}`;
            const on = !!bot.send_sticker_as_gif;
            const badges = [
                bot.is_qq_official
                    ? `<span class="badge badge-info" title="官方 QQ 无法发送小图表情包，开启后可明显节省会话空间">官方 QQ · 建议开启</span>`
                    : "",
                bot.enabled === false
                    ? `<span class="badge badge-secondary" title="该机器人已暂停，开关会保留但暂时不生效">已暂停</span>`
                    : "",
            ].join("");

            return `
                <div class="sticker-gif-row" data-bot-name="${window.escapeHtml(bot.name)}">
                    <div class="sticker-gif-row-main">
                        <span class="sticker-gif-bot-name" title="${window.escapeHtml(bot.name)}">${window.escapeHtml(bot.name)}</span>
                        ${badges}
                    </div>
                    <div class="switch-container" title="${on ? "关闭后按原文件格式发送" : "开启后统一转 GIF 发送"}">
                        <input type="checkbox" class="switch-checkbox sticker-gif-checkbox" id="${domId}"
                               data-bot-name="${window.escapeHtml(bot.name)}" ${on ? "checked" : ""}>
                        <label for="${domId}" class="switch-label"></label>
                    </div>
                </div>
            `;
        }).join("");
    } catch (e) {
        container.innerHTML = `<div class="sticker-gif-empty">加载失败: ${window.escapeHtml(e.message)}</div>`;
    }
}

/**
 * 开关走事件委托：机器人名是用户填的任意字符串，不编进 inline onchange 字符串，
 * 与本文件的 setupStickerListDelegation 保持同一套做法。
 */
function setupStickerGifDelegation() {
    const container = document.getElementById("sticker-gif-switches");
    if (!container || container.dataset.delegated === "1") return;
    container.dataset.delegated = "1";

    container.addEventListener("change", async (e) => {
        const checkbox = e.target.closest(".sticker-gif-checkbox");
        if (!checkbox) return;

        const botName = checkbox.getAttribute("data-bot-name");
        const desired = checkbox.checked;
        checkbox.disabled = true;
        try {
            const res = await window.apiPost("/stickers/gif_config", {
                bot_name: botName,
                send_sticker_as_gif: desired,
            });
            if (res && res.status === "success") {
                window.showToast(res.message || "已保存");
            } else {
                // 失败回滚，避免界面显示的状态和后端不一致
                checkbox.checked = !desired;
                window.showToast(res?.message || "保存失败");
            }
        } catch (err) {
            checkbox.checked = !desired;
            window.showToast("网络错误，保存失败");
        } finally {
            checkbox.disabled = false;
        }
    });
}

/** 懒加载表情包图片（默认缩略图，hover 后换原图以便看到 GIF 动画） */
export async function loadStickerImage(stickerId, elementId, isThumbnail = true) {
    const el = document.getElementById(elementId);
    if (!el) return;

    try {
        const endpoint = isThumbnail
            ? `/stickers/file/thumbnail/b64/${stickerId}`
            : `/stickers/file/b64/${stickerId}`;
        const res = await window.apiGet(endpoint);
        if (res && res.status === "success" && res.base64) {
            el.src = `data:${res.content_type || "image/png"};base64,${res.base64}`;
        } else {
            renderMissingImage(el);
        }
    } catch (e) {
        console.error("加载表情包图片失败:", stickerId, e);
        renderMissingImage(el);
    }
}

function renderMissingImage(imgEl) {
    const box = imgEl.closest(".sticker-preview-box");
    if (box) {
        box.innerHTML = `
            <div class="sticker-missing">
                <div class="sticker-missing-icon">🖼️</div>
                <div class="sticker-missing-hint">图片文件缺失</div>
            </div>
        `;
    }
}

/** 同步「已选 N 项」批量操作条与全选框状态 */
export function updateStickerSelectionUI() {
    const count = state.selectedStickers.size;
    const bar = document.getElementById("sticker-batch-bar");
    const countEl = document.getElementById("sticker-selected-count");

    if (bar) bar.style.display = count > 0 ? "flex" : "none";
    if (countEl) countEl.textContent = String(count);

    const cards = document.querySelectorAll("#sticker-list .sticker-card");
    const checkboxes = document.querySelectorAll("#sticker-list .sticker-select-checkbox");
    const selectAll = document.getElementById("sticker-select-all");
    if (selectAll) {
        const total = checkboxes.length;
        const selectedOnPage = Array.from(checkboxes).filter(cb => cb.checked).length;
        selectAll.checked = total > 0 && selectedOnPage === total;
        selectAll.indeterminate = selectedOnPage > 0 && selectedOnPage < total;
    }

    cards.forEach(card => {
        const id = card.getAttribute("data-sticker-id");
        card.classList.toggle("selected", state.selectedStickers.has(id));
    });
}

window.toggleStickerSelection = function(stickerId, checked) {
    if (checked) {
        state.selectedStickers.add(stickerId);
    } else {
        state.selectedStickers.delete(stickerId);
    }
    updateStickerSelectionUI();
};

window.toggleAllStickersOnPage = function(checked) {
    document.querySelectorAll("#sticker-list .sticker-select-checkbox").forEach(cb => {
        cb.checked = checked;
        const id = cb.getAttribute("data-sticker-id");
        if (checked) {
            state.selectedStickers.add(id);
        } else {
            state.selectedStickers.delete(id);
        }
    });
    updateStickerSelectionUI();
};

window.clearStickerSelection = function() {
    state.selectedStickers.clear();
    document.querySelectorAll("#sticker-list .sticker-select-checkbox").forEach(cb => {
        cb.checked = false;
    });
    updateStickerSelectionUI();
};

/** 点击卡片上的分类/标签徽章，直接筛选 */
window.filterStickersByCategory = async function(category) {
    const el = document.getElementById("sticker-category");
    if (el) {
        if (!Array.from(el.options).some(o => o.value === category)) {
            el.append(new Option(category, category));
        }
        el.value = category;
        state.pagination.stickers.page = 1;
        await loadStickers();
    }
};

window.filterStickersByTag = async function(tag) {
    const el = document.getElementById("sticker-tag");
    if (el) {
        if (!Array.from(el.options).some(o => o.value === tag)) {
            el.append(new Option(tag, tag));
        }
        el.value = tag;
        state.pagination.stickers.page = 1;
        await loadStickers();
    }
};

/**
 * 画廊内的交互统一走事件委托：卡片是动态渲染的，用户填的名称/标签
 * 不必编码进 onclick 字符串，省掉一层转义风险。
 */
function setupStickerListDelegation() {
    const container = document.getElementById("sticker-list");
    if (!container || container.dataset.delegated === "1") return;
    container.dataset.delegated = "1";

    container.addEventListener("change", (e) => {
        const checkbox = e.target.closest(".sticker-select-checkbox");
        if (checkbox) {
            window.toggleStickerSelection(
                checkbox.getAttribute("data-sticker-id"),
                checkbox.checked
            );
        }
    });

    container.addEventListener("click", (e) => {
        const tagChip = e.target.closest("[data-sticker-tag]");
        if (tagChip) {
            window.filterStickersByTag(tagChip.getAttribute("data-sticker-tag"));
            return;
        }

        const categoryBadge = e.target.closest("[data-sticker-category]");
        if (categoryBadge) {
            window.filterStickersByCategory(categoryBadge.getAttribute("data-sticker-category"));
            return;
        }

        const actionBtn = e.target.closest("[data-sticker-action]");
        if (!actionBtn) return;

        const card = actionBtn.closest(".sticker-card");
        const stickerId = card?.getAttribute("data-sticker-id");
        const sticker = currentStickers.get(stickerId);
        if (!sticker) return;

        const action = actionBtn.getAttribute("data-sticker-action");
        if (action === "edit" && typeof window.openEditStickerModal === "function") {
            window.openEditStickerModal(sticker);
        } else if (action === "delete" && typeof window.deleteSticker === "function") {
            window.deleteSticker(sticker.sticker_id, sticker.name);
        }
    });
}

export function renderStickers(items) {
    const container = document.getElementById("sticker-list");
    if (!container) return;

    setupStickerListDelegation();

    if (!items || items.length === 0) {
        currentStickers = new Map();
        container.innerHTML = `<div class="no-data-row flex-grow">暂无表情包。可以点击右上角「上传表情包」手动添加，或等机器人自己收藏。</div>`;
        updateStickerSelectionUI();
        return;
    }

    container.innerHTML = items.map(item => {
        const uniqueId = `sticker-preview-${item.sticker_id}`;
        const isSelected = state.selectedStickers.has(item.sticker_id);

        const tagsHtml = (item.tags || []).length
            ? item.tags.map(t => `
                <span class="sticker-tag-chip" data-sticker-tag="${window.escapeHtml(t)}" title="按此标签筛选">${window.escapeHtml(t)}</span>
              `).join("")
            : `<span class="sticker-tag-empty">无标签</span>`;

        const botsHtml = (item.bot_names || []).length
            ? `<span class="sticker-bot-badge" title="${window.escapeHtml(item.bot_names.join("、"))}">🤖 ${item.bot_names.length} 个</span>`
            : `<span class="sticker-bot-badge sticker-bot-badge-empty" title="没有任何机器人收藏它，因此永远不会被发送">🤖 未归属</span>`;

        const preview = item.has_file
            ? `<img id="${uniqueId}" alt="${window.escapeHtml(item.name)}">`
            : `<div class="sticker-missing"><div class="sticker-missing-icon">🖼️</div><div class="sticker-missing-hint">图片文件缺失</div></div>`;

        return `
            <div class="sticker-card card ${isSelected ? "selected" : ""}" data-sticker-id="${item.sticker_id}">
                <div class="sticker-card-top">
                    <label class="sticker-select-label" title="选择">
                        <input type="checkbox" class="sticker-select-checkbox" data-sticker-id="${item.sticker_id}"
                               ${isSelected ? "checked" : ""}>
                    </label>
                    ${botsHtml}
                </div>
                <div class="sticker-preview-box">${preview}</div>
                <div class="sticker-info">
                    <div class="sticker-name" title="${window.escapeHtml(item.name)}">${window.escapeHtml(item.name || "未命名")}</div>
                    <div class="sticker-meta-row">
                        <span class="badge badge-secondary sticker-category-badge"
                              data-sticker-category="${window.escapeHtml(item.category || "")}" title="按此分类筛选">
                            ${window.escapeHtml(item.category || "未分类")}
                        </span>
                    </div>
                    <div class="sticker-tags-row">${tagsHtml}</div>
                    <div class="sticker-desc" title="${window.escapeHtml(item.description || "")}">${window.escapeHtml(item.description || "暂无描述")}</div>
                </div>
                <div class="sticker-actions">
                    <button class="btn btn-secondary btn-small" data-sticker-action="edit">编辑</button>
                    <button class="btn btn-danger btn-small" data-sticker-action="delete">删除</button>
                </div>
            </div>
        `;
    }).join("");

    // 把本页数据挂到模块作用域，供事件委托读取（避免把用户数据编码进 onclick 字符串）
    currentStickers = new Map(items.map(i => [i.sticker_id, i]));

    // 缩略图懒加载 + hover 500ms 后换原图（GIF 才会动）
    const hoverTimers = new Map();
    items.forEach(item => {
        if (!item.has_file) return;

        const uniqueId = `sticker-preview-${item.sticker_id}`;
        const alreadyOriginal = state.loadedOriginalStickers.has(item.sticker_id);
        loadStickerImage(item.sticker_id, uniqueId, !alreadyOriginal);

        const imgEl = document.getElementById(uniqueId);
        const box = imgEl ? imgEl.closest(".sticker-preview-box") : null;
        if (!box) return;

        box.addEventListener("mouseenter", () => {
            if (state.loadedOriginalStickers.has(item.sticker_id)) return;
            const timer = setTimeout(() => {
                loadStickerImage(item.sticker_id, uniqueId, false);
                state.loadedOriginalStickers.add(item.sticker_id);
            }, 500);
            hoverTimers.set(item.sticker_id, timer);
        });

        box.addEventListener("mouseleave", () => {
            if (hoverTimers.has(item.sticker_id)) {
                clearTimeout(hoverTimers.get(item.sticker_id));
                hoverTimers.delete(item.sticker_id);
            }
        });
    });

    updateStickerSelectionUI();
}
