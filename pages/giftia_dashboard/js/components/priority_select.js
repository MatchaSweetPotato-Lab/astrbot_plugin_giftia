/**
 * PrioritySelectComponent - Ordered Model Provider Priority Selector with Drag & Drop,
 * Move Up/Down, Delete, Priority Badges, and Search Popover.
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
        this.documentClickHandler = null;

        this.init();
    }

    init() {
        if (!this.container) return;
        this.render();
        this.bindEvents();
    }

    render() {
        const escapeHtml = (str) => window.escapeHtml ? window.escapeHtml(str) : String(str || '');
        const knownMap = new Map(this.availableOptions.map(o => [o.id, o]));

        const listHtml = this.selectedValues.length === 0
            ? `<div class="priority-empty-state">暂未配置模型提供商，请在下方检索或输入添加</div>`
            : this.selectedValues.map((val, idx) => {
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

        const html = `
            <div class="priority-select-wrapper">
                <div class="priority-header-info">
                    <span>已选择 <strong class="priority-count-badge">${this.selectedValues.length}</strong> 个提供商 <span style="opacity: 0.8;">(由上至下顺序降级)</span></span>
                </div>

                <div class="priority-selected-list">
                    ${listHtml}
                </div>

                <div class="priority-search-container">
                    <div class="priority-search-input-box">
                        <svg class="priority-search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                            <circle cx="11" cy="11" r="8"></circle>
                            <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                        </svg>
                        <input type="text" class="priority-search-input" value="${escapeHtml(this.searchTerm)}" placeholder="${escapeHtml(this.placeholder)}">
                    </div>

                    <div class="priority-dropdown" style="display: ${this.isOpen ? 'block' : 'none'};">
                        ${this.renderDropdownOptions()}
                    </div>
                </div>
            </div>
        `;

        this.container.innerHTML = html;
    }

    renderDropdownOptions() {
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
                return `
                    <div class="priority-dropdown-option custom-add" data-action="add-custom" data-id="${escapeHtml(term)}">
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <span style="font-weight: 600;">+ 按回车添加自定义提供商: "${escapeHtml(this.searchTerm)}"</span>
                        </div>
                        <span style="font-size: 0.75rem; background: var(--info-bg, rgba(59,130,246,0.15)); color: var(--info, #3b82f6); padding: 2px 6px; border-radius: 4px;">Enter ↵</span>
                    </div>
                `;
            }
            return `<div style="padding: 12px; text-align: center; color: var(--font-secondary); font-size: 0.82rem;">无可匹配的预设提供商</div>`;
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

        if (term && !filtered.some(opt => opt.id.toLowerCase() === term)) {
            optionsHtml += `
                <div class="priority-dropdown-option custom-add" data-action="add-custom" data-id="${escapeHtml(this.searchTerm.trim())}" style="border-top: 1px solid var(--border-color);">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span>+ 添加自定义提供商: "<strong>${escapeHtml(this.searchTerm.trim())}</strong>"</span>
                    </div>
                    <span style="font-size: 0.75rem; background: var(--info-bg, rgba(59,130,246,0.15)); color: var(--info, #3b82f6); padding: 2px 6px; border-radius: 4px;">Enter ↵</span>
                </div>
            `;
        }

        return optionsHtml;
    }

    bindEvents() {
        if (!this.container) return;

        const input = this.container.querySelector('.priority-search-input');
        const dropdown = this.container.querySelector('.priority-dropdown');
        const list = this.container.querySelector('.priority-selected-list');

        // Input Focus & Typing
        if (input) {
            input.addEventListener('focus', () => {
                this.setOpen(true);
            });

            input.addEventListener('input', (e) => {
                this.searchTerm = e.target.value;
                this.setOpen(true);
                if (dropdown) dropdown.innerHTML = this.renderDropdownOptions();
            });

            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    const term = this.searchTerm.trim();
                    if (term) {
                        this.addValue(term);
                        this.searchTerm = '';
                        this.render();
                        this.bindEvents();
                        this.focusInput();
                    }
                } else if (e.key === 'Escape') {
                    this.setOpen(false);
                }
            });
        }

        // Dropdown Item Click
        if (dropdown) {
            dropdown.addEventListener('click', (e) => {
                const optEl = e.target.closest('.priority-dropdown-option');
                if (optEl) {
                    const val = optEl.dataset.id;
                    if (val) {
                        if (optEl.classList.contains('selected')) {
                            // If already selected, do nothing or show toast
                        } else {
                            this.addValue(val);
                            this.searchTerm = '';
                            this.render();
                            this.bindEvents();
                            this.focusInput();
                        }
                    }
                }
            });
        }

        // Actions in Priority List (Up, Down, Remove)
        if (list) {
            list.addEventListener('click', (e) => {
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

            // HTML5 Drag and Drop for items
            const cards = list.querySelectorAll('.priority-item-card');
            cards.forEach(card => {
                card.addEventListener('dragstart', (e) => {
                    this.draggedIndex = parseInt(card.dataset.index, 10);
                    card.classList.add('dragging');
                    e.dataTransfer.effectAllowed = 'move';
                    e.dataTransfer.setData('text/plain', this.draggedIndex);
                });

                card.addEventListener('dragend', () => {
                    card.classList.remove('dragging');
                    cards.forEach(c => c.classList.remove('drag-over-top', 'drag-over-bottom'));
                    this.draggedIndex = null;
                });

                card.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    e.dataTransfer.dropEffect = 'move';
                    if (this.draggedIndex === null) return;

                    const targetIndex = parseInt(card.dataset.index, 10);
                    if (targetIndex === this.draggedIndex) return;

                    const rect = card.getBoundingClientRect();
                    const midY = rect.top + rect.height / 2;
                    cards.forEach(c => c.classList.remove('drag-over-top', 'drag-over-bottom'));

                    if (e.clientY < midY) {
                        card.classList.add('drag-over-top');
                    } else {
                        card.classList.add('drag-over-bottom');
                    }
                });

                card.addEventListener('dragleave', () => {
                    card.classList.remove('drag-over-top', 'drag-over-bottom');
                });

                card.addEventListener('drop', (e) => {
                    e.preventDefault();
                    if (this.draggedIndex === null) return;

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

                    cards.forEach(c => c.classList.remove('drag-over-top', 'drag-over-bottom'));
                });
            });
        }

        // Global click listener to close dropdown
        if (this.documentClickHandler) {
            document.removeEventListener('click', this.documentClickHandler);
        }
        this.documentClickHandler = (e) => {
            if (this.container && !this.container.contains(e.target)) {
                this.setOpen(false);
            }
        };
        document.addEventListener('click', this.documentClickHandler);
    }

    focusInput() {
        const input = this.container.querySelector('.priority-search-input');
        if (input) {
            input.focus();
        }
    }

    setOpen(open) {
        this.isOpen = open;
        const dropdown = this.container.querySelector('.priority-dropdown');
        if (dropdown) {
            dropdown.style.display = open ? 'block' : 'none';
        }
    }

    addValue(val) {
        if (!val) return;
        if (!this.selectedValues.includes(val)) {
            this.selectedValues.push(val);
            if (this.onChange) this.onChange(this.selectedValues);
        }
    }

    removeAt(idx) {
        if (idx >= 0 && idx < this.selectedValues.length) {
            this.selectedValues.splice(idx, 1);
            this.render();
            this.bindEvents();
            if (this.onChange) this.onChange(this.selectedValues);
        }
    }

    moveUp(idx) {
        if (idx > 0 && idx < this.selectedValues.length) {
            const temp = this.selectedValues[idx];
            this.selectedValues[idx] = this.selectedValues[idx - 1];
            this.selectedValues[idx - 1] = temp;
            this.render();
            this.bindEvents();
            if (this.onChange) this.onChange(this.selectedValues);
        }
    }

    moveDown(idx) {
        if (idx >= 0 && idx < this.selectedValues.length - 1) {
            const temp = this.selectedValues[idx];
            this.selectedValues[idx] = this.selectedValues[idx + 1];
            this.selectedValues[idx + 1] = temp;
            this.render();
            this.bindEvents();
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
        this.render();
        this.bindEvents();
        if (this.onChange) this.onChange(this.selectedValues);
    }

    getValues() {
        return [...this.selectedValues];
    }

    setAvailableOptions(options) {
        this.availableOptions = options || [];
        this.render();
        this.bindEvents();
    }

    destroy() {
        if (this.documentClickHandler) {
            document.removeEventListener('click', this.documentClickHandler);
            this.documentClickHandler = null;
        }
    }
}

if (typeof window !== 'undefined') {
    window.PrioritySelectComponent = PrioritySelectComponent;
}
