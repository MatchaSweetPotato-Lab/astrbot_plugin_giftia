export function renderRelationChanges(items, { editable = false } = {}) {
    if (!items?.length) {
        return '<p class="relation-history-empty">暂无互动评价记录</p>';
    }
    return `<ol class="relation-change-list">${items.map((item, index) => {
        const delta = Number(item.delta) || 0;
        const direction = delta > 0 ? 'positive' : delta < 0 ? 'negative' : 'neutral';
        const timestamp = String(item.occurred_at || item.created_at || '');
        return `
            <li class="relation-change-item"${editable ? ` data-relation-index="${index}"` : ''}>
                <div class="relation-change-heading">
                    <span class="relation-change-delta ${direction}">互动评价 ${delta > 0 ? '+' : ''}${delta}</span>
                    <time class="relation-change-time" datetime="${window.escapeHtml(timestamp)}">${window.escapeHtml(String(window.formatDate(timestamp)))}</time>
                </div>
                <p class="relation-change-reason">${window.escapeHtml(String(item.reason || '未记录变化理由'))}</p>
                ${editable ? `<div class="relation-change-actions">
                    <button type="button" class="btn btn-secondary btn-small" variant="text" data-relation-action="edit">编辑</button>
                    <button type="button" class="btn btn-danger btn-small" variant="text" data-relation-action="delete">删除</button>
                </div>` : ''}
            </li>
        `;
    }).join('')}</ol>`;
}
