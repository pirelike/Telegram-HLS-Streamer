// ─── Constants ────────────────────────────────────────────────────────────────
const JOBS_PER_PAGE = 20;

// ─── State ────────────────────────────────────────────────────────────────────
let allJobs = [];
let activeCategory = 'all';
let searchQuery = '';
let jobsPage = 1;
let hasMoreJobs = false;
let navStack = [];

// ─── Navigation ──────────────────────────────────────────────────────────────
function resetNav(label) {
    navStack = [{type: 'root', label: label || 'Browse'}];
}

function pushNav(item) {
    navStack.push(item);
    loadJobs();
    window.scrollTo(0, 0);
}

function popNav(index) {
    if (index === undefined) navStack.pop();
    else navStack = navStack.slice(0, index + 1);
    loadJobs();
    window.scrollTo(0, 0);
}

function renderBreadcrumbs() {
    if (navStack.length <= 1) return '';
    return `<div class="breadcrumb">
        ${navStack.map((item, i) => {
            const isLast = i === navStack.length - 1;
            return `<span class="breadcrumb-item ${isLast ? 'active' : ''}" onclick="${isLast ? '' : `popNav(${i})`}">
                ${escapeHtml(item.label)}
            </span>${isLast ? '' : '<i class="material-icons-round">chevron_right</i>'}`;
        }).join('')}
    </div>`;
}

// ─── DOM refs ─────────────────────────────────────────────────────────────────
const searchInput = document.getElementById('searchInput');
const videosContainer = document.getElementById('videosContainer');
const loadMoreBtn = document.getElementById('loadMoreBtn');

// ─── Category ────────────────────────────────────────────────────────────────
function setCategory(cat) {
    activeCategory = cat;
    const labels = { all: 'Home', Film: 'Films', Series: 'Series', 'Anime Film': 'Anime Films', 'Anime TV': 'Anime TV' };
    resetNav(labels[cat] || cat);
    document.querySelectorAll('.sidebar-item[data-category]').forEach(el => {
        el.classList.toggle('active', el.dataset.category === cat);
    });
    loadJobs();
    if (window.innerWidth <= 1024) { sidebarOpen = false; updateSidebar(); }
}

// ─── Search ──────────────────────────────────────────────────────────────────
let searchTimeout = null;
searchInput.addEventListener('input', () => {
    searchQuery = searchInput.value.trim();
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
        resetNav(activeCategory === 'all' ? 'Search Results' : undefined);
        loadJobs();
    }, 400);
});

// ─── Job list ─────────────────────────────────────────────────────────────────
function loadJobs() {
    if (navStack.length === 0) resetNav();
    allJobs = [];
    videosContainer.innerHTML = `${renderBreadcrumbs()}<p class="no-results">Loading...</p>`;

    const url = new URL('/api/jobs', window.location.origin);
    url.searchParams.set('page', '1');
    url.searchParams.set('limit', JOBS_PER_PAGE);
    if (searchQuery) url.searchParams.set('search', searchQuery);
    if (activeCategory !== 'all') url.searchParams.set('category', activeCategory);

    const currentNav = navStack[navStack.length - 1];
    if (currentNav.type === 'root') {
        if (activeCategory === 'Series' || activeCategory === 'Anime TV') {
            url.searchParams.set('group_by', 'series');
        }
    } else if (currentNav.type === 'series') {
        url.searchParams.set('group_by', 'season');
        url.searchParams.set('series_name', currentNav.name);
    } else if (currentNav.type === 'season') {
        url.searchParams.set('series_name', currentNav.series);
        if (currentNav.season !== null) url.searchParams.set('season_number', currentNav.season);
    }

    fetch(url)
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
    const url = new URL('/api/jobs', window.location.origin);
    url.searchParams.set('page', jobsPage);
    url.searchParams.set('limit', JOBS_PER_PAGE);
    if (searchQuery) url.searchParams.set('search', searchQuery);
    if (activeCategory !== 'all') url.searchParams.set('category', activeCategory);

    const currentNav = navStack[navStack.length - 1];
    if (currentNav.type === 'root') {
        if (activeCategory === 'Series' || activeCategory === 'Anime TV') {
            url.searchParams.set('group_by', 'series');
        }
    } else if (currentNav.type === 'series') {
        url.searchParams.set('group_by', 'season');
        url.searchParams.set('series_name', currentNav.name);
    } else if (currentNav.type === 'season') {
        url.searchParams.set('series_name', currentNav.series);
        if (currentNav.season !== null) url.searchParams.set('season_number', currentNav.season);
    }

    fetch(url)
        .then(r => r.json())
        .then(data => {
            const newJobs = data.jobs || [];
            allJobs = allJobs.concat(newJobs);
            hasMoreJobs = !!data.has_more;
            loadMoreBtn.classList.toggle('visible', hasMoreJobs);
            renderJobs();
        }).catch(() => {});
}

function filteredJobs() {
    return allJobs;
}

function renderJobs() {
    const items = filteredJobs();
    const currentNav = navStack[navStack.length - 1];

    if (items.length === 0) {
        videosContainer.innerHTML = `${renderBreadcrumbs()}<div class="no-results">
            <i class="material-icons-round">video_library</i>
            <p>No items found</p>
        </div>`;
        return;
    }

    const headerLabels = { all: 'All Videos', Film: 'Films', Series: 'Series', 'Anime Film': 'Anime Films', 'Anime TV': 'Anime TV' };
    const headerLabel = headerLabels[activeCategory] || activeCategory;

    let contentHtml = '';
    let sectionTitle = headerLabel;

    if (currentNav.type === 'root') {
        if (activeCategory === 'Series' || activeCategory === 'Anime TV') {
            contentHtml = `<div class="video-grid posters">${items.map(j => renderCard(j, 'series', {label: j.series_name, count: j.episode_count})).join('')}</div>`;
        } else {
            contentHtml = `<div class="video-grid">${items.map(j => renderCard(j, 'video')).join('')}</div>`;
        }
    } else if (currentNav.type === 'series') {
        sectionTitle = currentNav.label;
        contentHtml = `<div class="video-grid posters">${items.map(j => renderCard(j, 'season', {series: currentNav.name, season: j.season_number, count: j.episode_count})).join('')}</div>`;
    } else if (currentNav.type === 'season') {
        sectionTitle = `Season ${currentNav.season}`;
        contentHtml = `<div class="video-grid episodes">${items.map(j => renderCard(j, 'video')).join('')}</div>`;
    }

    const sectionHeader = `<h2 class="section-header">${sectionTitle}</h2>`;
    videosContainer.innerHTML = renderBreadcrumbs() + sectionHeader + contentHtml;

    videosContainer.querySelectorAll('.video-card').forEach(card => {
        card.addEventListener('click', () => {
            const type = card.dataset.type;
            if (type === 'series') {
                pushNav({type: 'series', name: card.dataset.series, label: card.dataset.series});
            } else if (type === 'season') {
                pushNav({type: 'season', series: card.dataset.series, season: card.dataset.season === 'null' ? null : parseInt(card.dataset.season), label: `Season ${card.dataset.season}`});
            } else {
                window.location.href = `/watch/${card.dataset.jobId}`;
            }
        });
    });
}

function renderCard(j, type, extra = {}) {
    const safeId = escapeAttr(j.job_id);
    const thumbSrc = j.has_thumbnail ? `/thumbnail/${safeId}` : null;
    const gradient = jobIdToGradient(j.job_id);
    const thumbHtml = thumbSrc
        ? `<img class="thumb-img" src="${thumbSrc}" alt="" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
        : '';
    const placeholderStyle = thumbSrc ? 'display:none' : '';

    if (type === 'series') {
        const name = extra.label || 'Unknown Series';
        const count = extra.count || 0;
        return `<div class="video-card poster" data-type="series" data-series="${escapeAttr(name)}">
            <div class="thumb-wrap" style="background:${gradient}">
                ${thumbHtml}
                <div class="thumb-placeholder" style="${placeholderStyle}"><i class="material-icons-round">library_books</i></div>
                <div class="badge-count">${count}</div>
            </div>
            <div class="card-meta">
                <div class="card-title">${escapeHtml(name)}</div>
                <div class="card-subtitle"><span class="dot"></span>Series</div>
            </div>
        </div>`;
    }

    if (type === 'season') {
        const series = extra.series;
        const season = extra.season;
        const count = extra.count || 0;
        const seasonLabel = season === null ? 'Specials' : `Season ${season}`;
        return `<div class="video-card poster" data-type="season" data-series="${escapeAttr(series)}" data-season="${season}">
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
        </div>`;
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

    return `<div class="video-card" data-type="video" data-job-id="${safeId}">
        <div class="thumb-wrap" style="background:${gradient}">
            ${thumbHtml}
            <div class="thumb-placeholder" style="${placeholderStyle}"><i class="material-icons-round">play_circle_filled</i></div>
            ${dur ? `<div class="thumb-duration">${dur}</div>` : ''}
        </div>
        <div class="card-meta">
            <div class="card-title">${title}</div>
            <div class="card-subtitle"><span class="dot"></span>${subtitleHtml}</div>
        </div>
    </div>`;
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
const _urlCategory = new URLSearchParams(location.search).get('category');
if (_urlCategory) {
    setCategory(_urlCategory);
} else {
    resetNav('Home');
    loadJobs();
}
