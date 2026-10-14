export const MEMORY_KINDS = {
    event: '事件',
    agreement: '约定',
    relationship: '关系',
    preference: '偏好 / 边界',
    habit: '习惯'
};

export const MEMORY_STATUSES = {
    active: '有效',
    completed: '已完成',
    cancelled: '已取消',
    expired: '已过期',
    superseded: '已被替代'
};

export function memoryOriginLabel(item) {
    const meta = item.metadata || {};
    if (meta.manual_entry) return '人工录入';
    if (meta.origin === 'derived') return '整理归纳';
    if (meta.origin === 'observed') return item.kind === 'habit' ? '本人自述' : '原话记录';
    return '旧版记录';
}
