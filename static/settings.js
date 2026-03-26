// ─── Settings (dynamic, DB-backed) ────────────────────────────────────────────

let _settingsData = null;

function loadSettings() {
    fetch('/api/settings')
        .then(async r => {
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
            return data;
        })
        .then(data => {
            _settingsData = data;
            renderAllSettings(data);
        })
        .catch((err) => {
            document.getElementById('settingsContainer').innerHTML =
                `<div class="page-card"><p style="color:var(--danger)">Failed to load settings: ${escHtml(err.message || 'Unknown error')}.</p></div>`;
        });
}

function renderAllSettings(data) {
    const container = document.getElementById('settingsContainer');
    container.innerHTML = '';
    for (const [catKey, category] of Object.entries(data.categories)) {
        container.appendChild(renderCategoryCard(catKey, category));
    }
}

function renderCategoryCard(catKey, category) {
    const card = document.createElement('div');
    card.className = 'page-card';
    card.dataset.category = catKey;

    const header = document.createElement('div');
    header.className = 'page-card-header';
    header.innerHTML = `<span class="page-card-title">${escHtml(category.label)}</span>`;
    card.appendChild(header);

    const grid = document.createElement('div');
    grid.className = 'settings-grid';

    for (const setting of category.settings) {
        grid.appendChild(renderField(setting));
    }
    card.appendChild(grid);

    const actions = document.createElement('div');
    actions.className = 'settings-actions';
    const saveBtn = document.createElement('button');
    saveBtn.className = 'settings-btn';
    saveBtn.textContent = 'Save';
    saveBtn.onclick = () => saveCategory(catKey, saveBtn, statusEl);
    const statusEl = document.createElement('span');
    statusEl.className = 'settings-status';
    actions.appendChild(saveBtn);
    actions.appendChild(statusEl);
    card.appendChild(actions);

    return card;
}

function renderField(setting) {
    const wrap = document.createElement('div');
    wrap.className = 'form-group';
    wrap.dataset.key = setting.key;

    const labelRow = document.createElement('div');
    labelRow.style.cssText = 'display:flex;align-items:center;gap:0.5rem;margin-bottom:0.6rem;';

    const label = document.createElement('label');
    label.className = 'form-label';
    label.style.margin = '0';
    label.htmlFor = `sf_${setting.key}`;
    label.textContent = setting.key;
    labelRow.appendChild(label);

    const resetBtn = document.createElement('button');
    resetBtn.className = 'field-reset-link';
    resetBtn.textContent = 'reset';
    resetBtn.title = `Reset to default: ${setting.default}`;
    resetBtn.onclick = () => resetSetting(setting.key);
    labelRow.appendChild(resetBtn);
    wrap.appendChild(labelRow);

    let input;
    if (setting.type === 'bool') {
        const inline = document.createElement('div');
        inline.className = 'settings-inline';
        input = document.createElement('input');
        input.type = 'checkbox';
        input.id = `sf_${setting.key}`;
        input.checked = !!setting.value;
        const lbl = document.createElement('label');
        lbl.htmlFor = `sf_${setting.key}`;
        lbl.style.cssText = 'font-size:0.9rem;font-weight:500;';
        lbl.textContent = setting.description;
        inline.appendChild(input);
        inline.appendChild(lbl);
        wrap.appendChild(inline);
        wrap.appendChild(buildDefaultHint(setting.default));
    } else if (setting.type === 'tiers') {
        input = document.createElement('textarea');
        input.id = `sf_${setting.key}`;
        input.className = 'form-input';
        input.rows = 2;
        input.style.resize = 'vertical';
        input.value = setting.value || '';

        const descEl = document.createElement('div');
        descEl.className = 'field-description';
        descEl.textContent = setting.description;
        wrap.appendChild(input);
        wrap.appendChild(descEl);
        wrap.appendChild(buildDefaultHint(setting.default));
        return wrap;
    } else {
        input = document.createElement('input');
        input.id = `sf_${setting.key}`;
        input.className = 'form-input';
        input.type = setting.type === 'int' ? 'number' : 'text';
        input.value = setting.value !== null && setting.value !== undefined ? String(setting.value) : '';
    }

    if (setting.type !== 'bool') {
        wrap.appendChild(input);
    }

    if (setting.type !== 'bool') {
        const descEl = document.createElement('div');
        descEl.className = 'field-description';
        descEl.textContent = setting.description;
        wrap.appendChild(descEl);
        wrap.appendChild(buildDefaultHint(setting.default));
    }

    return wrap;
}

function buildDefaultHint(defaultValue) {
    const hintEl = document.createElement('div');
    hintEl.className = 'field-description';
    hintEl.style.color = 'var(--text-muted)';
    hintEl.textContent = `Default: ${defaultValue}`;
    return hintEl;
}

function collectCategoryValues(catKey) {
    const card = document.querySelector(`.page-card[data-category="${catKey}"]`);
    if (!card) return {};
    const result = {};
    const cat = _settingsData?.categories?.[catKey];
    if (!cat) return {};
    for (const setting of cat.settings) {
        const el = document.getElementById(`sf_${setting.key}`);
        if (!el) continue;
        if (setting.type === 'bool') {
            result[setting.key] = el.checked;
        } else if (setting.type === 'int') {
            result[setting.key] = parseInt(el.value, 10);
        } else {
            result[setting.key] = el.value;
        }
    }
    return result;
}

function saveCategory(catKey, btn, statusEl) {
    const values = collectCategoryValues(catKey);
    btn.disabled = true;
    statusEl.textContent = 'Saving…';
    statusEl.className = 'settings-status';
    fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: values }),
    }).then(async r => {
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Save failed');
        _settingsData = d.settings;
        statusEl.textContent = 'Saved.';
        statusEl.className = 'settings-status ok';
    }).catch(e => {
        statusEl.textContent = e.message;
        statusEl.className = 'settings-status error';
    }).finally(() => {
        btn.disabled = false;
    });
}

function resetSetting(key) {
    if (!confirm(`Reset "${key}" to its default value?`)) return;
    fetch('/api/settings/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key }),
    }).then(async r => {
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Reset failed');
        _settingsData = d.settings;
        renderAllSettings(d.settings);
    }).catch(e => alert(`Reset failed: ${e.message}`));
}

// ─── Bot Management ────────────────────────────────────────────────────────────

function loadBots() {
    fetch('/api/bots')
        .then(r => r.json())
        .then(data => renderBotList(data.bots))
        .catch(() => {
            document.getElementById('botListContainer').innerHTML =
                '<div class="bot-empty" style="color:var(--danger)">Failed to load bots.</div>';
        });
}

function renderBotList(bots) {
    const container = document.getElementById('botListContainer');
    if (!bots || bots.length === 0) {
        container.innerHTML = '<div class="bot-empty">No bots configured. Add one below.</div>';
        return;
    }

    const table = document.createElement('table');
    table.className = 'bot-table';
    table.innerHTML = `<thead><tr>
        <th>#</th>
        <th>Token</th>
        <th>Channel ID</th>
        <th>Status</th>
        <th>Actions</th>
    </tr></thead>`;

    const tbody = document.createElement('tbody');
    for (const bot of bots) {
        tbody.appendChild(renderBotRow(bot));
    }
    table.appendChild(tbody);
    container.innerHTML = '';
    container.appendChild(table);
}

function renderBotRow(bot) {
    const tr = document.createElement('tr');
    tr.id = `bot-row-${bot.index}`;
    tr.innerHTML = `
        <td>${bot.index}</td>
        <td><code>${escHtml(bot.token_masked)}</code>${bot.source === 'env' ? ' <span style="font-size:0.75rem;color:var(--text-muted)">(env)</span>' : ''}</td>
        <td><code>${bot.channel_id}</code></td>
        <td><span class="bot-status-dot" id="bot-dot-${bot.index}"></span><span id="bot-status-${bot.index}" style="font-size:0.85rem;color:var(--text-muted)">—</span></td>
        <td class="bot-actions">
            <button class="action-btn" onclick="checkBotHealth(${bot.index})">Check</button>
            ${bot.source === 'db' && bot.db_id != null
                ? `<button class="action-btn" style="color:var(--danger)" onclick="deleteBot(${bot.db_id})">Delete</button>`
                : `<button class="action-btn" disabled title="Remove from .env to delete">Delete</button>`
            }
        </td>
    `;
    return tr;
}

function updateBotStatusUI(index, result) {
    const dot = document.getElementById(`bot-dot-${index}`);
    const label = document.getElementById(`bot-status-${index}`);
    if (!dot || !label) return;
    dot.className = 'bot-status-dot ' + (result.ok ? 'ok' : 'error');
    label.textContent = result.ok ? 'OK' : (result.error || 'Error');
    label.style.color = result.ok ? 'var(--success)' : 'var(--danger)';
}

function checkBotHealth(index) {
    const dot = document.getElementById(`bot-dot-${index}`);
    const label = document.getElementById(`bot-status-${index}`);
    if (dot) dot.className = 'bot-status-dot checking';
    if (label) { label.textContent = 'Checking…'; label.style.color = 'var(--text-muted)'; }

    fetch('/api/bots/health', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ index }),
    }).then(async r => {
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Health check failed');
        const result = d.results?.[0];
        if (result) updateBotStatusUI(index, result);
    }).catch(e => {
        if (dot) dot.className = 'bot-status-dot error';
        if (label) { label.textContent = e.message; label.style.color = 'var(--danger)'; }
    });
}

function checkAllBotHealth() {
    const statusEl = document.getElementById('botHealthStatus');
    statusEl.textContent = 'Checking all…';
    statusEl.className = 'settings-status';

    // Set all dots to checking state
    document.querySelectorAll('.bot-status-dot').forEach(d => d.className = 'bot-status-dot checking');

    fetch('/api/bots/health', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
    }).then(async r => {
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Health check failed');
        for (const result of (d.results || [])) {
            updateBotStatusUI(result.index, result);
        }
        const ok = d.results?.every(r => r.ok);
        statusEl.textContent = ok ? 'All bots OK' : 'Some bots have errors';
        statusEl.className = 'settings-status ' + (ok ? 'ok' : 'error');
    }).catch(e => {
        statusEl.textContent = e.message;
        statusEl.className = 'settings-status error';
        document.querySelectorAll('.bot-status-dot').forEach(d => d.className = 'bot-status-dot error');
    });
}

function deleteBot(dbId) {
    if (!confirm('Remove this bot? It will no longer be used for uploads.')) return;
    fetch(`/api/bots/${dbId}`, { method: 'DELETE' })
        .then(async r => {
            const d = await r.json();
            if (!r.ok) throw new Error(d.error || 'Delete failed');
            loadBots();
        })
        .catch(e => alert(`Delete failed: ${e.message}`));
}

// ─── Add Bot Modal ─────────────────────────────────────────────────────────────

function openAddBotModal() {
    document.getElementById('newBotToken').value = '';
    document.getElementById('newBotChannelId').value = '';
    document.getElementById('newBotLabel').value = '';
    document.getElementById('addBotStatus').textContent = '';
    document.getElementById('addBotStatus').className = 'settings-status';
    document.getElementById('addBotModal').classList.add('active');
}

function closeAddBotModal() {
    document.getElementById('addBotModal').classList.remove('active');
}

function handleModalOverlayClick(e) {
    if (e.target === document.getElementById('addBotModal')) closeAddBotModal();
}

function testAndSaveBot() {
    const token = document.getElementById('newBotToken').value.trim();
    const channelId = document.getElementById('newBotChannelId').value.trim();
    const label = document.getElementById('newBotLabel').value.trim();
    const statusEl = document.getElementById('addBotStatus');
    const btn = document.getElementById('addBotSaveBtn');

    if (!token || !channelId) {
        statusEl.textContent = 'Token and Channel ID are required.';
        statusEl.className = 'settings-status error';
        return;
    }

    btn.disabled = true;
    statusEl.textContent = 'Testing…';
    statusEl.className = 'settings-status';

    fetch('/api/bots/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, channel_id: channelId, label }),
    }).then(async r => {
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Failed to add bot');
        statusEl.textContent = 'Bot added successfully!';
        statusEl.className = 'settings-status ok';
        setTimeout(() => {
            closeAddBotModal();
            loadBots();
        }, 800);
    }).catch(e => {
        statusEl.textContent = e.message;
        statusEl.className = 'settings-status error';
    }).finally(() => {
        btn.disabled = false;
    });
}

// ─── Watch Settings ────────────────────────────────────────────────────────────

function setWatchSettingsStatus(msg, kind='') {
    const el = document.getElementById('watchSettingsStatus');
    el.textContent = msg;
    el.className = `settings-status${kind ? ' '+kind : ''}`;
}

function applyWatchSettings(data) {
    document.getElementById('watchEnabled').checked = !!data.watch_enabled;
    document.getElementById('watchRoot').value = data.watch_root || '';
    document.getElementById('watchDoneDir').value = data.watch_done_dir || '';
    if (data.watch_enabled) {
        setWatchSettingsStatus(`Saved. ${data.watch_running ? 'Watcher active.' : 'Watcher pending.'}`, 'ok');
    } else { setWatchSettingsStatus('Watcher disabled.'); }
}

function loadWatchSettings() {
    fetch('/api/watch-settings').then(r=>r.json()).then(applyWatchSettings)
        .catch(()=>setWatchSettingsStatus('Could not load settings.','error'));
}

function saveWatchSettings() {
    const btn = document.getElementById('saveWatchSettingsBtn');
    btn.disabled = true;
    setWatchSettingsStatus('Saving…','');
    fetch('/api/watch-settings', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
            watch_enabled: document.getElementById('watchEnabled').checked,
            watch_root: document.getElementById('watchRoot').value.trim(),
            watch_done_dir: document.getElementById('watchDoneDir').value.trim(),
        }),
    }).then(async r => {
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Could not save.');
        applyWatchSettings(d);
    }).catch(e=>setWatchSettingsStatus(e.message,'error')).finally(()=>{ btn.disabled=false; });
}

// ─── Database Load ─────────────────────────────────────────────────────────────

function loadDatabaseFromFile() {
    const fileInput = document.getElementById('databaseFileInput');
    const statusEl = document.getElementById('databaseLoadStatus');
    const btn = document.getElementById('databaseLoadBtn');
    const file = fileInput?.files?.[0];

    if (!file) {
        statusEl.textContent = 'Choose a database file first.';
        statusEl.className = 'settings-status error';
        return;
    }

    const formData = new FormData();
    formData.append('database', file);
    btn.disabled = true;
    statusEl.textContent = 'Loading database…';
    statusEl.className = 'settings-status';

    fetch('/api/database/load', {
        method: 'POST',
        body: formData,
    }).then(async (r) => {
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || 'Failed to load database');
        statusEl.textContent = `Loaded. Backup saved at ${data.backup_path || 'server backup path unavailable'}.`;
        statusEl.className = 'settings-status ok';
        loadSettings();
        loadBots();
    }).catch((err) => {
        statusEl.textContent = err.message || 'Failed to load database';
        statusEl.className = 'settings-status error';
    }).finally(() => {
        btn.disabled = false;
    });
}

// ─── Utilities ─────────────────────────────────────────────────────────────────

function escHtml(str) {
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ─── Init ──────────────────────────────────────────────────────────────────────

loadSettings();
loadBots();
loadWatchSettings();
