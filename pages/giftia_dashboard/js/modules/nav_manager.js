// Giftia Dashboard Navigation Manager
// Handles customizable pinned tabs, overflow dropdown, and navigation modal

export const ALL_TABS = [
    {
        id: 'chat-history',
        name: '决策审计',
        icon: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>'
    },
    {
        id: 'memories',
        name: '长期记忆',
        icon: '<path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"></path><path d="M12 6v6l4 2"></path>'
    },
    {
        id: 'profiles',
        name: '画像管理',
        icon: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M23 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path>'
    },
    {
        id: 'slang',
        name: '黑话管理',
        icon: '<path d="M12 3v18M3 4h5a4 4 0 0 1 4 2 4 4 0 0 1 4-2h5v15h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3z"></path>'
    },
    {
        id: 'bot-status',
        name: '会话状态',
        icon: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path><path d="M13.73 21a2 2 0 0 1-3.46 0"></path>'
    },
    {
        id: 'bots',
        name: '机器人管理',
        icon: '<rect x="4" y="8" width="16" height="12" rx="2"></rect><path d="M9 13v2"></path><path d="M15 13v2"></path>'
    },
    {
        id: 'report-templates',
        name: '报告模板',
        icon: '<rect x="3" y="3" width="18" height="18" rx="2"></rect><path d="M7 8h10M7 12h6M7 16h10"></path>'
    },
    {
        id: 'media-captions',
        name: '媒体转述',
        icon: '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path><circle cx="12" cy="13" r="4"></circle>'
    },
    {
        id: 'stickers',
        name: '表情包管理',
        icon: '<circle cx="12" cy="12" r="10"></circle><path d="M8 14s1.5 2 4 2 4-2 4-2"></path><line x1="9" y1="9" x2="9.01" y2="9"></line><line x1="15" y1="9" x2="15.01" y2="9"></line>'
    },
    {
        id: 'forward-messages',
        name: '合并转发',
        icon: '<rect x="3" y="4" width="18" height="14" rx="2"></rect><path d="M7 8h10"></path><path d="M7 12h6"></path><path d="M8 22l4-4 4 4"></path>'
    },
    {
        id: 'token-stats',
        name: 'Token统计',
        icon: '<line x1="12" y1="1" x2="12" y2="23"></line><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"></path>'
    }
];

export const DEFAULT_PINNED_IDS = [
    'chat-history',
    'memories',
    'profiles',
    'slang',
    'bot-status',
    'bots'
];

let draftModalConfig = null;
let currentNavConfig = null;

export function getDefaultNavConfig() {
    return ALL_TABS.map(tab => ({
        id: tab.id,
        pinned: DEFAULT_PINNED_IDS.includes(tab.id)
    }));
}

export function sanitizeNavConfig(parsed) {
    if (!Array.isArray(parsed) || parsed.length === 0) {
        return getDefaultNavConfig();
    }

    const validIds = new Set(ALL_TABS.map(t => t.id));
    const userItems = parsed
        .filter(item => item && validIds.has(item.id))
        .map(item => ({
            id: item.id,
            pinned: Boolean(item.pinned)
        }));
    const includedIds = new Set(userItems.map(item => item.id));

    // Append any newly added tabs that weren't in user's saved config
    ALL_TABS.forEach(tab => {
        if (!includedIds.has(tab.id)) {
            userItems.push({
                id: tab.id,
                pinned: DEFAULT_PINNED_IDS.includes(tab.id)
            });
        }
    });

    return userItems;
}

export function loadNavConfig() {
    if (currentNavConfig) {
        return currentNavConfig;
    }
    currentNavConfig = getDefaultNavConfig();
    return currentNavConfig;
}

export function saveNavConfig(config) {
    currentNavConfig = sanitizeNavConfig(config);
}

export async function syncNavConfigFromBackend() {
    if (!window.apiGet) return;
    try {
        const res = await window.apiGet('/settings/nav_config');
        if (res && res.status === 'success' && Array.isArray(res.data) && res.data.length > 0) {
            const syncedConfig = sanitizeNavConfig(res.data);
            currentNavConfig = syncedConfig;
            // Only update active navbar if user is not currently editing in modal
            if (!draftModalConfig) {
                const activeTabId = (window.GiftiaApp ? window.GiftiaApp.activeTab : null) || 'chat-history';
                renderNavbar(activeTabId);
            }
        }
    } catch (e) {
        // Silently ignore network or API errors
    }
}

export async function persistNavConfigToBackend(config) {
    if (!window.apiPost) return;
    try {
        await window.apiPost('/settings/nav_config', { config });
    } catch (e) {
        console.warn('Failed to persist nav tabs config to server:', e);
    }
}

export function switchToTab(tabId) {
    const targetPanel = document.getElementById(`tab-${tabId}`);
    if (!targetPanel) return;

    // Deactivate all panels
    document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.remove('active'));
    targetPanel.classList.add('active');

    // Update global activeTab
    if (window.GiftiaApp) {
        window.GiftiaApp.activeTab = tabId;
    }

    // Update visual active state across navbar
    updateNavbarActiveState(tabId);

    // Close More dropdown if open
    closeMoreDropdown();

    // Trigger data loading for active tab
    if (window.GiftiaApp && typeof window.GiftiaApp.loadActiveTabData === 'function') {
        window.GiftiaApp.loadActiveTabData();
    }
}

export function updateNavbarActiveState(activeTabId) {
    const config = loadNavConfig();
    const activeConfig = config.find(item => item.id === activeTabId);
    const isPinned = activeConfig ? activeConfig.pinned : true;

    // Update pinned tabs active styles
    document.querySelectorAll('#nav-pinned-container .nav-tab').forEach(btn => {
        const tab = btn.getAttribute('data-tab');
        if (tab === activeTabId) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });

    // Update items inside More dropdown
    document.querySelectorAll('#nav-more-items .nav-more-item').forEach(btn => {
        const tab = btn.getAttribute('data-tab');
        if (tab === activeTabId) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });

    // Update More button appearance
    const moreBtn = document.getElementById('nav-more-btn');
    const moreLabel = document.getElementById('nav-more-label');
    if (moreBtn && moreLabel) {
        if (!isPinned) {
            const activeMeta = ALL_TABS.find(t => t.id === activeTabId);
            moreBtn.classList.add('active');
            moreLabel.textContent = activeMeta ? `更多 · ${activeMeta.name}` : '更多';
        } else {
            moreBtn.classList.remove('active');
            moreLabel.textContent = '更多';
        }
    }
}

export function renderNavbar(currentActiveTab = null) {
    const pinnedContainer = document.getElementById('nav-pinned-container');
    const moreItemsContainer = document.getElementById('nav-more-items');
    const moreWrapper = document.getElementById('nav-more-wrapper');
    if (!pinnedContainer || !moreItemsContainer || !moreWrapper) return;

    const activeId = currentActiveTab || (window.GiftiaApp ? window.GiftiaApp.activeTab : 'chat-history') || 'chat-history';
    const config = loadNavConfig();
    const metaMap = new Map(ALL_TABS.map(t => [t.id, t]));

    pinnedContainer.innerHTML = '';
    moreItemsContainer.innerHTML = '';

    const pinnedItems = config.filter(c => c.pinned);
    const unpinnedItems = config.filter(c => !c.pinned);

    // 1. Render Pinned Tabs
    pinnedItems.forEach(item => {
        const meta = metaMap.get(item.id);
        if (!meta) return;

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `nav-tab ${meta.id === activeId ? 'active' : ''}`;
        btn.setAttribute('data-tab', meta.id);
        btn.innerHTML = `
            <svg class="tab-icon" viewBox="0 0 24 24">${meta.icon}</svg>
            <span class="tab-label">${meta.name}</span>
        `;
        btn.addEventListener('click', () => switchToTab(meta.id));
        pinnedContainer.appendChild(btn);
    });

    // 2. Render Unpinned Tabs into More Dropdown
    if (unpinnedItems.length === 0) {
        moreWrapper.style.display = 'none';
    } else {
        moreWrapper.style.display = '';
        unpinnedItems.forEach(item => {
            const meta = metaMap.get(item.id);
            if (!meta) return;

            const itemBtn = document.createElement('button');
            itemBtn.type = 'button';
            itemBtn.className = `nav-more-item ${meta.id === activeId ? 'active' : ''}`;
            itemBtn.setAttribute('data-tab', meta.id);
            itemBtn.innerHTML = `
                <svg class="tab-icon" viewBox="0 0 24 24">${meta.icon}</svg>
                <span class="tab-label">${meta.name}</span>
            `;
            itemBtn.addEventListener('click', () => switchToTab(meta.id));
            moreItemsContainer.appendChild(itemBtn);
        });
    }

    // 3. Update active state on More button
    updateNavbarActiveState(activeId);
}

export function closeMoreDropdown() {
    const dropdown = document.getElementById('nav-more-dropdown');
    const moreBtn = document.getElementById('nav-more-btn');
    if (dropdown) dropdown.classList.remove('show');
    if (moreBtn) {
        moreBtn.classList.remove('open');
        moreBtn.setAttribute('aria-expanded', 'false');
    }
}

export function toggleMoreDropdown() {
    const dropdown = document.getElementById('nav-more-dropdown');
    const moreBtn = document.getElementById('nav-more-btn');
    if (dropdown && moreBtn) {
        const isOpen = dropdown.classList.contains('show');
        if (isOpen) {
            dropdown.classList.remove('show');
            moreBtn.classList.remove('open');
            moreBtn.setAttribute('aria-expanded', 'false');
        } else {
            dropdown.classList.add('show');
            moreBtn.classList.add('open');
            moreBtn.setAttribute('aria-expanded', 'true');
        }
    }
}

// =========================================================================
// Customization Modal Logic
// =========================================================================

let isNavCustomEventsBound = false;

function setupNavCustomListEvents() {
    if (isNavCustomEventsBound) return;
    const listContainer = document.getElementById('nav-custom-list');
    if (!listContainer) return;
    isNavCustomEventsBound = true;

    let draggedIndex = null;
    let touchDraggedIndex = null;
    let touchCurrentTarget = null;
    let touchPlaceAfter = false;

    // 1. Delegated Action Buttons (Up / Down)
    listContainer.addEventListener('click', (e) => {
        const btn = e.target.closest('.nav-custom-action-btn');
        if (!btn || btn.disabled) return;

        const action = btn.dataset.action;
        const index = parseInt(btn.dataset.index, 10);
        if (isNaN(index)) return;

        if (action === 'up' && index > 0) {
            reorderDraftItem(index, index - 1);
        } else if (action === 'down' && index < draftModalConfig.length - 1) {
            reorderDraftItem(index, index + 1);
        }
    });

    // 2. Delegated Change Listener (Pin Toggle)
    listContainer.addEventListener('change', (e) => {
        const toggle = e.target.closest('.nav-pin-toggle');
        if (!toggle) return;

        const index = parseInt(toggle.dataset.index, 10);
        if (isNaN(index) || !draftModalConfig || !draftModalConfig[index]) return;

        draftModalConfig[index].pinned = toggle.checked;
        renderNavCustomModalList();
    });

    // 3. Desktop Drag & Drop Listeners
    listContainer.addEventListener('dragstart', (e) => {
        if (e.target.closest('input, button, label, .switch-container')) {
            e.preventDefault();
            return;
        }

        const card = e.target.closest('.nav-custom-item-row');
        if (!card) return;

        draggedIndex = parseInt(card.dataset.index, 10);
        card.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', String(draggedIndex));
    });

    listContainer.addEventListener('dragend', () => {
        const cards = listContainer.querySelectorAll('.nav-custom-item-row');
        cards.forEach(c => {
            c.classList.remove('dragging', 'drag-over-top', 'drag-over-bottom');
        });
        draggedIndex = null;
    });

    listContainer.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        if (draggedIndex === null) return;

        const card = e.target.closest('.nav-custom-item-row');
        if (!card) return;

        const targetIndex = parseInt(card.dataset.index, 10);
        const cards = listContainer.querySelectorAll('.nav-custom-item-row');
        cards.forEach(c => c.classList.remove('drag-over-top', 'drag-over-bottom'));

        if (targetIndex === draggedIndex) return;

        const rect = card.getBoundingClientRect();
        const midY = rect.top + rect.height / 2;
        if (e.clientY < midY) {
            card.classList.add('drag-over-top');
        } else {
            card.classList.add('drag-over-bottom');
        }
    });

    listContainer.addEventListener('dragleave', (e) => {
        const card = e.target.closest('.nav-custom-item-row');
        if (card && !card.contains(e.relatedTarget)) {
            card.classList.remove('drag-over-top', 'drag-over-bottom');
        }
    });

    listContainer.addEventListener('drop', (e) => {
        e.preventDefault();
        if (draggedIndex === null) return;

        const card = e.target.closest('.nav-custom-item-row');
        if (!card) return;

        const targetIndex = parseInt(card.dataset.index, 10);
        const rect = card.getBoundingClientRect();
        const midY = rect.top + rect.height / 2;
        const placeAfter = e.clientY >= midY;

        let newIndex = targetIndex;
        if (placeAfter && draggedIndex > targetIndex) {
            newIndex = targetIndex + 1;
        } else if (!placeAfter && draggedIndex < targetIndex) {
            newIndex = targetIndex - 1;
        }

        if (newIndex !== draggedIndex && newIndex >= 0 && newIndex < draftModalConfig.length) {
            reorderDraftItem(draggedIndex, newIndex);
        } else if (newIndex !== draggedIndex) {
            reorderDraftItem(draggedIndex, targetIndex);
        }

        const cards = listContainer.querySelectorAll('.nav-custom-item-row');
        cards.forEach(c => c.classList.remove('drag-over-top', 'drag-over-bottom'));
    });

    // 4. Touch Drag Listeners for Mobile
    listContainer.addEventListener('touchstart', (e) => {
        const handle = e.target.closest('.nav-custom-drag-handle');
        if (!handle) return;

        const card = handle.closest('.nav-custom-item-row');
        if (!card) return;

        touchDraggedIndex = parseInt(card.dataset.index, 10);
        card.classList.add('dragging');
    }, { passive: true });

    listContainer.addEventListener('touchmove', (e) => {
        if (touchDraggedIndex === null) return;

        const touch = e.touches[0];
        const targetElement = document.elementFromPoint(touch.clientX, touch.clientY);
        const card = targetElement ? targetElement.closest('.nav-custom-item-row') : null;

        const allCards = listContainer.querySelectorAll('.nav-custom-item-row');
        allCards.forEach(c => c.classList.remove('drag-over-top', 'drag-over-bottom'));

        if (!card || parseInt(card.dataset.index, 10) === touchDraggedIndex) {
            touchCurrentTarget = null;
            return;
        }

        if (e.cancelable) e.preventDefault();

        touchCurrentTarget = card;
        const rect = card.getBoundingClientRect();
        const midY = rect.top + rect.height / 2;
        touchPlaceAfter = touch.clientY >= midY;

        if (touchPlaceAfter) {
            card.classList.add('drag-over-bottom');
        } else {
            card.classList.add('drag-over-top');
        }
    }, { passive: false });

    const cleanTouchState = () => {
        const allCards = listContainer.querySelectorAll('.nav-custom-item-row');
        allCards.forEach(c => {
            c.classList.remove('dragging', 'drag-over-top', 'drag-over-bottom');
        });
        touchDraggedIndex = null;
        touchCurrentTarget = null;
    };

    listContainer.addEventListener('touchend', () => {
        if (touchDraggedIndex === null) return;

        if (touchCurrentTarget) {
            const targetIndex = parseInt(touchCurrentTarget.dataset.index, 10);
            let newIndex = targetIndex;
            if (touchPlaceAfter && touchDraggedIndex > targetIndex) {
                newIndex = targetIndex + 1;
            } else if (!touchPlaceAfter && touchDraggedIndex < targetIndex) {
                newIndex = targetIndex - 1;
            }
            if (newIndex >= 0 && newIndex < draftModalConfig.length && newIndex !== touchDraggedIndex) {
                reorderDraftItem(touchDraggedIndex, newIndex);
            } else if (targetIndex !== touchDraggedIndex) {
                reorderDraftItem(touchDraggedIndex, targetIndex);
            }
        }
        cleanTouchState();
    });

    listContainer.addEventListener('touchcancel', cleanTouchState);
}

function reorderDraftItem(fromIndex, toIndex) {
    if (!draftModalConfig) return;
    if (fromIndex === toIndex || fromIndex < 0 || toIndex < 0 ||
        fromIndex >= draftModalConfig.length || toIndex >= draftModalConfig.length) {
        return;
    }
    const item = draftModalConfig.splice(fromIndex, 1)[0];
    draftModalConfig.splice(toIndex, 0, item);
    renderNavCustomModalList();
}

export function openNavCustomModal() {
    closeMoreDropdown();
    draftModalConfig = JSON.parse(JSON.stringify(loadNavConfig()));
    renderNavCustomModalList();
    if (typeof window.openModal === 'function') {
        window.openModal('nav-custom-modal');
    }
}

function renderNavCustomModalList() {
    const listContainer = document.getElementById('nav-custom-list');
    const pinnedCountEl = document.getElementById('nav-custom-pinned-count');
    if (!listContainer || !draftModalConfig) return;

    setupNavCustomListEvents();

    const metaMap = new Map(ALL_TABS.map(t => [t.id, t]));
    const pinnedCount = draftModalConfig.filter(item => item.pinned).length;
    if (pinnedCountEl) {
        pinnedCountEl.textContent = `已常驻 ${pinnedCount} 项`;
    }

    listContainer.innerHTML = draftModalConfig.map((item, index) => {
        const meta = metaMap.get(item.id);
        if (!meta) return '';

        const badgeClass = item.pinned ? 'primary-rank' : 'fallback-rank';
        const tagClass = item.pinned ? 'pinned-tag' : 'more-tag';
        const tagLabel = item.pinned ? '常驻' : '更多';

        return `
            <div class="nav-custom-item-row ${item.pinned ? 'is-pinned' : ''}" draggable="true" data-index="${index}" data-id="${item.id}">
                <span class="nav-custom-drag-handle" title="按住拖拽排序">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                        <circle cx="9" cy="5" r="1"></circle>
                        <circle cx="9" cy="12" r="1"></circle>
                        <circle cx="9" cy="19" r="1"></circle>
                        <circle cx="15" cy="5" r="1"></circle>
                        <circle cx="15" cy="12" r="1"></circle>
                        <circle cx="15" cy="19" r="1"></circle>
                    </svg>
                </span>

                <span class="nav-custom-rank-badge ${badgeClass}">#${index + 1}</span>

                <div class="nav-custom-tab-info">
                    <svg class="tab-icon" viewBox="0 0 24 24">${meta.icon}</svg>
                    <span class="nav-custom-tab-title" title="${meta.name}">${meta.name}</span>
                    <span class="nav-custom-tag ${tagClass}">${tagLabel}</span>
                </div>

                <div class="nav-custom-actions">
                    <button type="button" class="nav-custom-action-btn nav-custom-up-btn" data-action="up" data-index="${index}" ${index === 0 ? 'disabled' : ''} title="提升排序 (上移)">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                            <polyline points="18 15 12 9 6 15"></polyline>
                        </svg>
                    </button>
                    <button type="button" class="nav-custom-action-btn nav-custom-down-btn" data-action="down" data-index="${index}" ${index === draftModalConfig.length - 1 ? 'disabled' : ''} title="降低排序 (下移)">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                            <polyline points="6 9 12 15 18 9"></polyline>
                        </svg>
                    </button>
                    <div class="switch-container" title="${item.pinned ? '已常驻显示' : '收纳到更多'}">
                        <input type="checkbox" id="nav-pin-${meta.id}" class="switch-checkbox nav-pin-toggle" data-index="${index}" ${item.pinned ? 'checked' : ''}>
                        <label for="nav-pin-${meta.id}" class="switch-label"></label>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

export function resetNavCustomModalToDefault() {
    draftModalConfig = getDefaultNavConfig();
    renderNavCustomModalList();
    if (typeof window.showToast === 'function') {
        window.showToast('已恢复推荐默认排版');
    }
}

export function saveNavCustomModal() {
    if (!draftModalConfig) return;
    const toSave = JSON.parse(JSON.stringify(draftModalConfig));
    saveNavConfig(toSave);
    persistNavConfigToBackend(toSave);
    if (typeof window.closeModal === 'function') {
        window.closeModal('nav-custom-modal');
    }
    draftModalConfig = null;
    renderNavbar();
    if (typeof window.showToast === 'function') {
        window.showToast('导航设置已保存');
    }
}

// =========================================================================
// Initialization
// =========================================================================

export function initNavigation() {
    // 1. Initial render of navbar immediately using in-memory config or default
    const initialActiveTab = (window.GiftiaApp ? window.GiftiaApp.activeTab : null) || 'chat-history';
    renderNavbar(initialActiveTab);

    // 2. Asynchronously sync user's persisted nav configuration from backend
    syncNavConfigFromBackend();

    // 3. Setup More button click listener
    const moreBtn = document.getElementById('nav-more-btn');
    if (moreBtn) {
        moreBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleMoreDropdown();
        });
    }

    // 4. Close More dropdown on click/touch outside
    const handleOutsideInteraction = (e) => {
        const moreWrapper = document.getElementById('nav-more-wrapper');
        if (moreWrapper && !moreWrapper.contains(e.target)) {
            closeMoreDropdown();
        }
    };
    document.addEventListener('click', handleOutsideInteraction);
    document.addEventListener('pointerdown', handleOutsideInteraction);

    // 4. Setup Custom Nav Modal Triggers
    const btnOpenCustom = document.getElementById('btn-open-nav-custom');
    if (btnOpenCustom) {
        btnOpenCustom.addEventListener('click', () => openNavCustomModal());
    }

    const btnSaveCustom = document.getElementById('nav-custom-save');
    if (btnSaveCustom) {
        btnSaveCustom.addEventListener('click', () => saveNavCustomModal());
    }

    const btnResetCustom = document.getElementById('nav-custom-reset');
    if (btnResetCustom) {
        btnResetCustom.addEventListener('click', () => resetNavCustomModalToDefault());
    }

    // Expose helpers globally if needed
    window.openNavCustomModal = openNavCustomModal;
    window.switchToTab = switchToTab;
}
