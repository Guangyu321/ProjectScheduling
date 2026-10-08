'use strict';
// The editor owns a draft. The execution view always renders the server's frozen run.
const Composer = (() => {
    const storageKey = 'labflow.recipe-draft.v1';
    let cell, catalog = [], records = [], draft, baseVersion = 0, dirty = false, working = false, runSnapshot;
    const clone = value => JSON.parse(JSON.stringify(value));
    const freshId = prefix => prefix + '_' + crypto.randomUUID().replaceAll('-', '').slice(0, 12);
    const labelFor = cap => catalog.find(c => c.capability === cap)?.label || cap;
    const recordKey = record => `${record.recipe.id}@${record.recipe.version}`;
    const option = (value, label, selected) => `<option value="${esc(value)}" ${value === selected ? 'selected' : ''}>${esc(label)}</option>`;
    const plateTypes = () => [...new Set(cell.locations.flatMap(p => p.plate_types))];

    function persist() {
        try { localStorage.setItem(storageKey, JSON.stringify({ draft, baseVersion, dirty })); } catch { /* Saving to the backend remains available. */ }
    }
    function status() {
        $('recipe-status').textContent = dirty ? `未保存的修改${baseVersion ? ' · 基于 v' + baseVersion : ''}` :
            draft.id.startsWith('builtin_') ? '内置模板 · 可另存' : baseVersion ? `已保存 · v${baseVersion}` : '新流程草稿';
        $('operation-count').textContent = `${draft.operations.length} 步`;
    }
    function changed() {
        dirty = true; persist(); status();
        $('preview-status').textContent = '流程已修改，请重新预演。';
        $('preview-status').className = 'muted'; $('recipe-preview').replaceChildren();
    }
    function renderSteps() {
        $('recipe-steps').innerHTML = draft.operations.map((op, i) => {
            const devices = catalog.find(c => c.capability === op.capability)?.devices || [];
            const slots = cell.locations.filter(p => devices.includes(p.device_id) && (!op.device_id || p.device_id === op.device_id));
            return `<article class="operation-card" data-op-index="${i}">
                <div class="operation-head"><h3><span class="step-number">${i + 1}</span>${esc(labelFor(op.capability))}</h3>
                <div class="step-actions"><button data-op-action="up" aria-label="上移第 ${i + 1} 步" ${i === 0 ? 'disabled' : ''}>↑</button><button data-op-action="down" aria-label="下移第 ${i + 1} 步" ${i === draft.operations.length - 1 ? 'disabled' : ''}>↓</button><button data-op-action="duplicate">复制</button><button data-op-action="remove" class="danger">删除</button></div></div>
                <div class="operation-fields"><label>执行设备<select data-op-field="device_id">${option('', '按能力自动匹配', op.device_id || '')}${devices.map(id => option(id, `${names[id] || id} · ${id}`, op.device_id)).join('')}</select></label>
                <label>操作板位<select data-op-field="location">${option('', '自动选择兼容板位', op.location || '')}${slots.map(p => option(p.id, p.id, op.location)).join('')}</select></label>
                <label class="method-field">${op.capability === 'identity.scan' ? '身份核对' : '实验方法标识'}${op.capability === 'identity.scan' ? '<span>使用每块样品板填写的条码自动核对</span>' : `<input data-op-field="method_id" value="${esc(op.method_id)}" maxlength="200" placeholder="例如 METHOD_A">`}<small>步骤标识：${esc(op.id)}</small></label>
                <label>预计时长（秒）<input data-op-field="estimated_duration_s" type="number" min="0.01" max="86400" step="any" value="${esc(op.estimated_duration_s)}"></label>
                <label>超时时间（秒）<input data-op-field="timeout_s" type="number" min="0.01" max="86400" step="any" value="${esc(op.timeout_s)}"></label></div></article>`;
        }).join('') || '<div class="empty">还没有步骤。点击左侧操作库开始组合。</div>';
    }
    function renderPlates() {
        const locations = cell.locations.filter(p => cell.transport.reachable_locations.includes(p.id));
        $('recipe-plates').innerHTML = draft.plates.map((p, i) => `<article class="plate-editor" data-plate-index="${i}">
            <div class="operation-head"><h3>样品板 ${i + 1}</h3><button data-remove-plate class="danger" ${draft.plates.length === 1 ? 'disabled' : ''}>移除</button></div>
            <div class="plate-fields"><label>板标识<input data-plate-field="id" maxlength="80" value="${esc(p.id)}"></label><label>条码<input data-plate-field="barcode" value="${esc(p.barcode)}"></label><label>板型<select data-plate-field="type">${plateTypes().map(t => option(t, t, p.type)).join('')}</select></label>
            <label>初始位置<select data-plate-field="initial_location">${locations.map(l => option(l.id, l.id, p.initial_location)).join('')}</select></label><label>最终位置<select data-plate-field="final_location">${locations.map(l => option(l.id, l.id, p.final_location)).join('')}</select></label></div></article>`).join('');
        $('add-plate').disabled = draft.plates.length >= 8;
    }
    function renderEditor() {
        $('recipe-name').value = draft.name; $('execution-mode').value = draft.execution_mode || 'serial';
        renderSteps(); renderPlates(); status();
    }
    function templateOptions() {
        const previous = $('recipe-template').value;
        $('recipe-template').innerHTML = records.map(r => option(recordKey(r), `${r.recipe.name} · v${r.recipe.version}${r.builtin ? '（内置）' : ''}`, previous)).join('');
    }
    async function reloadTemplates() { records = await api('/api/v1/recipes'); templateOptions(); }
    function adopt(recipe, version = 0, isDirty = true) {
        draft = clone(recipe); baseVersion = version; dirty = isDirty;
        persist(); renderEditor(); $('recipe-preview').replaceChildren();
        $('preview-status').textContent = '编辑完成后，先预演检查流程。'; $('preview-status').className = 'muted';
    }
    async function task(fn) {
        if (working) return;
        working = true;
        const controls = [...$('view-composer').querySelectorAll('input,select,button')].map(el => [el, el.disabled]);
        controls.forEach(([el]) => { el.disabled = true; });
        try { await fn(); }
        catch (e) { $('preview-status').textContent = e.message; $('preview-status').className = 'error'; note(e.message, true); }
        finally { working = false; controls.forEach(([el, disabled]) => { el.disabled = disabled; }); }
    }
    function renderPreview(report) {
        const { summary, workflow, timeline } = report;
        $('preview-status').textContent = `预演通过：${summary.plates} 块板，每板 ${summary.operations} 个实验步骤；共 ${summary.steps} 个执行步骤（含 ${summary.transports} 次转运），预计 ${Number(summary.duration_s.toFixed(2))} 秒。`;
        $('preview-status').className = 'success';
        const duration = Math.max(1, summary.duration_s), definitions = Object.fromEntries(workflow.steps.map(s => [s.id, s]));
        let html = '<div class="preview-timeline"><div class="time-axis"><div></div><div class="axis-track">' +
            Array.from({ length: 5 }, (_, i) => `<span>${Math.round(duration * i / 4)}s</span>`).join('') + '</div></div>';
        for (const device of cell.devices.filter(d => timeline.some(t => t.device_id === d.id))) {
            html += `<div class="lane"><span class="lane-label">${esc(names[device.id] || device.id)}</span><div class="lane-track">`;
            for (const t of timeline.filter(t => t.device_id === device.id)) {
                const index = workflow.plates.findIndex(p => p.id === t.plate_id);
                html += `<div class="bar ${index % 2 ? 'purple' : ''}" style="left:${t.started / duration * 100}%;width:${(t.ended - t.started) / duration * 100}%" title="${esc(t.step_id)} · ${t.started}–${t.ended}s"></div>`;
            }
            html += '</div></div>';
        }
        html += '</div><details class="preview-route"><summary>查看生成的转运与执行步骤</summary><div class="table-scroll"><table><thead><tr><th>样品板</th><th>动作</th><th>设备</th><th>位置</th><th>预计区间</th></tr></thead><tbody>';
        html += [...timeline].sort((a, b) => a.started - b.started).map(t => {
            const s = definitions[t.step_id];
            return `<tr><td>${esc(t.plate_id)}</td><td>${esc(s.kind === 'transport' ? '转运' : labelFor(s.capability))}</td><td>${esc(names[t.device_id] || t.device_id)}</td><td>${esc(s.kind === 'transport' ? s.source + ' → ' + s.target : s.location)}</td><td>${t.started}–${t.ended}s</td></tr>`;
        }).join('') + '</tbody></table></div></details>';
        $('recipe-preview').innerHTML = html;
    }
    async function save(asNew) {
        const data = clone(draft);
        let expected = baseVersion;
        if (asNew || data.id.startsWith('builtin_')) { data.id = freshId('recipe'); data.version = 1; expected = 0; }
        const saved = await api('/api/v1/recipes', { recipe: data, actor: actor(), expected_version: expected });
        adopt(saved.recipe, saved.recipe.version, false);
        await reloadTemplates(); $('recipe-template').value = recordKey(saved);
        note(`流程“${saved.recipe.name}”已保存为 v${saved.recipe.version}。`);
    }
    function runSummary() {
        const key = $('run-template').value;
        const data = key === '__draft' ? draft : records.find(r => recordKey(r) === key)?.recipe;
        if (!data) { runSnapshot = null; $('new-summary').textContent = '请选择流程模板。'; return; }
        runSnapshot = clone(data);
        if (key === '__draft' && dirty) { runSnapshot.id = freshId('draft'); runSnapshot.version = 1; }
        $('new-summary').textContent = `${data.name} · ${data.plates.length} 块板 · ${data.execution_mode === 'parallel' ? '交错执行' : '逐板执行'}：${data.operations.map(o => labelFor(o.capability)).join(' → ') || '尚未添加步骤'}`;
    }
    function prepareRun(preferred) {
        const previous = preferred || $('run-template').value;
        $('run-template').innerHTML = records.map(r => option(recordKey(r), `${r.recipe.name} · v${r.recipe.version}`, previous)).join('') + option('__draft', '当前编排内容' + (dirty ? '（未保存）' : ''), previous);
        runSummary();
    }
    async function init(bootstrap) {
        cell = bootstrap.workcell; catalog = bootstrap.operation_catalog;
        await reloadTemplates();
        draft = clone(bootstrap.default_recipe);
        try {
            const saved = JSON.parse(localStorage.getItem(storageKey));
            if (saved?.draft && typeof saved.draft.name === 'string' && typeof saved.draft.id === 'string' &&
                Array.isArray(saved.draft.operations) && saved.draft.operations.every(o => o && typeof o.capability === 'string') &&
                Array.isArray(saved.draft.plates) && saved.draft.plates.every(p => p && typeof p.id === 'string')) {
                draft = saved.draft; baseVersion = saved.baseVersion || 0; dirty = !!saved.dirty;
            }
        } catch { /* A corrupt browser draft does not prevent loading saved templates. */ }
        $('operation-library').innerHTML = catalog.map(c => `<button data-add-capability="${esc(c.capability)}">${esc(c.label)}</button>`).join('');
        renderEditor();
        $('recipe-name').oninput = () => { draft.name = $('recipe-name').value; changed(); };
        $('execution-mode').onchange = () => { draft.execution_mode = $('execution-mode').value; changed(); };
        $('operation-library').onclick = event => {
            const button = event.target.closest('[data-add-capability]'); if (!button) return;
            if (draft.operations.length >= 50) return note('每个流程最多 50 个实验步骤。', true);
            const c = catalog.find(c => c.capability === button.dataset.addCapability);
            draft.operations.push({ id: freshId('step'), capability: c.capability, method_id: c.method_id,
                estimated_duration_s: c.estimated_duration_s, timeout_s: c.timeout_s });
            changed(); renderSteps();
        };
        $('recipe-steps').onclick = event => {
            const button = event.target.closest('[data-op-action]'); if (!button) return;
            const i = Number(button.closest('[data-op-index]').dataset.opIndex), action = button.dataset.opAction;
            if (action === 'remove') draft.operations.splice(i, 1);
            if (action === 'duplicate') {
                if (draft.operations.length >= 50) return note('每个流程最多 50 个实验步骤。', true);
                draft.operations.splice(i + 1, 0, { ...clone(draft.operations[i]), id: freshId('step') });
            }
            if (action === 'up' || action === 'down') {
                const target = i + (action === 'up' ? -1 : 1);
                if (target < 0 || target >= draft.operations.length) return;
                [draft.operations[i], draft.operations[target]] = [draft.operations[target], draft.operations[i]];
            }
            changed(); renderSteps();
        };
        $('recipe-steps').oninput = event => {
            const field = event.target.dataset.opField; if (!field) return;
            const op = draft.operations[Number(event.target.closest('[data-op-index]').dataset.opIndex)];
            op[field] = ['estimated_duration_s', 'timeout_s'].includes(field) ? Number(event.target.value) : event.target.value;
            if (field === 'device_id') { op.location = ''; changed(); renderSteps(); } else changed();
        };
        $('recipe-plates').oninput = event => {
            const field = event.target.dataset.plateField; if (!field) return;
            draft.plates[Number(event.target.closest('[data-plate-index]').dataset.plateIndex)][field] = event.target.value; changed();
        };
        $('recipe-plates').onclick = event => {
            const button = event.target.closest('[data-remove-plate]'); if (!button || draft.plates.length <= 1) return;
            draft.plates.splice(Number(button.closest('[data-plate-index]').dataset.plateIndex), 1); changed(); renderPlates();
        };
        $('add-plate').onclick = () => {
            if (draft.plates.length >= 8) return;
            const available = (field, kind) => cell.locations.find(p => !draft.plates.some(d => d[field] === p.id) &&
                cell.devices.find(d => d.id === p.device_id)?.kind === kind && cell.transport.reachable_locations.includes(p.id));
            const start = available('initial_location', 'stack'), end = available('final_location', 'fridge');
            if (!start || !end) return note('没有空闲的默认堆栈或冰箱板位。请在工作站配置中增加板位后再添加样品板。', true);
            const id = freshId('plate'); draft.plates.push({ id, barcode: id.toUpperCase(), type: start.plate_types[0], initial_location: start.id, final_location: end.id });
            changed(); renderPlates();
        };
        $('load-recipe').onclick = () => task(async () => {
            const key = $('recipe-template').value; await reloadTemplates();
            const selected = records.find(r => recordKey(r) === key); if (!selected) throw new Error('未找到模板，请刷新后重试。');
            $('recipe-template').value = key; adopt(selected.recipe, selected.builtin ? 0 : selected.recipe.version, false);
        });
        $('blank-recipe').onclick = () => adopt({ ...clone(bootstrap.default_recipe), id: freshId('recipe'), name: '新实验流程', version: 1, execution_mode: 'serial', operations: [] });
        $('preview-recipe').onclick = () => task(async () => {
            $('preview-status').textContent = '正在预演…'; $('recipe-preview').replaceChildren();
            renderPreview(await api('/api/v1/recipes/preview', { recipe: clone(draft) }));
        });
        $('save-recipe').onclick = () => task(() => save(false));
        $('save-recipe-as').onclick = () => task(() => save(true));
        $('run-recipe').onclick = () => {
            if ($('new-run').disabled) return note('当前批次结束并完成资源核实后，才能创建下一批次。', true);
            newKey = crypto.randomUUID(); prepareRun('__draft'); openDialog('new-dialog');
        };
        $('run-template').onchange = () => { newKey = crypto.randomUUID(); runSummary(); };
        $('scenario').onchange = () => { newKey = crypto.randomUUID(); };
        $('export-recipe').onclick = () => {
            const url = URL.createObjectURL(new Blob([JSON.stringify(draft, null, 2)], { type: 'application/json' }));
            const link = document.createElement('a'); link.href = url; link.download = `${draft.id}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 5000);
        };
        $('import-recipe').onclick = () => $('recipe-file').click();
        $('recipe-file').onchange = () => task(async () => {
            const file = $('recipe-file').files[0]; if (!file) return;
            try {
                if (file.size > 1024 * 1024) throw new Error('流程文件不能超过 1 MiB。');
                const data = JSON.parse(await file.text());
                const report = await api('/api/v1/recipes/preview', { recipe: data });
                data.id = freshId('recipe'); data.version = 1; adopt(data); renderPreview(report);
                note('流程已导入为新草稿，可以修改或保存为模板。');
            } finally { $('recipe-file').value = ''; }
        });
    }
    return { init, prepareRun, forRun: () => { if (!runSnapshot) throw new Error('请选择流程模板。'); return clone(runSnapshot); } };
})();
