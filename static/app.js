/* PDF表格填写工具 - 支持多表单切换 */

let sessionId = '';
let currentPage = 0;
let totalPages = 1;
let currentFormIndex = 0;
let forms = [];  // {index, name, total_fields, filled_fields}

// ─── 文件上传 ───
function setupDropZone(zoneId, listId, onFile) {
    const zone = document.getElementById(zoneId);
    const list = document.getElementById(listId);
    zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
    zone.addEventListener('drop', e => {
        e.preventDefault(); zone.classList.remove('dragover');
        Array.from(e.dataTransfer.files).forEach(f => { addFile(list, f); onFile(f); });
    });
    zone.addEventListener('click', () => {
        const input = document.createElement('input');
        input.type = 'file';         input.accept = '.pdf,.doc,.docx,.png,.jpg,.jpeg,.txt'; input.multiple = true;
        input.onchange = () => Array.from(input.files).forEach(f => { addFile(list, f); onFile(f); });
        input.click();
    });
}

function addFile(list, file) {
    const item = document.createElement('div');
    item.className = 'file-item';
    const ext = file.name.split('.').pop().toUpperCase();
    const size = file.size > 1048576 ? (file.size/1048576).toFixed(1)+' MB' : (file.size/1024).toFixed(0)+' KB';
    const iconSvg = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M9 1H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4z"/><path d="M9 1v3h3"/></svg>';
    item.innerHTML = `<span class="file-item-icon">${iconSvg}</span><span class="file-item-name">${file.name}</span><span class="file-item-size">${ext} · ${size}</span><span class="file-item-remove" onclick="this.parentElement.remove()">&times;</span>`;
    list.appendChild(item);
}

let studentFile = null, formFiles = [];
setupDropZone('studentDropZone', 'studentFiles', f => { studentFile = f; });
setupDropZone('formDropZone', 'formFiles', f => {
    if (!formFiles.find(x => x.name === f.name && x.size === f.size)) {
        formFiles.push(f);
    }
});

// ─── 步骤进度控制 ───
function setProgress(step) {
    const steps = document.querySelectorAll('.progress-step');
    steps.forEach((s, i) => {
        s.classList.remove('active', 'completed');
        if (i < step) s.classList.add('completed');
        else if (i === step) s.classList.add('active');
    });
}

// ─── 表单Tab渲染 ───
function renderFormTabs() {
    const container = document.getElementById('formTabs');
    container.innerHTML = '';
    forms.forEach((f, i) => {
        const tab = document.createElement('button');
        tab.className = 'form-tab' + (i === currentFormIndex ? ' active' : '');
        tab.textContent = f.name || `表单 ${i+1}`;
        tab.onclick = () => switchForm(i);
        container.appendChild(tab);
    });
}

// ─── 切换表单 ───
async function switchForm(index) {
    if (index === currentFormIndex || !sessionId) return;
    currentFormIndex = index;
    currentPage = 0;
    renderFormTabs();

    // 更新状态信息
    const f = forms[index];
    document.getElementById('statusText').textContent =
        `表单 ${index+1}: ${f.name} (${f.filled_fields}/${f.total_fields} 字段)`;

    // 清空聊天记录（不同表单字段不同）
    document.getElementById('aiMessages').innerHTML = '';

    // 重新加载预览
    loadPDFPreview(0);
}

// ─── 开始处理：上传 → 自动填充 → 生成PDF（支持多个PDF）───
async function startProcessing() {
    if (formFiles.length === 0) { alert('请上传PDF申请表'); return; }

    const btn = document.getElementById('processBtn');
    const waiting = document.getElementById('waitingState');
    const loading = document.getElementById('processingLoading');

    btn.disabled = true;
    waiting.style.display = 'none';
    loading.style.display = '';

    // 清除旧结果
    document.getElementById('resultSection').style.display = 'none';
    document.getElementById('formTabs').innerHTML = '';
    forms = [];

    try {
        // 1. 上传学生资料（仅第一次）
        if (studentFile && !sessionId) {
            setProgress(0);
            document.getElementById('loadingText').textContent = '正在上传并分析学生资料…';
            const sf = new FormData();
            sf.append('file', studentFile);
            const sr = await fetch('/api/upload-student', { method: 'POST', body: sf });
            if (!sr.ok) throw new Error('学生资料上传失败');
            const sd = await sr.json();
            sessionId = sd.session_id;
        }
        if (!sessionId) { alert('请先上传学生资料'); throw new Error('无session'); }

        // 2. 逐个上传PDF表单
        for (let fi = 0; fi < formFiles.length; fi++) {
            const file = formFiles[fi];
            setProgress(1);
            document.getElementById('loadingText').textContent = `正在识别表单 ${fi+1}/${formFiles.length}: ${file.name}…`;

            const ff = new FormData();
            ff.append('file', file);
            ff.append('session_id', sessionId);
            const fr = await fetch('/api/upload-form', { method: 'POST', body: ff });
            if (!fr.ok) {
                console.warn(`表单 ${file.name} 上传失败，跳过`);
                continue;
            }
            const fd = await fr.json();
            currentFormIndex = fd.form_index;

            // 生成填写版PDF
            document.getElementById('loadingText').textContent = `正在生成 ${file.name} 填写版…`;
            const gr = await fetch('/api/generate-pdf', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, form_index: currentFormIndex }),
            });
            if (!gr.ok) {
                console.warn(`表单 ${file.name} 生成失败，跳过`);
                continue;
            }
        }

        // 3. 获取表单列表，更新Tab
        await refreshFormList();

        // 4. 显示最后一个表单的结果
        loading.style.display = 'none';
        if (forms.length > 0) {
            currentFormIndex = forms.length - 1;
            renderFormTabs();
            const lastForm = forms[currentFormIndex];
            document.getElementById('statusText').textContent =
                `已处理 ${forms.length} 个表单，当前: ${lastForm.name} (${lastForm.filled_fields}/${lastForm.total_fields} 字段)`;
            loadPDFPreview(0);
            showAIWelcome();
        }

    } catch (err) {
        loading.style.display = 'none';
        waiting.style.display = '';
        btn.disabled = false;
        alert('处理失败: ' + err.message);
    }
}

async function refreshFormList() {
    if (!sessionId) return;
    try {
        const r = await fetch('/api/forms/' + sessionId);
        const d = await r.json();
        forms = d.forms || [];
        renderFormTabs();
        // 同步显示当前表单的Tab为激活
        const tabs = document.querySelectorAll('.form-tab');
        tabs.forEach((t, i) => t.classList.toggle('active', i === currentFormIndex));
    } catch {}
}

function showAIWelcome() {
    document.getElementById('resultSection').style.display = '';
    const msgs = document.getElementById('aiMessages');
    msgs.innerHTML = `
        <div class="ai-msg ai">
            <div class="ai-msg-avatar ai">AI</div>
            <div class="ai-msg-bubble">
                PDF已生成完成。你可以：<br>
                • 点击下方"下载填写版"获取PDF<br>
                • 在输入框中告诉我修改意见<br>
                • 修改完成后点击"重新生成"
            </div>
        </div>
    `;
}

// ─── PDF预览 ───
async function loadPDFPreview(page) {
    if (!sessionId) return;
    const body = document.getElementById('pdfBody');
    body.innerHTML = '<div class="spinner"><div class="spinner-ring"></div></div>';

    try {
        // 先获取总页数
        const infoResp = await fetch(`/api/pdf-info/${sessionId}?form_index=${currentFormIndex}`);
        if (infoResp.ok) {
            const info = await infoResp.json();
            console.log('PDF info:', info);
            totalPages = info.total_pages || 1;
        }

        const resp = await fetch(`/api/pdf-preview/${sessionId}?page=${page}&form_index=${currentFormIndex}`);
        const data = await resp.json();
        body.innerHTML = `<img src="${data.image}" alt="PDF预览">`;
        document.getElementById('pageIndicator').textContent = `${page + 1} / ${totalPages}`;
        console.log('Page indicator:', `${page + 1} / ${totalPages}`);
    } catch (err) {
        console.error('Preview error:', err);
        body.innerHTML = '<div style="color:var(--muted)">预览加载失败</div>';
    }
}

function changePage(delta) {
    currentPage = Math.max(0, Math.min(totalPages - 1, currentPage + delta));
    loadPDFPreview(currentPage);
}

// ─── 下载/重新生成 ───
async function downloadPDF() {
    if (!sessionId) return;
    try {
        const resp = await fetch(`/api/download-pdf/${sessionId}?form_index=${currentFormIndex}`);
        if (!resp.ok) throw new Error('下载失败');
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        const name = forms[currentFormIndex]?.name || 'filled_form.pdf';
        a.href = url; a.download = name; a.click();
        URL.revokeObjectURL(url);
    } catch (err) { alert('下载失败: ' + err.message); }
}

async function downloadAll() {
    if (!sessionId || forms.length === 0) return;
    for (let i = 0; i < forms.length; i++) {
        try {
            const resp = await fetch(`/api/download-pdf/${sessionId}?form_index=${i}`);
            if (!resp.ok) continue;
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `filled_${forms[i]?.name || `form_${i+1}.pdf`}`;
            a.click();
            URL.revokeObjectURL(url);
            await new Promise(r => setTimeout(r, 500));
        } catch {}
    }
}

async function regeneratePDF() {
    if (!sessionId) return;
    const body = document.getElementById('pdfBody');
    body.innerHTML = '<div class="spinner"><div class="spinner-ring"></div></div>';
    try {
        const resp = await fetch('/api/generate-pdf', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId, form_index: currentFormIndex }),
        });
        if (!resp.ok) throw new Error('生成失败');
        document.getElementById('statusText').textContent = 'PDF 已重新生成';
        loadPDFPreview(currentPage);
    } catch (err) { alert('重新生成失败: ' + err.message); }
}

// ─── AI对话 ───
async function sendAI() {
    const input = document.getElementById('aiInput');
    const text = input.value.trim();
    if (!text || !sessionId) return;

    const msgs = document.getElementById('aiMessages');
    msgs.innerHTML += `<div class="ai-msg user"><div class="ai-msg-avatar user">你</div><div class="ai-msg-bubble">${esc(text)}</div></div>`;
    input.value = '';
    msgs.scrollTop = msgs.scrollHeight;

    const thinkingId = 'thinking-' + Date.now();
    msgs.innerHTML += `<div class="ai-msg ai" id="${thinkingId}"><div class="ai-msg-avatar ai">AI</div><div class="ai-msg-bubble">处理中...</div></div>`;
    msgs.scrollTop = msgs.scrollHeight;

    try {
        const resp = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: text, session_id: sessionId, form_index: currentFormIndex }),
        });
        const data = await resp.json();

        const el = document.getElementById(thinkingId);
        let reply = '';
        if (data.action === 'updated') {
            reply = `已更新 ${data.fields.length} 个字段。点击"重新生成"查看效果。`;
        } else if (data.action === 'deleted') {
            reply = '已删除指定字段。点击"重新生成"查看效果。';
        } else {
            reply = data.message || '处理完成。';
        }
        if (el) el.querySelector('.ai-msg-bubble').innerHTML = reply;
    } catch (err) {
        const el = document.getElementById(thinkingId);
        if (el) el.querySelector('.ai-msg-bubble').innerHTML = '出错: ' + err.message;
    }

    msgs.scrollTop = msgs.scrollHeight;
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// ─── 重置 ───
function resetAll() {
    sessionId = ''; currentPage = 0; currentFormIndex = 0;
    studentFile = null; formFiles = []; forms = [];
    document.getElementById('resultSection').style.display = 'none';
    document.getElementById('formTabs').innerHTML = '';
    document.getElementById('processingLoading').style.display = 'none';
    document.getElementById('waitingState').style.display = '';
    document.getElementById('processBtn').disabled = false;
    document.getElementById('studentFiles').innerHTML = '';
    document.getElementById('formFiles').innerHTML = '';
    document.getElementById('aiMessages').innerHTML = '';
    document.querySelectorAll('.progress-step').forEach(s => s.classList.remove('active', 'completed'));
    document.getElementById('loadingText').textContent = 'AI 正在提取学生资料…';
}
