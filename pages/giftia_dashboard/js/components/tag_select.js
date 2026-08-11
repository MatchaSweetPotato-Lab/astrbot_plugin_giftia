/**
 * TagSelectComponent - Modern Tag Select Component with Floating Dropdown Popover.
 */
class TagSelectComponent {
    constructor(containerId, options = {}) {
        this.containerId = containerId;
        this.container = document.getElementById(containerId);
        this.placeholder = options.placeholder || '点击或检索选择...';
        this.availableOptions = options.availableOptions || []; // [{id, name, ...}, ...]
        this.selectedValues = options.selectedValues ? [...options.selectedValues] : []; // ['id1', 'id2', ...]
        this.onChange = options.onChange || null;

        this.searchTerm = '';
        this.isOpen = false;
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
        const knownMap = new Map(this.availableOptions.map(o => [o.id, o.name]));

        let html = `
            <div class="tag-select-wrapper">
                <div class="tag-select-box" tabindex="0">
                    <div class="tag-chips-list" style="display: flex; flex-wrap: wrap; gap: 6px; align-items: center; width: 100%;">
                        ${this.selectedValues.map((val, idx) => {
                            const label = knownMap.get(val) || val;
                            return `
                                <span class="tag-chip">
                                    <span>${escapeHtml(label)}</span>
                                    <button type="button" class="tag-chip-remove" data-index="${idx}" title="移除">&times;</button>
                                </span>
                            `;
                        }).join('')}
                        <input type="text" class="tag-select-input" value="${escapeHtml(this.searchTerm)}" placeholder="${this.selectedValues.length === 0 ? escapeHtml(this.placeholder) : '检索或追加...'}">
                    </div>
                </div>

                <div class="tag-select-dropdown" style="display: ${this.isOpen ? 'block' : 'none'};">
                    ${this.renderOptionsList()}
                </div>
            </div>
        `;

        this.container.innerHTML = html;
    }

    renderOptionsList() {
        const escapeHtml = (str) => window.escapeHtml ? window.escapeHtml(str) : String(str || '');
        const filtered = this.availableOptions.filter(opt => {
            if (!this.searchTerm) return true;
            const term = this.searchTerm.toLowerCase();
            return (opt.name || '').toLowerCase().includes(term) || (opt.id || '').toLowerCase().includes(term);
        });

        if (filtered.length === 0) {
            if (this.searchTerm) {
                return `
                    <div class="tag-select-option custom-add-option">
                        <span>按回车添加自定义标签: "<strong>${escapeHtml(this.searchTerm)}</strong>"</span>
                        <span style="font-size: 0.75rem; background: var(--info-bg, rgba(59,130,246,0.15)); color: var(--info, #3b82f6); padding: 2px 6px; border-radius: 4px;">Enter ↵</span>
                    </div>
                `;
            }
            return `<div style="padding: 12px; text-align: center; color: var(--font-secondary); font-size: 0.82rem;">无可匹配的预设选项</div>`;
        }

        return filtered.map(opt => {
            const isSelected = this.selectedValues.includes(opt.id);
            return `
                <div class="tag-select-option ${isSelected ? 'selected' : ''}" data-id="${escapeHtml(opt.id)}">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span style="font-weight: 500;">${escapeHtml(opt.name)}</span>
                        ${opt.name !== opt.id ? `<span class="muted-text" style="font-size: 0.76rem; color: var(--font-secondary);">(${escapeHtml(opt.id)})</span>` : ''}
                    </div>
                    ${isSelected ? `<span style="font-weight: bold; color: var(--info, #3b82f6);">✓</span>` : ''}
                </div>
            `;
        }).join('');
    }

    bindEvents() {
        const box = this.container.querySelector('.tag-select-box');
        const input = this.container.querySelector('.tag-select-input');
        const dropdown = this.container.querySelector('.tag-select-dropdown');

        if (!box || !input) return;

        // Focus & Click to toggle open
        box.addEventListener('click', (e) => {
            if (e.target.classList.contains('tag-chip-remove')) {
                const idx = parseInt(e.target.dataset.index);
                this.removeValueAt(idx);
                return;
            }
            input.focus();
            this.setOpen(true);
        });

        // Live Search Input Event
        input.addEventListener('input', (e) => {
            this.searchTerm = e.target.value;
            this.setOpen(true);
            if (dropdown) dropdown.innerHTML = this.renderOptionsList();
        });

        // Keydown Handling (Enter to add custom value, Backspace to delete last chip)
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                const term = this.searchTerm.trim();
                if (term) {
                    this.addValue(term);
                    this.searchTerm = '';
                    this.render();
                    this.rebindAndFocus();
                }
            } else if (e.key === 'Backspace' && !this.searchTerm && this.selectedValues.length > 0) {
                this.removeValueAt(this.selectedValues.length - 1);
            } else if (e.key === 'Escape') {
                this.setOpen(false);
            }
        });

        // Dropdown Item Selection
        dropdown.addEventListener('click', (e) => {
            const optEl = e.target.closest('.tag-select-option');
            if (optEl) {
                if (optEl.classList.contains('custom-add-option')) {
                    const term = this.searchTerm.trim();
                    if (term) {
                        this.addValue(term);
                        this.searchTerm = '';
                    }
                } else {
                    const val = optEl.dataset.id;
                    this.toggleValue(val);
                }
                this.render();
                this.rebindAndFocus();
            }
        });

        // Outside Click Listener
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

    destroy() {
        if (this.documentClickHandler) {
            document.removeEventListener('click', this.documentClickHandler);
            this.documentClickHandler = null;
        }
    }


    rebindAndFocus() {
        this.bindEvents();
        const input = this.container.querySelector('.tag-select-input');
        if (input) {
            input.focus();
            this.setOpen(true);
        }
    }

    setOpen(open) {
        this.isOpen = open;
        const dropdown = this.container.querySelector('.tag-select-dropdown');
        if (dropdown) {
            dropdown.style.display = open ? 'block' : 'none';
        }
    }

    toggleValue(val) {
        const idx = this.selectedValues.indexOf(val);
        if (idx >= 0) {
            this.selectedValues.splice(idx, 1);
        } else {
            this.selectedValues.push(val);
        }
        if (this.onChange) this.onChange(this.selectedValues);
    }

    addValue(val) {
        if (!this.selectedValues.includes(val)) {
            this.selectedValues.push(val);
            if (this.onChange) this.onChange(this.selectedValues);
        }
    }

    removeValueAt(idx) {
        if (idx >= 0 && idx < this.selectedValues.length) {
            this.selectedValues.splice(idx, 1);
            this.render();
            this.rebindAndFocus();
            if (this.onChange) this.onChange(this.selectedValues);
        }
    }

    getValues() {
        return [...this.selectedValues];
    }

    setAvailableOptions(options) {
        this.availableOptions = options || [];
        this.render();
        this.bindEvents();
    }
}

if (typeof window !== 'undefined') {
    window.TagSelectComponent = TagSelectComponent;
}
