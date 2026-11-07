import { state } from './state.js';

const statusLabels = {
    unknown: '未检测', healthy: '健康', degraded: '待观察', cooldown: '冷却中',
    probe_due: '待恢复试探', probing: '试探中', missing: '未注册'
};
const reasonLabels = {
    connection: '连接失败或请求超时', server_error: '供应商服务异常',
    rate_limit: '请求限流', quota: '余额或配额不足',
    authentication: '鉴权或权限异常', provider_missing: '供应商或模型不存在'
};
let models = [];
let loadVersion = 0;
let refreshTimer = null;
const testing = new Set();
const escape = value => window.escapeHtml(String(value ?? ''));
const formatTime = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12: false}) : '—';

function renderModels() {
    const container = document.getElementById('model-health-list');
    if (!container) return;
    document.getElementById('model-health-count').textContent = `已配置 ${models.length} 个供应商`;
    if (!models.length) {
        container.innerHTML = '<tr><td colspan="6" class="empty-state">尚未配置对话模型。请在机器人管理或插件设置中选择供应商。</td></tr>';
        return;
    }
    container.innerHTML = models.map(model => {
        const busy = testing.has(model.provider_id) || model.status === 'probing';
        const status = !model.registered ? 'missing' : (busy ? 'probing' : model.status);
        const color = status === 'healthy' ? 'success' : (['cooldown', 'missing'].includes(status) ? 'danger' : 'secondary');
        const detail = status === 'cooldown' ? `约 ${model.retry_in_seconds} 秒后可试探`
            : status === 'probe_due' ? '下次请求将尝试恢复'
            : status === 'missing' ? '供应商配置尚未加载或已删除'
            : (reasonLabels[model.reason] || '');
        return `<tr>
            <td><strong>${escape(model.name)}</strong><div class="muted-text model-health-id">${escape(model.provider_id)}</div><div class="muted-text">${escape(model.model)}</div></td>
            <td><div class="model-health-uses">${model.uses.map(use => `<span class="badge badge-secondary">${escape(use)}</span>`).join('')}</div></td>
            <td><span class="badge badge-${color}">${escape(statusLabels[status] || status)}</span><div class="muted-text model-health-detail">${escape(detail)}</div>${model.reason && detail !== reasonLabels[model.reason] ? `<div class="muted-text">${escape(reasonLabels[model.reason] || '')}</div>` : ''}${model.failures ? `<div class="muted-text">连续失败 ${model.failures} 次</div>` : ''}</td>
            <td>${escape(formatTime(model.last_attempt))}<div class="muted-text">${model.last_latency_ms == null ? '—' : `${escape(model.last_latency_ms)} ms`}</div></td>
            <td>${escape(formatTime(model.last_success))}</td>
            <td><button class="btn btn-small btn-secondary" data-test-model="${escape(model.provider_id)}" ${busy ? 'disabled' : ''}>${busy ? '测试中…' : '连通性测试'}</button></td>
        </tr>`;
    }).join('');
}

export async function loadModelHealth() {
    clearTimeout(refreshTimer);
    const version = ++loadVersion;
    const error = document.getElementById('model-health-error');
    const refresh = document.getElementById('model-health-refresh');
    if (refresh) refresh.disabled = true;
    try {
        const result = await window.apiGet('/models/health');
        if (version !== loadVersion) return;
        if (result?.status !== 'success' || !Array.isArray(result.data)) {
            throw new Error(result?.message || '模型状态加载失败');
        }
        models = result.data;
        if (error) error.textContent = '';
        renderModels();
    } catch (err) {
        if (version === loadVersion && error) error.textContent = err.message || '模型状态加载失败';
    } finally {
        if (version === loadVersion) {
            if (refresh) refresh.disabled = false;
            refreshTimer = setTimeout(() => {
                if (state.activeTab === 'model-health') loadModelHealth();
            }, 5000);
        }
    }
}

export async function testModelConnection(providerId) {
    if (testing.has(providerId)) return;
    testing.add(providerId);
    ++loadVersion;
    renderModels();
    try {
        const result = await window.apiPost('/models/test', {provider_id: providerId});
        window.showToast(result?.message || '连通性测试失败', result?.status === 'success' ? 'success' : 'error');
    } catch {
        window.showToast('连通性测试请求失败，请刷新查看模型状态', 'error');
    } finally {
        testing.delete(providerId);
        if (state.activeTab === 'model-health') await loadModelHealth();
    }
}

document.addEventListener('click', event => {
    const button = event.target.closest('[data-test-model]');
    if (button) testModelConnection(button.dataset.testModel);
    if (event.target.closest('#model-health-refresh')) loadModelHealth();
});
