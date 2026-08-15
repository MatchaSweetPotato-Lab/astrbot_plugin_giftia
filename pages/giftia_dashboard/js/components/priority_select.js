/**
 * PrioritySelectComponent - Ordered Model Provider Priority Selector with Drag & Drop,
 * Move Up/Down, Delete, Priority Badges, and Search Popover.
 *
 * Performance Optimized:
 * - Single-pass event delegation for list actions, drag & drop, and dropdown options.
 * - Minimal DOM updates: keeps search input and wrapper intact without re-binding.
 * - Dynamic global document listener: attached only when dropdown is open, cleaned up on close.
 * - Smart ID matching: resolves input term to existing provider ID before falling back to custom.
 */
class PrioritySelectComponent {
    constructor(containerId, options = {}) {
        this.containerId = containerId;
        this.container = document.getElementById(containerId);
        this.placeholder = options.placeholder || '检索或输入模型提供商 (按回车添加)...';
        this.availableOptions = options.availableOptions || []; // [{id, name, type}, ...]
        this.selectedValues = options.selectedValues ? [...options.selectedValues] : []; // ['id1', 'id2', ...]
        this.onChange = options.onChange || null;

        this.searchTerm = '';
        this.isOpen = false;
        this.draggedIndex = null;

        // Bound event handler for clean removal
        this.outsideClickHandler = (e) => {
            if (this.container && !this.container.contains(e.target)) {
                this.setOpen(false);
            }
        };

        this.init();
    }

    init() {
        if (!this.container) return;
        this.renderStructure();
        this.bindEvents();
        this.updateList();
        this.updateDropdown();
    }

    /**
     * Render the static outer layout structure once.
     */
    renderStructure() {
        const escapeHtml = (str) => window.escapeHtml ? window.escapeHtml(str) : String(str || '');

        this.container.innerHTML = `
            <div class="priority-select-wrapper">
                <div class="priority-header-info">
                    <span class="priority-header-text">已选择 <strong class="priority-count-badge">0</strong> 个提供商 <span style="opacity: 0.8;">(由上至下顺序降级)</span></span>
                </div>

                <div class="priority-selected-list"></div>

                <div class="priority-search-container">
                    <div class="priority-search-input-box">
                        <svg class="priority-search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                            <circle cx="11" cy="11" r="8"></circle>
                            <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                        </svg>
                        <input type="text" class="priority-search-input" placeholder="${escapeHtml(this.placeholder)}">
                    </div>

                    <div class="priority-dropdown" style="display: none;"></div>
                </div>
            </div>
        `;

        this.headerTextEl = this.container.querySelector('.priority-header-text');
        this.listEl = this.container.querySelector('.priority-selected-list');
        this.inputEl = this.container.querySelector('.priority-search-input');
        this.dropdownEl = this.container.querySelector('.priority-dropdown');
    }

    /**
     * Update the ordered priority list items (minimal update path).
     */
    updateList() {
        if (!this.listEl) return;
        const escapeHtml = (str) => window.escapeHtml ? window.escapeHtml(str) : String(str || '');
        const knownMap = new Map(this.availableOptions.map(o => [o.id, o]));

        // Update header count
        if (this.headerTextEl) {
            this.headerTextEl.innerHTML = `已选择 <strong class="priority-count-badge">${this.selectedValues.length}</strong> 个提供商 <span style="opacity: 0.8;">(由上至下顺序降级)</span>`;
        }

        if (this.selectedValues.length === 0) {
            this.listEl.innerHTML = `<div class="priority-empty-state">暂未配置模型提供商，请在下方检索或输入添加</div>`;
            return;
        }

        this.listEl.innerHTML = this.selectedValues.map((val, idx) => {
            const item = knownMap.get(val);
            const displayName = item ? (item.name || item.id) : val;
            const isPrimary = idx === 0;
            const badgeLabel = isPrimary ? '#1 首选' : `#${idx + 1} 备用`;
            const badgeClass = isPrimary ? 'primary-rank' : 'fallback-rank';
            const hasSeparateId = item && item.name && item.id && item.name !== item.id;
            const typeName = item ? (item.type || '') : '';

            return `
                <div class="priority-item-card" draggable="true" data-index="${idx}" data-id="${escapeHtml(val)}">
                    <span class="priority-drag-handle" title="按住拖拽排序">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                            <circle cx="9" cy="5" r="1"></circle>
                            <circle cx="9" cy="12" r="1"></circle>
                            <circle cx="9" cy="19" r="1"></circle>
                            <circle cx="15" cy="5" r="1"></circle>
                            <circle cx="15" cy="12" r="1"></circle>
                            <circle cx="15" cy="19" r="1"></circle>
                        </svg>
                    </span>

                    <span class="priority-badge ${badgeClass}">${escapeHtml(badgeLabel)}</span>

                    <div class="priority-item-info">
                        <div class="priority-item-title-row">
                            <span class="priority-item-name" title="${escapeHtml(displayName)}">${escapeHtml(displayName)}</span>
                            ${typeName ? `<span class="priority-type-pill">${escapeHtml(typeName)}</span>` : ''}
                        </div>
                        ${hasSeparateId ? `<div class="priority-item-id" title="${escapeHtml(item.id)}">${escapeHtml(item.id)}</div>` : ''}
                    </div>

                    <div class="priority-item-actions">
                        <button type="button" class="priority-action-btn priority-up-btn" data-action="up" data-index="${idx}" ${idx === 0 ? 'disabled' : ''} title="提升优先级 (上移)">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="18 15 12 9 6 15"></polyline>
                            </svg>
                        </button>
                        <button type="button" class="priority-action-btn priority-down-btn" data-action="down" data-index="${idx}" ${idx === this.selectedValues.length - 1 ? 'disabled' : ''} title="降低优先级 (下移)">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="6 9 12 15 18 9"></polyline>
                            </svg>
                        </button>
                        <button type="button" class="priority-action-btn priority-remove-btn" data-action="remove" data-index="${idx}" title="从优先级列表中移除">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                <line x1="18" y1="6" x2="6" y2="18"></line>
                                <line x1="6" y1="6" x2="18" y2="18"></line>
                            </svg>
                        </button>
                    </div>
                </div>
            `;
        }).join('');
    }

    /**
     * Update the search dropdown items list.
     */
    updateDropdown() {
        if (!this.dropdownEl) return;
        const escapeHtml = (str) => window.escapeHtml ? window.escapeHtml(str) : String(str || '');
        const term = (this.searchTerm || '').trim().toLowerCase();

        const filtered = this.availableOptions.filter(opt => {
            if (!term) return true;
            return (opt.name || '').toLowerCase().includes(term) ||
                   (opt.id || '').toLowerCase().includes(term) ||
                   (opt.type || '').toLowerCase().includes(term);
        });

        if (filtered.length === 0) {
            if (term) {
                this.dropdownEl.innerHTML = `
                    <div class="priority-dropdown-option custom-add" data-action="add-custom" data-id="${escapeHtml(this.searchTerm.trim())}">
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <span style="font-weight: 600;">+ 按回车添加自定义提供商: "${escapeHtml(this.searchTerm)}"</span>
                        </div>
                        <span style="font-size: 0.75rem; background: var(--info-bg, rgba(59,130,246,0.15)); color: var(--info, #3b82f6); padding: 2px 6px; border-radius: 4px;">Enter ↵</span>
                    </div>
                `;
            } else {
                this.dropdownEl.innerHTML = `<div style="padding: 12px; text-align: center; color: var(--font-secondary); font-size: 0.82rem;">无可匹配的预设提供商</div>`;
            }
            return;
        }

        let optionsHtml = filtered.map(opt => {
            const isSelected = this.selectedValues.includes(opt.id);
            const rankIndex = this.selectedValues.indexOf(opt.id);
            const rankBadge = isSelected ? (rankIndex === 0 ? '首选 (#1)' : `备用 (#${rankIndex + 1})`) : '';

            return `
                <div class="priority-dropdown-option ${isSelected ? 'selected' : ''}" data-id="${escapeHtml(opt.id)}">
                    <div style="display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1;">
                        <span style="font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(opt.name || opt.id)}</span>
                        ${opt.name && opt.id && opt.name !== opt.id ? `<span class="priority-item-id" style="max-width: 160px;">(${escapeHtml(opt.id)})</span>` : ''}
                        ${opt.type ? `<span class="priority-type-pill">${escapeHtml(opt.type)}</span>` : ''}
                    </div>
                    ${isSelected ? `<span style="font-size: 0.76rem; font-weight: 600; color: var(--info, #3b82f6); white-space: nowrap;">✓ 已选 ${rankBadge}</span>` : `<span style="font-size: 0.76rem; color: var(--font-secondary); white-space: nowrap;">+ 添加</span>`}
                </div>
            `;
        }).join('');

        if (term && !filtered.some(opt => (opt.id || '').toLowerCase() === term)) {
            optionsHtml += `
                <div class="priority-dropdown-option custom-add" data-action="add-custom" data-id="${escapeHtml(this.searchTerm.trim())}" style="border-top: 1px solid var(--border-color);">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span>+ 添加自定义提供商: "<strong>${escapeHtml(this.searchTerm.trim())}</strong>"</span>
                    </div>
                    <span style="font-size: 0.75rem; background: var(--info-bg, rgba(59,130,246,0.15)); color: var(--info, #3b82f6); padding: 2px 6px; border-radius: 4px;">Enter ↵</span>
                </div>
            `;
        }

        this.dropdownEl.innerHTML = optionsHtml;
    }

    /**
     * Resolve an input term to an existing option ID or return the clean custom term.
     */
    resolveProviderId(term) {
        if (typeof window.resolveOptionId === 'function') {
            return window.resolveOptionId(term, this.availableOptions);
        }
        return (term || '').trim();
    }

    /**
     * Bind all delegated event listeners once.
     */
    bindEvents() {
        if (!this.container) return;

        // 1. Search Input Listeners
        if (this.inputEl) {
            this.inputEl.addEventListener('focus', () => {
                this.setOpen(true);
            });

            this.inputEl.addEventListener('input', (e) => {
                this.searchTerm = e.target.value;
                this.setOpen(true);
                this.updateDropdown();
            });

            this.inputEl.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    const term = this.searchTerm.trim();
                    if (term) {
                        const targetId = this.resolveProviderId(term);
                        this.addValue(targetId);
                        this.searchTerm = '';
                        this.inputEl.value = '';
                        this.updateDropdown();
                    }
                } else if (e.key === 'Escape') {
                    this.setOpen(false);
                }
            });
        }

        // 2. Delegated Dropdown Option Click Listener
        if (this.dropdownEl) {
            this.dropdownEl.addEventListener('click', (e) => {
                const optEl = e.target.closest('.priority-dropdown-option');
                if (optEl) {
                    const rawVal = optEl.dataset.id;
                    if (rawVal) {
                        const targetId = this.resolveProviderId(rawVal);
                        if (!optEl.classList.contains('selected')) {
                            this.addValue(targetId);
                            this.searchTerm = '';
                            if (this.inputEl) this.inputEl.value = '';
                            this.updateDropdown();
                            if (this.inputEl) this.inputEl.focus();
                        }
                    }
                }
            });
        }

        // 3. Delegated List Action Click Listener (Up / Down / Remove)
        if (this.listEl) {
            this.listEl.addEventListener('click', (e) => {
                const btn = e.target.closest('.priority-action-btn');
                if (!btn || btn.disabled) return;

                const action = btn.dataset.action;
                const index = parseInt(btn.dataset.index, 10);
                if (isNaN(index)) return;

                if (action === 'up') {
                    this.moveUp(index);
                } else if (action === 'down') {
                    this.moveDown(index);
                } else if (action === 'remove') {
                    this.removeAt(index);
                }
            });

            // 4. Delegated Drag & Drop Listeners
            this.listEl.addEventListener('dragstart', (e) => {
                const card = e.target.closest('.priority-item-card');
                if (!card) return;

                this.draggedIndex = parseInt(card.dataset.index, 10);
                card.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', this.draggedIndex);
            });

            this.listEl.addEventListener('dragend', () => {
                const cards = this.listEl.querySelectorAll('.priority-item-card');
                cards.forEach(c => {
                    c.classList.remove('dragging');
                    c.classList.remove('drag-over-top');
                    c.classList.remove('drag-over-bottom');
                });
                this.draggedIndex = null;
            });

            this.listEl.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                if (this.draggedIndex === null) return;

                const card = e.target.closest('.priority-item-card');
                if (!card) return;

                const targetIndex = parseInt(card.dataset.index, 10);
                const cards = this.listEl.querySelectorAll('.priority-item-card');
                cards.forEach(c => c.classList.remove('drag-over-top', 'drag-over-bottom'));

                if (targetIndex === this.draggedIndex) return;

                const rect = card.getBoundingClientRect();
                const midY = rect.top + rect.height / 2;
                if (e.clientY < midY) {
                    card.classList.add('drag-over-top');
                } else {
                    card.classList.add('drag-over-bottom');
                }
            });

            this.listEl.addEventListener('dragleave', (e) => {
                const card = e.target.closest('.priority-item-card');
                if (card && !card.contains(e.relatedTarget)) {
                    card.classList.remove('drag-over-top', 'drag-over-bottom');
                }
            });

            this.listEl.addEventListener('drop', (e) => {
                e.preventDefault();
                if (this.draggedIndex === null) return;

                const card = e.target.closest('.priority-item-card');
                if (!card) return;

                const targetIndex = parseInt(card.dataset.index, 10);
                const rect = card.getBoundingClientRect();
                const midY = rect.top + rect.height / 2;
                const placeAfter = e.clientY >= midY;

                let newIndex = targetIndex;
                if (placeAfter && this.draggedIndex > targetIndex) {
                    newIndex = targetIndex + 1;
                } else if (!placeAfter && this.draggedIndex < targetIndex) {
                    newIndex = targetIndex - 1;
                }

                if (newIndex !== this.draggedIndex && newIndex >= 0 && newIndex < this.selectedValues.length) {
                    this.reorderItem(this.draggedIndex, newIndex);
                } else if (newIndex !== this.draggedIndex) {
                    this.reorderItem(this.draggedIndex, targetIndex);
                }

                const cards = this.listEl.querySelectorAll('.priority-item-card');
                cards.forEach(c => c.classList.remove('drag-over-top', 'drag-over-bottom'));
            });
        }
    }

    /**
     * Toggle dropdown popover state and manage dynamic global document listener.
     */
    setOpen(open) {
        if (this.isOpen === open) return;
        this.isOpen = open;

        if (this.dropdownEl) {
            this.dropdownEl.style.display = open ? 'block' : 'none';
        }

        // Attach global document listener ONLY while open, remove immediately when closed
        if (open) {
            document.addEventListener('click', this.outsideClickHandler);
        } else {
            document.removeEventListener('click', this.outsideClickHandler);
        }
    }

    addValue(val) {
        if (!val) return;
        if (!this.selectedValues.includes(val)) {
            this.selectedValues.push(val);
            this.updateList();
            this.updateDropdown();
            if (this.onChange) this.onChange(this.selectedValues);
        }
    }

    removeAt(idx) {
        if (idx >= 0 && idx < this.selectedValues.length) {
            this.selectedValues.splice(idx, 1);
            this.updateList();
            this.updateDropdown();
            if (this.onChange) this.onChange(this.selectedValues);
        }
    }

    moveUp(idx) {
        if (idx > 0 && idx < this.selectedValues.length) {
            const temp = this.selectedValues[idx];
            this.selectedValues[idx] = this.selectedValues[idx - 1];
            this.selectedValues[idx - 1] = temp;
            this.updateList();
            this.updateDropdown();
            if (this.onChange) this.onChange(this.selectedValues);
        }
    }

    moveDown(idx) {
        if (idx >= 0 && idx < this.selectedValues.length - 1) {
            const temp = this.selectedValues[idx];
            this.selectedValues[idx] = this.selectedValues[idx + 1];
            this.selectedValues[idx + 1] = temp;
            this.updateList();
            this.updateDropdown();
            if (this.onChange) this.onChange(this.selectedValues);
        }
    }

    reorderItem(fromIndex, toIndex) {
        if (fromIndex === toIndex || fromIndex < 0 || toIndex < 0 ||
            fromIndex >= this.selectedValues.length || toIndex >= this.selectedValues.length) {
            return;
        }
        const item = this.selectedValues.splice(fromIndex, 1)[0];
        this.selectedValues.splice(toIndex, 0, item);
        this.updateList();
        this.updateDropdown();
        if (this.onChange) this.onChange(this.selectedValues);
    }

    getValues() {
        return [...this.selectedValues];
    }

    setAvailableOptions(options) {
        this.availableOptions = options || [];
        this.updateList();
        this.updateDropdown();
    }

    destroy() {
        this.setOpen(false);
        if (this.outsideClickHandler) {
            document.removeEventListener('click', this.outsideClickHandler);
        }
        if (this.container) {
            this.container.innerHTML = '';
        }
    }
}

if (typeof window !== 'undefined') {
    window.PrioritySelectComponent = PrioritySelectComponent;
}
