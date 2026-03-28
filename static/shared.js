// ─── Category → DB mapping (used by upload and edit modal) ───────────────────
const CATEGORY_DB = {
    'Film':           { media_type: 'Film',     is_series: 0 },
    'Film Series':    { media_type: 'Film',     is_series: 1 },
    'TV Series':      { media_type: 'Series',   is_series: 1 },
    'Anime Film':     { media_type: 'Anime Film', is_series: 0 },
    'Anime TV':       { media_type: 'Anime TV', is_series: 0 },
    'Anime TV Series':{ media_type: 'Anime TV', is_series: 1 },
};

// ─── Category path map (used by browse.js for URL construction) ───────────────
const CATEGORY_PATHS = {
    'all': '/', 'Film': '/films', 'Series': '/series',
    'Anime Film': '/anime-films', 'Anime TV': '/anime-tv',
};

// ─── Utilities ────────────────────────────────────────────────────────────────
function slugify(text) {
    return String(text || '').toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/-+/g, '-')
        .replace(/^-|-$/g, '');
}

function cleanTitle(filename) {
    if (!filename) return 'Untitled';
    let name = filename.replace(/^[0-9a-f]{16}_/i, '');
    name = name.replace(/\.[^.]+$/, '');
    name = name.replace(/[_.]/g, ' ');
    name = name.replace(/\s+/g, ' ').trim();
    return name;
}

function jobIdToGradient(jobId) {
    let hash = 0;
    const s = jobId || '';
    for (let i = 0; i < s.length; i++) {
        hash = Math.imul(31, hash) + s.charCodeAt(i) | 0;
    }
    const h1 = Math.abs(hash) % 360;
    const h2 = (h1 + 45) % 360;
    return `linear-gradient(145deg, hsl(${h1},40%,22%) 0%, hsl(${h2},50%,14%) 100%)`;
}

function escapeHtml(str) {
    const d = document.createElement('div');
    d.appendChild(document.createTextNode(String(str || '')));
    return d.innerHTML;
}

function escapeAttr(str) {
    return String(str || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
    if (bytes < 1073741824) return (bytes/1048576).toFixed(1) + ' MB';
    return (bytes/1073741824).toFixed(2) + ' GB';
}

function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '?';
    if (seconds < 60) return Math.round(seconds) + 's';
    if (seconds < 3600) return Math.round(seconds/60) + 'm ' + Math.round(seconds%60) + 's';
    return Math.floor(seconds/3600) + 'h ' + Math.round((seconds%3600)/60) + 'm';
}

function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return '';
    const h = Math.floor(seconds/3600), m = Math.floor((seconds%3600)/60), s = Math.round(seconds%60);
    if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    return `${m}:${String(s).padStart(2,'0')}`;
}

// ─── Theme ────────────────────────────────────────────────────────────────────
const THEME_KEY = 'hls_theme';
const themeToggleBtn = document.getElementById('themeToggleBtn');

function applyTheme(dark) {
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    themeToggleBtn.innerHTML = dark
        ? '<i class="material-icons-round">light_mode</i>'
        : '<i class="material-icons-round">dark_mode</i>';
    themeToggleBtn.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
}

function initTheme() {
    const saved = localStorage.getItem(THEME_KEY);
    const prefersDark = saved ? saved === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
    applyTheme(prefersDark);
}

themeToggleBtn.addEventListener('click', () => {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    applyTheme(!isDark);
    localStorage.setItem(THEME_KEY, !isDark ? 'dark' : 'light');
});

initTheme();

// ─── Sidebar toggle (all pages) ──────────────────────────────────────────────
let sidebarOpen = window.innerWidth > 1024;
let _sidebarTransitionTimer = null;
let _sidebarTransitionEndHandler = null;

function updateSidebar() {
    const sidebar = document.getElementById('sidebar');
    const mainEl = document.getElementById('mainContent');
    if (!sidebar) return;
    if (window.innerWidth <= 1024) {
        sidebar.classList.toggle('open', sidebarOpen);
        sidebar.classList.remove('collapsed');
        if (mainEl) mainEl.classList.add('sidebar-collapsed');
    } else {
        sidebar.classList.remove('open');
        sidebar.classList.toggle('collapsed', !sidebarOpen);
        if (mainEl) mainEl.classList.remove('sidebar-collapsed');
    }
}

(function () {
    const btn = document.getElementById('hamburgerBtn');
    if (!btn) return;
    btn.addEventListener('click', () => {
        sidebarOpen = !sidebarOpen;

        // On desktop, freeze grid widths during sidebar transition to prevent
        // continuous column reflow and card pop-in effect.
        if (window.innerWidth > 1024) {
            const sidebar = document.getElementById('sidebar');
            const grids = document.querySelectorAll('.video-grid');

            // Cancel any pending unfreeze from a previous click.
            if (_sidebarTransitionTimer) clearTimeout(_sidebarTransitionTimer);
            if (sidebar && _sidebarTransitionEndHandler) {
                sidebar.removeEventListener('transitionend', _sidebarTransitionEndHandler);
            }

            // Snapshot current width of each grid to freeze it during animation.
            grids.forEach(g => { g.style.width = g.offsetWidth + 'px'; });

            updateSidebar();

            let unfrozen = false;
            function unfreezeGrids() {
                if (unfrozen) return;
                unfrozen = true;
                _sidebarTransitionTimer = null;
                _sidebarTransitionEndHandler = null;
                grids.forEach(g => { g.style.width = ''; });
                if (sidebar) sidebar.removeEventListener('transitionend', _sidebarTransitionEndHandler);
            }
            _sidebarTransitionEndHandler = function (e) {
                if (e.propertyName === 'width') unfreezeGrids();
            };
            if (sidebar) sidebar.addEventListener('transitionend', _sidebarTransitionEndHandler);
            // Safety fallback slightly longer than --transition (250ms).
            _sidebarTransitionTimer = setTimeout(unfreezeGrids, 300);
        } else {
            updateSidebar();
        }
    });
    window.addEventListener('resize', updateSidebar);
    updateSidebar();
})();
