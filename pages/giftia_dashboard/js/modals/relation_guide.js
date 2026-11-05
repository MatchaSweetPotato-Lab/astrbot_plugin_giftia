// Bot-scoped affinity guidance editor.
let defaultGuide = '';
let editorVersion = 0;

window.openRelationScoreGuideModal = async function() {
    const bot = document.getElementById('profile-bot-name').value;
    if (!bot) {
        window.showToast('请先选择 Bot');
        return;
    }
    const button = document.getElementById('relation-score-guide-btn');
    if (button.disabled) return;
    button.disabled = true;
    try {
        const res = await window.apiGet('/profiles/user/relation_guide', { bot_name: bot });
        if (res.status !== 'success' || !res.data) throw new Error(res.message || '读取失败');
        editorVersion++;
        defaultGuide = res.data.default_guide;
        document.getElementById('relation-score-guide-bot').value = bot;
        document.getElementById('relation-score-guide-scope').textContent = `Bot：${bot} · 所有群聊与私聊共用`;
        document.getElementById('relation-score-guide-text').value = res.data.guide;
        document.getElementById('relation-score-guide-save').disabled = false;
        window.openModal('relation-score-guide-modal');
        document.getElementById('relation-score-guide-content').scrollTop = 0;
        document.getElementById('relation-score-guide-text').scrollTop = 0;
    } catch (e) {
        window.showToast(`读取好感度说明失败：${e.message}`);
    } finally {
        button.disabled = false;
    }
};

window.restoreDefaultRelationScoreGuide = function() {
    document.getElementById('relation-score-guide-text').value = defaultGuide;
    window.showToast('已填入默认说明，保存后生效');
};

window.saveRelationScoreGuide = async function() {
    const button = document.getElementById('relation-score-guide-save');
    if (button.disabled) return;
    const bot = document.getElementById('relation-score-guide-bot').value;
    const version = editorVersion;
    const guide = document.getElementById('relation-score-guide-text').value.trim();
    if (!bot || guide.length > 12000) {
        window.showToast('请选择 Bot，好感度说明最多 12000 字');
        return;
    }
    button.disabled = true;
    try {
        const res = await window.apiPost('/profiles/user/relation_guide', { bot_name: bot, guide });
        if (res.status !== 'success') throw new Error(res.message || '保存失败');
        window.showToast('好感度说明已保存');
        if (version === editorVersion) window.closeModal('relation-score-guide-modal');
    } catch (e) {
        window.showToast(`保存好感度说明失败：${e.message}`);
    } finally {
        if (version === editorVersion) button.disabled = false;
    }
};
