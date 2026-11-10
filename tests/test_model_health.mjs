import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

const fields = new Map();
globalThis.document = {
    addEventListener() {},
    getElementById(id) {
        if (!fields.has(id)) fields.set(id, {innerHTML: '', textContent: '', disabled: false, querySelectorAll: () => []});
        return fields.get(id);
    }
};
globalThis.window = {showToast() {}};
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
globalThis.ResizeObserver = class { observe() {} };
await import('../pages/giftia_dashboard/js/utils.js');
await import('../pages/giftia_dashboard/js/api.js');
const apiPost = window.apiPost;
const originalFetch = globalThis.fetch;
const { state } = await import('../pages/giftia_dashboard/js/modules/state.js');
const { ALL_TABS, DEFAULT_PINNED_IDS } = await import('../pages/giftia_dashboard/js/modules/nav_manager.js');
const { loadModelHealth, testModelConnection, saveModelHealthSettings } = await import('../pages/giftia_dashboard/js/modules/model_health.js');
const get = id => document.getElementById(id);
const model = status => ({provider_id: "provider'<>id", name: '<script>name</script>', model: '<img src=x>', registered: true, status, failures: status === 'cooldown' ? 2 : 0, retry_in_seconds: 60, uses: ['bot · 正式回复'], reason: 'server_error'});

beforeEach(() => {
    fields.clear();
    delete window.AstrBotPluginPage;
    globalThis.fetch = originalFetch;
    state.activeTab = 'model-health';
    window.showToast = () => {};
    window.apiGet = async () => ({status: 'success', data: {enabled: true, models: [model('cooldown')]}});
});

test('model status is discoverable in the standard navigation', () => {
    assert.equal(ALL_TABS.find(tab => tab.id === 'model-health').name, '模型状态');
    assert.ok(DEFAULT_PINNED_IDS.includes('model-health'));
});

test('model status escapes provider names, model IDs and action attributes', async () => {
    await loadModelHealth();
    const html = get('model-health-list').innerHTML;
    assert.match(html, /冷却中/);
    assert.match(html, /60 秒/);
    assert.match(html, /&lt;script&gt;name/);
    assert.doesNotMatch(html, /<script>|<img|data-test-model="provider'<>/);
});

test('duplicate tests are blocked and success refreshes the shared health display', async () => {
    await loadModelHealth();
    let finish;
    let calls = 0;
    window.apiPost = async (endpoint, body) => {
        calls++;
        assert.equal(endpoint, '/models/test');
        assert.equal(body.provider_id, "provider'<>id");
        return await new Promise(resolve => { finish = resolve; });
    };
    const pending = testModelConnection("provider'<>id");
    await testModelConnection("provider'<>id");
    assert.equal(calls, 1);
    assert.match(get('model-health-list').innerHTML, /disabled/);
    window.apiGet = async () => ({status: 'success', data: {enabled: true, models: [model('healthy')]}});
    finish({status: 'success', message: '恢复成功'});
    await pending;
    assert.match(get('model-health-list').innerHTML, /健康/);
    assert.doesNotMatch(get('model-health-list').innerHTML, /冷却中|disabled/);
});

for (const bridged of [false, true]) {
    test(`successful connectivity test shows success with ${bridged ? 'unwrapped bridge' : 'direct'} response`, async () => {
        await loadModelHealth();
        const notifications = [];
        window.showToast = (...args) => notifications.push(args);
        window.apiPost = apiPost;
        if (bridged) {
            window.AstrBotPluginPage = {
                apiPost: async (endpoint, body) => {
                    assert.equal(endpoint, '/models/test');
                    assert.equal(body.provider_id, "provider'<>id");
                    return model('healthy');
                }
            };
        } else {
            globalThis.fetch = async (endpoint, options) => {
                assert.equal(endpoint, '/models/test');
                assert.equal(options.method, 'POST');
                assert.equal(JSON.parse(options.body).provider_id, "provider'<>id");
                return {json: async () => ({status: 'success', message: '连通性测试通过，已恢复共享健康状态', data: model('healthy')})};
            };
        }
        window.apiGet = async () => ({status: 'success', data: {enabled: true, models: [model('healthy')]}});
        await testModelConnection("provider'<>id");
        assert.deepEqual(notifications, [['连通性测试通过，已恢复共享健康状态', 'success']]);
        assert.match(get('model-health-list').innerHTML, /健康/);
    });
}

test('a failed bridge connectivity test keeps the failure notification and cooldown', async () => {
    await loadModelHealth();
    const notifications = [];
    window.showToast = (...args) => notifications.push(args);
    window.apiPost = apiPost;
    window.AstrBotPluginPage = {
        apiPost: async () => { throw new Error('测试未收到文本响应，未恢复健康状态'); }
    };
    await testModelConnection("provider'<>id");
    assert.deepEqual(notifications, [['测试未收到文本响应，未恢复健康状态', 'error']]);
    assert.match(get('model-health-list').innerHTML, /冷却中/);
});

test('a stale list response cannot overwrite a newer health snapshot', async () => {
    let finish;
    window.apiGet = () => new Promise(resolve => { finish = resolve; });
    const pending = loadModelHealth();
    window.apiGet = async () => ({status: 'success', data: {enabled: true, models: [model('healthy')]}});
    await loadModelHealth();
    finish({status: 'success', data: {enabled: true, models: [model('cooldown')]}});
    await pending;
    assert.match(get('model-health-list').innerHTML, /健康/);
    assert.doesNotMatch(get('model-health-list').innerHTML, /冷却中/);
});

test('refresh errors preserve the last known rows and re-enable refresh', async () => {
    await loadModelHealth();
    const before = get('model-health-list').innerHTML;
    window.apiGet = async () => { throw new Error('offline'); };
    await loadModelHealth();
    assert.equal(get('model-health-list').innerHTML, before);
    assert.equal(get('model-health-error').textContent, 'offline');
    assert.equal(get('model-health-refresh').disabled, false);
});

test('the global switch saves and disables testing while manual disables remain visible', async () => {
    await loadModelHealth();
    const updated = {enabled: false, models: [{...model('disabled'), enabled: false}, {...model('unmanaged'), provider_id: 'other', enabled: true}]};
    window.apiPost = async (endpoint, body) => {
        assert.equal(endpoint, '/models/settings');
        assert.deepEqual(body, {enabled: false});
        return {status: 'success', data: updated};
    };
    window.apiGet = async () => ({status: 'success', data: updated});
    await saveModelHealthSettings({enabled: false});
    assert.equal(get('model-health-enabled').checked, false);
    assert.match(get('model-health-hint').textContent, /手动停用.*仍会跳过/);
    assert.match(get('model-health-list').innerHTML, /已手动停用/);
    assert.match(get('model-health-list').innerHTML, /未启用管理/);
    window.apiPost = async () => assert.fail('disabled management must not send test requests');
    await testModelConnection('other');
});

test('manual disable blocks tests and can be enabled again', async () => {
    const providerId = "provider'<>id";
    let updated = {enabled: true, models: [{...model('disabled'), enabled: false}]};
    window.apiGet = async () => ({status: 'success', data: updated});
    await loadModelHealth();
    assert.match(get('model-health-list').innerHTML, /不发起探测/);
    assert.match(get('model-health-list').innerHTML, /data-toggle-model="provider&#039;&lt;&gt;id"[^>]*>启用/);
    window.apiPost = async () => assert.fail('disabled providers must not be tested');
    await testModelConnection(providerId);
    window.apiPost = async (endpoint, body) => {
        assert.equal(endpoint, '/models/settings');
        assert.deepEqual(body, {provider_id: providerId, enabled: true});
        updated = {enabled: true, models: [{...model('unknown'), enabled: true}]};
        return {status: 'success', data: updated};
    };
    await saveModelHealthSettings({provider_id: providerId, enabled: true});
    assert.match(get('model-health-list').innerHTML, /未检测/);
    assert.doesNotMatch(get('model-health-list').innerHTML, /已手动停用/);
});

test('failed setting saves restore the switch and preserve existing rows', async () => {
    await loadModelHealth();
    const notifications = [];
    window.showToast = (...args) => notifications.push(args);
    window.apiPost = async () => ({status: 'error', message: '保存失败'});
    get('model-health-enabled').checked = false;
    await saveModelHealthSettings({enabled: false});
    assert.equal(get('model-health-enabled').checked, true);
    assert.equal(get('model-health-enabled').disabled, false);
    assert.match(get('model-health-list').innerHTML, /冷却中/);
    assert.deepEqual(notifications, [['保存失败', 'error']]);
});

test('duplicate setting saves cannot send conflicting writes', async () => {
    await loadModelHealth();
    let finish;
    let calls = 0;
    window.apiPost = () => {
        calls++;
        return new Promise(resolve => { finish = resolve; });
    };
    const pending = saveModelHealthSettings({enabled: false});
    await saveModelHealthSettings({enabled: true});
    assert.equal(calls, 1);
    assert.equal(get('model-health-enabled').disabled, true);
    finish({status: 'success', data: {enabled: false, models: [model('unmanaged')]}});
    await pending;
    assert.equal(get('model-health-enabled').disabled, false);
});

test('management can be configured before any models have been selected', async () => {
    window.apiGet = async () => ({status: 'success', data: {enabled: false, models: []}});
    await loadModelHealth();
    assert.equal(get('model-health-enabled').checked, false);
    assert.equal(get('model-health-enabled').disabled, false);
    assert.match(get('model-health-list').innerHTML, /尚未配置对话模型/);
    window.apiPost = async () => ({status: 'success', data: {enabled: true, models: []}});
    window.apiGet = async () => ({status: 'success', data: {enabled: true, models: []}});
    await saveModelHealthSettings({enabled: true});
    assert.equal(get('model-health-enabled').checked, true);
});
