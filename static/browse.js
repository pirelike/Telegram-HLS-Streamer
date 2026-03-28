// ─── Constants ────────────────────────────────────────────────────────────────
const JOBS_PER_PAGE = 20;

// ─── State ────────────────────────────────────────────────────────────────────
let allJobs = [];
let searchQuery = '';
let jobsPage = 1;
let hasMoreJobs = false;

// ─── DOM refs ─────────────────────────────────────────────────────────────────
const searchInput = document.getElementById('searchInput');
const videosContainer = document.getElementById('videosContainer');
const loadMoreBtn = document.getElementById('loadMoreBtn');

// ─── Search ──────────────────────────────────────────────────────────────────
let searchTimeout = null;
searchInput.addEventListener('input', () => {
    searchQuery = searchInput.value.trim();
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(loadJobs, 400);
});

// ─── Breadcrumbs ─────────────────────────────────────────────────────────────
function renderBreadcrumbs() {
    const crumbs = window.BROWSE_CTX.breadcrumbs || [];
    if (crumbs.length <= 1) return '';
    return `<div class="breadcrumb">
        ${crumbs.map((c, i) => {
            const isLast = i === crumbs.length - 1;
            return isLast
                ? `<span class="breadcrumb-item active">${escapeHtml(c.label)}</span>`
                : `<a class="breadcrumb-item" href="${escapeAttr(c.href || '/')}">${escapeHtml(c.label)}</a>
                   <i class="material-icons-round">chevron_right</i>`;
        }).join('')}
    </div>`;
}

// ─── Build API URL from BROWSE_CTX ───────────────────────────────────────────
function _buildApiUrl(page) {
    const ctx = window.BROWSE_CTX;
    const url = new URL('/api/jobs', window.location.origin);
    url.searchParams.set('page', page);
    url.searchParams.set('limit', JOBS_PER_PAGE);
    if (searchQuery) url.searchParams.set('search', searchQuery);
    if (ctx.category !== 'all') url.searchParams.set('category', ctx.category);

    if (ctx.view === 'series_list') {
        url.searchParams.set('group_by', 'series');
    } else if (ctx.view === 'seasons') {
        url.searchParams.set('group_by', 'season');
        url.searchParams.set('series_name', ctx.seriesName);
    } else if (ctx.view === 'episodes') {
        url.searchParams.set('series_name', ctx.seriesName);
        if (ctx.seasonNumber !== null) url.searchParams.set('season_number', ctx.seasonNumber);
    }
    return url;
}

// ─── Job list ─────────────────────────────────────────────────────────────────
function loadJobs() {
    allJobs = [];
    videosContainer.innerHTML = `${renderBreadcrumbs()}<p class="no-results">Loading...</p>`;

    fetch(_buildApiUrl(1))
        .then(r => r.json())
        .then(data => {
            jobsPage = 1;
            allJobs = data.jobs || [];
            hasMoreJobs = !!data.has_more;
            loadMoreBtn.classList.toggle('visible', hasMoreJobs);
            renderJobs();
        })
        .catch(() => { videosContainer.innerHTML = '<p class="no-results">Could not load items.</p>'; });
}

function loadMoreJobs() {
    jobsPage += 1;
    fetch(_buildApiUrl(jobsPage))
        .then(r => r.json())
        .then(data => {
            const newJobs = data.jobs || [];
            allJobs = allJobs.concat(newJobs);
            hasMoreJobs = !!data.has_more;
            loadMoreBtn.classList.toggle('visible', hasMoreJobs);
            renderJobs();
        }).catch(() => {});
}

function renderJobs() {
    const ctx = window.BROWSE_CTX;
    const items = allJobs;

    if (items.length === 0) {
        videosContainer.innerHTML = `${renderBreadcrumbs()}<div class="no-results">
            <i class="material-icons-round">video_library</i>
            <p>No items found</p>
        </div>`;
        return;
    }

    const headerLabels = { all: 'All Videos', Film: 'Films', Series: 'Series', 'Anime Film': 'Anime Films', 'Anime TV': 'Anime TV' };
    let sectionTitle = headerLabels[ctx.category] || ctx.category;
    let contentHtml = '';

    if (ctx.view === 'series_list') {
        contentHtml = `<div class="video-grid posters">${items.map(j => renderCard(j, 'series')).join('')}</div>`;
    } else if (ctx.view === 'seasons') {
        sectionTitle = ctx.seriesName || sectionTitle;
        contentHtml = `<div class="video-grid posters">${items.map(j => renderCard(j, 'season')).join('')}</div>`;
    } else {
        // 'grid' or 'episodes'
        const gridClass = ctx.view === 'episodes' ? 'video-grid episodes' : 'video-grid';
        contentHtml = `<div class="${gridClass}">${items.map(j => renderCard(j, 'video')).join('')}</div>`;
    }

    const sectionHeader = `<h2 class="section-header">${escapeHtml(sectionTitle)}</h2>`;
    videosContainer.innerHTML = renderBreadcrumbs() + sectionHeader + contentHtml;
}

function renderCard(j, type) {
    const ctx = window.BROWSE_CTX;
    const safeId = escapeAttr(j.job_id);
    const thumbSrc = j.has_thumbnail ? `/thumbnail/${safeId}` : null;
    const gradient = jobIdToGradient(j.job_id);
    const thumbHtml = thumbSrc
        ? `<img class="thumb-img" src="${thumbSrc}" alt="" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
        : '';
    const placeholderStyle = thumbSrc ? 'display:none' : '';

    if (type === 'series') {
        const name = j.series_name || 'Unknown Series';
        const count = j.episode_count || 0;
        const catPath = CATEGORY_PATHS[ctx.category] || '/';
        const href = escapeAttr(catPath + '/' + slugify(name));
        return `<a class="video-card poster" href="${href}">
            <div class="thumb-wrap" style="background:${gradient}">
                ${thumbHtml}
                <div class="thumb-placeholder" style="${placeholderStyle}"><i class="material-icons-round">library_books</i></div>
                <div class="badge-count">${count}</div>
            </div>
            <div class="card-meta">
                <div class="card-title">${escapeHtml(name)}</div>
                <div class="card-subtitle"><span class="dot"></span>Series</div>
            </div>
        </a>`;
    }

    if (type === 'season') {
        const season = j.season_number;
        const count = j.episode_count || 0;
        const seasonLabel = season === null ? 'Specials' : `Season ${season}`;
        const catPath = CATEGORY_PATHS[ctx.category] || '/';
        const seasonPath = season === null ? '/specials' : `/s${season}`;
        const href = escapeAttr(catPath + '/' + (ctx.seriesSlug || slugify(ctx.seriesName || '')) + seasonPath);
        return `<a class="video-card poster" href="${href}">
            <div class="thumb-wrap" style="background:${gradient}">
                ${thumbHtml}
                <div class="thumb-placeholder" style="${placeholderStyle}"><i class="material-icons-round">folder</i></div>
                <div class="season-overlay">
                    <div class="season-label">${season === null ? '' : 'Season'}</div>
                    <div class="season-num">${season === null ? 'SP' : season}</div>
                </div>
                <div class="badge-count">${count}</div>
            </div>
            <div class="card-meta">
                <div class="card-title">${seasonLabel}</div>
                <div class="card-subtitle"><span class="dot"></span>${count} Episode${count !== 1 ? 's' : ''}</div>
            </div>
        </a>`;
    }

    // Default video card
    const dur = formatDuration(j.duration);
    const title = escapeHtml(cleanTitle(j.filename || j.job_id));
    const subtitleParts = [];
    if (j.media_type) subtitleParts.push(escapeHtml(j.media_type));
    if (j.season_number != null && j.episode_number != null) {
        subtitleParts.push(`S${String(j.season_number).padStart(2,'0')}E${String(j.episode_number).padStart(2,'0')}`);
    } else if (j.episode_number != null) {
        subtitleParts.push(`Ep ${j.episode_number}`);
    } else if (j.part_number != null) {
        subtitleParts.push(`Part ${j.part_number}`);
    }
    if (j.video_height) subtitleParts.push(`${j.video_height}p`);
    const subtitleHtml = subtitleParts.map((p, i) =>
        i === 0 ? p : `<span class="sep">&bull;</span> ${p}`
    ).join(' ');

    return `<a class="video-card" href="/watch/${safeId}">
        <div class="thumb-wrap" style="background:${gradient}">
            ${thumbHtml}
            <div class="thumb-placeholder" style="${placeholderStyle}"><i class="material-icons-round">play_circle_filled</i></div>
            ${dur ? `<div class="thumb-duration">${dur}</div>` : ''}
        </div>
        <div class="card-meta">
            <div class="card-title">${title}</div>
            <div class="card-subtitle"><span class="dot"></span>${subtitleHtml}</div>
        </div>
    </a>`;
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
        allJobs = allJobs.filter(j => j.job_id !== jobId);
        renderJobs();
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
    const job = allJobs.find(j => j.job_id === jobId);
    if (!job) return;

    document.getElementById('editJobId').value = job.job_id;
    document.getElementById('editTitle').value = cleanTitle(job.filename || job.job_id);
    document.getElementById('editCategory').value = getCategoryFromJob(job);
    document.getElementById('editSeriesName').value = job.series_name || '';
    document.getElementById('editSeasonNumber').value = job.season_number != null ? job.season_number : '';
    document.getElementById('editEpisodeNumber').value = job.episode_number != null ? job.episode_number : '';
    document.getElementById('editPartNumber').value = job.part_number != null ? job.part_number : '';

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

        const job = allJobs.find(j => j.job_id === jobId);
        if (job) {
            Object.assign(job, payload);
            if (payload.title) job.filename = payload.title;
            if (payload.part_number !== undefined) job.part_number = payload.part_number ? parseInt(payload.part_number) : null;
            if (payload.season_number !== undefined) job.season_number = payload.season_number ? parseInt(payload.season_number) : null;
            if (payload.episode_number !== undefined) job.episode_number = payload.episode_number ? parseInt(payload.episode_number) : null;

            if (!['Film Series'].includes(cat)) job.part_number = null;
            if (!['TV Series', 'Anime TV', 'Anime TV Series'].includes(cat)) {
                job.season_number = null;
                job.episode_number = null;
            }
            if (['Film', 'Anime Film'].includes(cat)) job.series_name = '';
        }
        closeEditModal();
        renderJobs();
    } catch (e) {
        alert(e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save Changes';
    }
}

// ─── Init ─────────────────────────────────────────────────────────────────────
loadJobs();
