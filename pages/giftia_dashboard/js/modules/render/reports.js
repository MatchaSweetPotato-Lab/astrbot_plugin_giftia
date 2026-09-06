let loaded = false;
let loading = false;
let bound = false;
let templates = [];
const drafts = new Map();
let currentType = '';
let previewTimer;
let revision = 0;
let saving = false;
let rendering = false;
const el = id => document.getElementById(`report-${id}`);

function message(text, error = false) {
    el('message').textContent = text;
    el('message').classList.toggle('error', error);
}

function requireSuccess(response) {
    if (!response || response.status !== 'success') throw new Error(response?.message || '请求失败');
    return response.data;
}

function captureDraft() {
    if (!currentType) return;
    drafts.set(currentType, { html: el('html').value, data: el('data').value });
}

function getReportTypeIconSvg(type) {
    if (type === 'status') {
        return '<svg class="report-type-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>';
    }
    if (type === 'user_profile') {
        return '<svg class="report-type-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';
    }
    return '<svg class="report-type-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"></rect><path d="M7 8h10M7 12h6M7 16h10"></path></svg>';
}

function updateSegmentDirtyState() {
    const segmented = el('type-segmented');
    if (!segmented) return;
    const buttons = segmented.querySelectorAll('.report-type-btn');
    buttons.forEach(btn => {
        const type = btn.dataset.type;
        const orig = templates.find(item => item.report_type === type);
        let isDirty = false;
        if (type === currentType) {
            isDirty = orig && el('html').value !== orig.html;
        } else if (drafts.has(type)) {
            isDirty = orig && drafts.get(type).html !== orig.html;
        }
        btn.classList.toggle('has-dirty', Boolean(isDirty));
    });
}

function setTypeSegmentsDisabled(disabled) {
    const segmented = el('type-segmented');
    if (!segmented) return;
    segmented.querySelectorAll('.report-type-btn').forEach(btn => {
        btn.disabled = disabled;
    });
}

function renderTypeSegments() {
    const segmented = el('type-segmented');
    if (!segmented) return;
    segmented.replaceChildren();
    for (const item of templates) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `report-type-btn${item.report_type === currentType ? ' active' : ''}`;
        btn.dataset.type = item.report_type;
        btn.setAttribute('role', 'tab');
        btn.setAttribute('aria-selected', item.report_type === currentType ? 'true' : 'false');
        btn.innerHTML = `${getReportTypeIconSvg(item.report_type)}<span>${item.label}</span><span class="pill-dirty-dot" title="有未保存的修改"></span>`;
        btn.addEventListener('click', () => {
            if (currentType === item.report_type) return;
            selectTemplate(item.report_type);
        });
        segmented.appendChild(btn);
    }
    updateSegmentDirtyState();
}

function markDirty() {
    captureDraft();
    const original = templates.find(item => item.report_type === currentType);
    const dirty = original && el('html').value !== original.html;
    el('dirty').textContent = dirty ? '有未保存的修改' : '已保存';
    el('dirty').classList.toggle('dirty', dirty);
    el('render-result').hidden = true;
    updateSegmentDirtyState();
    revision += 1;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(preview, 450);
}

async function preview() {
    const requestRevision = revision;
    try {
        const data = JSON.parse(el('data').value);
        const result = requireSuccess(await window.apiPost('/reports/preview', {
            report_type: currentType, html: el('html').value, data, mode: 'html'
        }));
        if (requestRevision !== revision) return;
        el('preview').srcdoc = result.html;
        el('preview-error').hidden = true;
    } catch (error) {
        if (requestRevision !== revision) return;
        el('preview-error').textContent = `预览未更新：${error.message}`;
        el('preview-error').hidden = false;
    }
}

function selectTemplate(type) {
    captureDraft();
    currentType = type;
    if (el('type') && el('type').value !== type) {
        el('type').value = type;
    }
    const segmented = el('type-segmented');
    if (segmented) {
        segmented.querySelectorAll('.report-type-btn').forEach(btn => {
            const isActive = btn.dataset.type === type;
            btn.classList.toggle('active', isActive);
            btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
        });
    }
    const template = templates.find(item => item.report_type === type);
    if (!template) return;
    const draft = drafts.get(type);
    el('html').value = draft?.html ?? template.html;
    el('data').value = draft?.data ?? JSON.stringify(template.sample_data, null, 2);
    el('fields').replaceChildren();
    for (const [name, description] of Object.entries(template.fields)) {
        const code = document.createElement('code');
        code.textContent = `{{ ${name} }}`;
        const label = document.createElement('span');
        label.textContent = description;
        el('fields').append(code, label);
    }
    markDirty();
}

async function loadAssets() {
    const assets = requireSuccess(await window.apiGet('/reports/assets'));
    el('assets').replaceChildren();
    if (!assets.length) el('assets').textContent = '暂无素材，上传背景图或装饰图片后即可插入模板。';
    for (const asset of assets) {
        const button = document.createElement('button');
        button.className = 'report-asset';
        button.title = `插入图片 ${asset.name}`;
        const image = document.createElement('img');
        image.src = asset.url;
        image.alt = `素材 ${asset.name.slice(0, 8)}`;
        const label = document.createElement('span');
        label.textContent = `${asset.name.slice(0, 8)} · ${Math.ceil(asset.size / 1024)} KB`;
        button.append(image, label);
        button.addEventListener('click', () => {
            const editor = el('html');
            editor.setRangeText(`<img src="{{ asset('${asset.name}') }}" alt="" style="max-width:100%">`, editor.selectionStart, editor.selectionEnd, 'end');
            editor.focus();
            markDirty();
        });
        el('assets').append(button);
    }
}

export async function loadReportTemplates() {
    if (loading) return;
    if (loaded) {
        // Preserve drafts when returning from another dashboard tab.
        el('preview').style.zoom = Math.min(1, el('preview').parentElement.clientWidth / 800);
        updateSegmentDirtyState();
        return;
    }
    loading = true;
    if (!bound) {
        bound = true;
        el('type').addEventListener('change', event => selectTemplate(event.target.value));
        el('html').addEventListener('input', markDirty);
        el('data').addEventListener('input', markDirty);
        el('default').addEventListener('click', () => {
            el('html').value = templates.find(item => item.report_type === currentType).default_html;
            markDirty();
            message('已载入默认模板，点击保存后生效。');
        });
        el('save').addEventListener('click', async () => {
            if (saving) return;
            saving = true;
            el('save').disabled = true;
            const type = currentType;
            const html = el('html').value;
            try {
                requireSuccess(await window.apiPost('/reports/templates/save', { report_type: type, html }));
                templates.find(item => item.report_type === type).html = html;
                markDirty();
                message('模板已保存。插件配置 → 报告渲染配置 → 渲染模式选择「图片响应」后，对应报告指令将使用此模板。');
            } catch (error) { message(error.message, true); }
            finally { saving = false; el('save').disabled = false; }
        });
        el('render').addEventListener('click', async () => {
            if (rendering) return;
            rendering = true;
            el('render').disabled = true;
            el('render').textContent = '渲染中…';
            const requestRevision = revision;
            try {
                const data = JSON.parse(el('data').value);
                const result = requireSuccess(await window.apiPost('/reports/preview', {
                    report_type: currentType, html: el('html').value, data, mode: 'image'
                }));
                if (requestRevision !== revision) { message('模板已修改，请重新试渲染以查看最新结果。'); return; }
                el('image').src = result.image;
                el('render-result').hidden = false;
                message('t2i 试渲染完成。');
            } catch (error) { message(error.message, true); }
            finally { rendering = false; el('render').disabled = false; el('render').textContent = 't2i 试渲染'; }
        });
        el('upload').addEventListener('change', async event => {
            const files = Array.from(event.target.files || []);
            el('upload').disabled = true;
            let count = 0;
            try {
                for (const file of files) {
                    if (file.size > 2 * 1024 * 1024) throw new Error(`${file.name} 超过 2 MB`);
                    message(`正在上传 ${file.name}…`);
                    const dataUrl = await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = () => reject(new Error('无法读取图片'));
                        reader.readAsDataURL(file);
                    });
                    requireSuccess(await window.apiPost('/reports/assets/upload', { base64: dataUrl.split(',')[1] }));
                    count += 1;
                }
                message(`已上传 ${count} 张图片，点击素材可插入模板。`);
            } catch (error) { message(`已上传 ${count} 张。${error.message}`, true); }
            finally {
                event.target.value = '';
                el('upload').disabled = false;
                try { await loadAssets(); } catch (error) { message(error.message, true); }
            }
        });
        const resizeObserver = new ResizeObserver(() => {
            const width = el('preview').parentElement.clientWidth;
            if (width) el('preview').style.zoom = Math.min(1, width / 800);
        });
        resizeObserver.observe(el('preview').parentElement);
        window.addEventListener('beforeunload', event => {
            captureDraft();
            if (templates.some(item => drafts.has(item.report_type) && drafts.get(item.report_type).html !== item.html)) {
                event.preventDefault();
                event.returnValue = '';
            }
        });
    }
    try {
        templates = requireSuccess(await window.apiGet('/reports/templates'));
        el('type').replaceChildren(...templates.map(item => new Option(item.label, item.report_type)));
        if (!templates.length) throw new Error('暂无可用的报告类型');
        renderTypeSegments();
        for (const id of ['type', 'html', 'default', 'save', 'render', 'upload']) el(id).disabled = false;
        setTypeSegmentsDisabled(false);
        selectTemplate(templates[0].report_type);
        loaded = true;
        await loadAssets();
    } catch (error) { message(`加载失败：${error.message}。可重新进入此页重试。`, true); }
    finally { loading = false; }
}
