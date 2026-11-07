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
const expandedUses = new Set();
let usageObserver;
const escape = value => window.escapeHtml(String(value ?? ''));
const formatTime = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12: false}) : '—';

function updateUsageOverflow() {
    const container = document.getElementById('model-health-list');
    if (!container) return;
    for (const usage of container.querySelectorAll('.model-health-usage')) {
        const list = usage.querySelector('.model-health-uses');
        const button = usage.querySelector('[data-model-uses]');
        const expanded = expandedUses.has(button.dataset.modelUses);
        list.classList.toggle('is-expanded', expanded);
        button.hidden = !expanded;
        if (!expanded) {
            button.hidden = list.scrollHeight <= list.clientHeight + 1
                && [...list.children].every(label => label.scrollWidth <= label.clientWidth + 1);
        }
        button.textContent = expanded ? '收起' : '展开';
        button.setAttribute('aria-expanded', String(expanded));
    }
}

function renderModels() {
    const container = document.getElementById('model-health-list');
    if (!container) return;
    document.getElementById('model-health-count').textContent = `已配置 ${models.length} 个供应商`;
    if (!models.length) {
        container.innerHTML = '<tr class="model-health-empty"><td colspan="6" class="empty-state">尚未配置对话模型。请在机器人管理或插件设置中选择供应商。</td></tr>';
        return;
    }
    container.innerHTML = models.map((model, index) => {
        const busy = testing.has(model.provider_id) || model.status === 'probing';
        const status = !model.registered ? 'missing' : (busy ? 'probing' : model.status);
        const color = status === 'healthy' ? 'success' : (['cooldown', 'missing'].includes(status) ? 'danger' : 'secondary');
        const detail = status === 'cooldown' ? `约 ${model.retry_in_seconds} 秒后可试探`
            : status === 'probe_due' ? '等待后台连通性测试'
            : status === 'missing' ? '供应商配置尚未加载或已删除'
            : (reasonLabels[model.reason] || '');
        return `<tr>
            <td class="model-health-name" data-label="供应商 / 模型"><strong>${escape(model.name || model.provider_id)}</strong></td>
            <td class="model-health-usage-cell" data-label="使用位置"><div class="model-health-usage">
                <div class="model-health-uses" id="model-health-uses-${index}">${model.uses.map(use => `<span class="badge badge-secondary">${escape(use)}</span>`).join('')}</div>
                <button class="model-health-uses-toggle" type="button" data-model-uses="${escape(model.provider_id)}" aria-controls="model-health-uses-${index}" aria-expanded="false" hidden>展开</button>
            </div></td>
            <td class="model-health-status" data-label="健康状态"><span class="badge badge-${color}">${escape(statusLabels[status] || status)}</span>${detail ? `<div class="muted-text model-health-detail">${escape(detail)}</div>` : ''}${model.reason && detail !== reasonLabels[model.reason] ? `<div class="muted-text">${escape(reasonLabels[model.reason] || '')}</div>` : ''}${model.failures ? `<div class="muted-text">连续失败 ${model.failures} 次</div>` : ''}</td>
            <td class="model-health-attempt" data-label="最近请求 / 耗时">${escape(formatTime(model.last_attempt))}<div class="muted-text">${model.last_latency_ms == null ? '—' : `${escape(model.last_latency_ms)} ms`}</div></td>
            <td class="model-health-success" data-label="最近成功">${escape(formatTime(model.last_success))}</td>
            <td class="model-health-action" data-label="操作"><button class="btn btn-small btn-secondary" data-test-model="${escape(model.provider_id)}" ${busy ? 'disabled' : ''}>${busy ? '测试中…' : '测试'}</button></td>
        </tr>`;
    }).join('');
    if (!usageObserver) {
        usageObserver = new ResizeObserver(updateUsageOverflow);
        usageObserver.observe(container);
    }
    updateUsageOverflow();
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
        const success = result?.status === 'success';
        window.showToast(result?.message || (success ? '连通性测试通过，已恢复共享健康状态' : '连通性测试失败'), success ? 'success' : 'error');
    } catch {
        window.showToast('连通性测试请求失败，请刷新查看模型状态', 'error');
    } finally {
        testing.delete(providerId);
        if (state.activeTab === 'model-health') await loadModelHealth();
    }
}

document.addEventListener('click', event => {
    const usageButton = event.target.closest('[data-model-uses]');
    if (usageButton) {
        const providerId = usageButton.dataset.modelUses;
        if (expandedUses.has(providerId)) expandedUses.delete(providerId);
        else expandedUses.add(providerId);
        updateUsageOverflow();
    }
    const button = event.target.closest('[data-test-model]');
    if (button) testModelConnection(button.dataset.testModel);
    if (event.target.closest('#model-health-refresh')) loadModelHealth();
});
