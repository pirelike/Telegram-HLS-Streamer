// ─── State ────────────────────────────────────────────────────────────────────
let shakaPlayer = null;
let shakaUi = null;
let currentJob = null;
let attemptedQuotaRecovery = false;

// ─── Player init ──────────────────────────────────────────────────────────────
async function initPlayer(job) {
    currentJob = job;
    attemptedQuotaRecovery = false;
    const videoEl = document.getElementById('videoEl');
    const m3u8Url = `${window.location.origin}/hls/${job.job_id}/master.m3u8`;

    if (shakaUi) { shakaUi.destroy(); shakaUi = null; }
    if (shakaPlayer) { await shakaPlayer.destroy(); shakaPlayer = null; }

    shaka.polyfill.installAll();
    if (!shaka.Player.isBrowserSupported()) {
        document.getElementById('playerInfo').innerHTML =
            `<p style="color:var(--danger)">Your browser does not support Shaka Player. Use the M3U8 URL directly.</p>`;
    } else {
        const container = document.getElementById('playerContainer');
        const player = new shaka.Player();
        await player.attach(videoEl);
        shakaPlayer = player;

        shakaUi = new shaka.ui.Overlay(player, container, videoEl);
        shakaUi.configure({
            addSeekBar: true,
            addBigPlayButton: true,
            controlPanelElements: [
                'play_pause',
                'mute',
                'volume',
                'spacer',
                'time_and_duration',
                'overflow_menu',
                'fullscreen',
            ],
            overflowMenuButtons: [
                'quality',
                'language',
                'captions',
                'playback_rate',
                'picture_in_picture',
            ],
            seekBarColors: {
                base: 'rgba(255,255,255,0.3)',
                buffered: 'rgba(255,255,255,0.54)',
                played: 'rgb(255,255,255)',
            },
        });

        player.configure({
            streaming: {
                bufferingGoal: 15,
                rebufferingGoal: 2,
                bufferBehind: 20,
            },
            abr: {
                defaultBandwidthEstimate: 10_000_000,
            },
            preferredAudioLanguage: 'und',
            preferredTextLanguage: '',
        });

        player.addEventListener('error', async e => {
            console.error('Shaka error', e.detail);
            const code = e?.detail?.code;
            const message = e?.detail?.message || String(code);

            if (code === 3017 && !attemptedQuotaRecovery) {
                attemptedQuotaRecovery = true;
                document.getElementById('playerInfo').insertAdjacentHTML('afterbegin',
                    `<p style="color:var(--warning);margin-bottom:0.5rem">Playback buffer was full (Shaka 3017). Retrying with a smaller buffer…</p>`);
                player.configure({
                    streaming: {
                        bufferingGoal: 6,
                        rebufferingGoal: 1,
                        bufferBehind: 6,
                    },
                });
                if (typeof player.retryStreaming === 'function') {
                    try { await player.retryStreaming(); return; }
                    catch (retryErr) { console.error('Shaka retryStreaming failed', retryErr); }
                }
            }

            document.getElementById('playerInfo').insertAdjacentHTML('afterbegin',
                `<p style="color:var(--danger);margin-bottom:0.5rem">Playback error: ${escapeHtml(message)}</p>`);
            if (code === 3017) {
                document.getElementById('playerInfo').insertAdjacentHTML('afterbegin',
                    `<p style="color:var(--text-muted);margin-bottom:0.5rem">Tip: this usually means one or more HLS segments are too large for browser MSE memory. Re-process this video with smaller segments or disable copy mode.</p>`);
            }
        });

        renderInfoPanel(job);

        try { await player.load(m3u8Url); }
        catch (e) {
            console.error('Shaka load error', e);
            document.getElementById('playerInfo').insertAdjacentHTML('afterbegin',
                `<p style="color:var(--danger);margin-bottom:0.5rem">Failed to load stream: ${escapeHtml(e.message || String(e))}</p>`);
        }
    }
}

function renderInfoPanel(job) {
    const m3u8Url = `${window.location.origin}/hls/${job.job_id}/master.m3u8`;
    const audioCount = job.audio_count || 0;
    const subCount = job.subtitle_count || 0;
    const metaParts = [];
    if (job.media_type) metaParts.push(escapeHtml(job.media_type));
    if (job.series_name) metaParts.push(escapeHtml(job.series_name));
    if (job.duration > 0) metaParts.push(formatDuration(job.duration));
    if (audioCount > 0) metaParts.push(`${audioCount} audio track${audioCount !== 1 ? 's' : ''}`);
    if (subCount > 0) metaParts.push(`${subCount} subtitle${subCount !== 1 ? 's' : ''}`);

    document.getElementById('playerInfo').innerHTML = `
        <div class="player-title">${escapeHtml(cleanTitle(job.filename || job.job_id))}</div>
        <div class="player-meta">${metaParts.join(' &bull; ')}</div>
        <div class="player-m3u8">
            <div class="url-box">
                <span class="url-text" id="playerM3u8Url">${escapeHtml(m3u8Url)}</span>
                <button class="copy-btn" onclick="copyPlayerUrl()">Copy M3U8</button>
            </div>
            <div class="player-actions" style="margin-top: 1rem; display: flex; gap: 0.5rem;">
                <button class="action-btn" onclick="openEditModal('${escapeAttr(job.job_id)}')">
                    <i class="material-icons-round" style="font-size:1.1rem;vertical-align:middle;margin-right:0.2rem;">edit</i> Edit Metadata
                </button>
                <button class="action-btn danger" onclick="deleteJob('${escapeAttr(job.job_id)}')">
                    <i class="material-icons-round" style="font-size:1.1rem;vertical-align:middle;margin-right:0.2rem;">delete</i> Delete Video
                </button>
            </div>
        </div>`;
}

function copyPlayerUrl() {
    const el = document.getElementById('playerM3u8Url');
    if (el) navigator.clipboard.writeText(el.textContent).then(() => {
        const btn = el.nextElementSibling;
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = 'Copy M3U8', 2000);
    });
}

// ─── Edit Metadata & Delete ───────────────────────────────────────────────────
async function deleteJob(jobId) {
    if (!confirm('Are you sure you want to delete this video? This cannot be undone.')) return;
    try {
        const resp = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
        if (!resp.ok) {
            const data = await resp.json();
            throw new Error(data.error || 'Failed to delete');
        }
        window.location.href = '/';
    } catch (e) {
        alert(e.message);
    }
}

function closeEditModal() {
    document.getElementById('editModal').classList.remove('active');
}

function updateEditModalFields() {
    const cat = document.getElementById('editCategory').value;
    const seriesGrp = document.getElementById('editSeriesGroup');
    const seasonGrp = document.getElementById('editSeasonGroup');
    const epGrp = document.getElementById('editEpisodeGroup');
    const partGrp = document.getElementById('editPartGroup');

    seriesGrp.style.display = 'none';
    seasonGrp.style.display = 'none';
    epGrp.style.display = 'none';
    partGrp.style.display = 'none';

    if (cat === 'Film Series') {
        seriesGrp.style.display = 'block';
        partGrp.style.display = 'block';
    } else if (['TV Series', 'Anime TV', 'Anime TV Series'].includes(cat)) {
        seriesGrp.style.display = 'block';
        seasonGrp.style.display = 'block';
        epGrp.style.display = 'block';
    }
}

function getCategoryFromJob(job) {
    if (job.media_type === 'Film') return job.is_series ? 'Film Series' : 'Film';
    if (job.media_type === 'Series') return 'TV Series';
    if (job.media_type === 'Anime Film') return 'Anime Film';
    if (job.media_type === 'Anime TV') return job.is_series ? 'Anime TV Series' : 'Anime TV';
    return 'Film';
}

function openEditModal(jobId) {
    if (!currentJob || currentJob.job_id !== jobId) return;

    document.getElementById('editJobId').value = currentJob.job_id;
    document.getElementById('editTitle').value = cleanTitle(currentJob.filename || currentJob.job_id);
    document.getElementById('editCategory').value = getCategoryFromJob(currentJob);
    document.getElementById('editSeriesName').value = currentJob.series_name || '';
    document.getElementById('editSeasonNumber').value = currentJob.season_number != null ? currentJob.season_number : '';
    document.getElementById('editEpisodeNumber').value = currentJob.episode_number != null ? currentJob.episode_number : '';
    document.getElementById('editPartNumber').value = currentJob.part_number != null ? currentJob.part_number : '';

    updateEditModalFields();
    document.getElementById('editModal').classList.add('active');
}

async function saveEditModal() {
    const jobId = document.getElementById('editJobId').value;
    const cat = document.getElementById('editCategory').value;
    const btn = document.getElementById('saveEditBtn');

    const dbFields = CATEGORY_DB[cat];
    const payload = {
        title: document.getElementById('editTitle').value.trim(),
        media_type: dbFields.media_type,
        is_series: dbFields.is_series,
        series_name: document.getElementById('editSeriesName').value.trim()
    };

    if (cat === 'Film Series') {
        payload.part_number = document.getElementById('editPartNumber').value;
    } else if (['TV Series', 'Anime TV', 'Anime TV Series'].includes(cat)) {
        payload.season_number = document.getElementById('editSeasonNumber').value;
        payload.episode_number = document.getElementById('editEpisodeNumber').value;
    }

    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        const resp = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!resp.ok) {
            const data = await resp.json();
            throw new Error(data.error || 'Failed to save');
        }

        // Update currentJob in-place
        Object.assign(currentJob, payload);
        if (payload.title) currentJob.filename = payload.title;
        if (payload.part_number !== undefined) currentJob.part_number = payload.part_number ? parseInt(payload.part_number) : null;
        if (payload.season_number !== undefined) currentJob.season_number = payload.season_number ? parseInt(payload.season_number) : null;
        if (payload.episode_number !== undefined) currentJob.episode_number = payload.episode_number ? parseInt(payload.episode_number) : null;
        if (!['Film Series'].includes(cat)) currentJob.part_number = null;
        if (!['TV Series', 'Anime TV', 'Anime TV Series'].includes(cat)) {
            currentJob.season_number = null;
            currentJob.episode_number = null;
        }
        if (['Film', 'Anime Film'].includes(cat)) currentJob.series_name = '';

        closeEditModal();
        renderInfoPanel(currentJob);
    } catch (e) {
        alert(e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save Changes';
    }
}

// ─── Init ─────────────────────────────────────────────────────────────────────
(async () => {
    // Extract job_id from URL path: /watch/<job_id>
    const jobId = window.location.pathname.split('/watch/')[1];
    if (!jobId) {
        document.getElementById('playerInfo').innerHTML =
            `<p style="color:var(--danger)">No job ID in URL.</p>`;
        return;
    }

    try {
        const resp = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
        if (!resp.ok) throw new Error('Job not found');
        const job = await resp.json();
        await initPlayer(job);
    } catch (e) {
        document.getElementById('playerInfo').innerHTML =
            `<p style="color:var(--danger)">Could not load video: ${escapeHtml(e.message)}</p>`;
    }
})();
