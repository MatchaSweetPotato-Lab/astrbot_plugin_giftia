export function renderRelationChanges(items) {
    if (!items?.length) {
        return '<p class="relation-history-empty">暂无互动评价记录</p>';
    }
    return `<ol class="relation-change-list">${items.map(item => {
        const delta = Number(item.delta) || 0;
        const direction = delta > 0 ? 'positive' : delta < 0 ? 'negative' : 'neutral';
        const timestamp = String(item.occurred_at || item.created_at || '');
        return `
            <li class="relation-change-item">
                <div class="relation-change-heading">
                    <span class="relation-change-delta ${direction}">互动评价 ${delta > 0 ? '+' : ''}${delta}</span>
                    <time class="relation-change-time" datetime="${window.escapeHtml(timestamp)}">${window.escapeHtml(String(window.formatDate(timestamp)))}</time>
                </div>
                <p class="relation-change-reason">${window.escapeHtml(String(item.reason || '未记录变化理由'))}</p>
            </li>
        `;
    }).join('')}</ol>`;
}
