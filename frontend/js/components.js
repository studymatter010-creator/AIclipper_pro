/**
 * AIClipper Reusable UI Components
 * Toast notifications, modals, progress bars, cards, dropzones, etc.
 */

function gradientText(text) {
    return `<span class="gradient-text">${text}</span>`;
}

// ─────────────────────────────────────────────
// Toast Notification System
// ─────────────────────────────────────────────

const Toast = {
    _container: null,

    _getContainer() {
        if (!this._container) {
            this._container = document.getElementById('toastContainer');
        }
        return this._container;
    },

    show(message, type = 'info', duration = 4000) {
        const container = this._getContainer();
        const icons = { info: 'info', success: 'circle-check', warning: 'triangle-alert', error: 'circle-x' };

        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.innerHTML = `
            <span class="toast-icon"><i data-lucide="${icons[type] || 'info'}"></i></span>
            <span class="toast-message">${message}</span>
            <button class="toast-close" onclick="this.parentElement.remove()">✕</button>
        `;

        container.appendChild(toast);

        // Trigger animation
        requestAnimationFrame(() => toast.classList.add('toast-show'));

        if (duration > 0) {
            setTimeout(() => {
                toast.classList.remove('toast-show');
                toast.classList.add('toast-hide');
                setTimeout(() => toast.remove(), 300);
            }, duration);
        }
    },

    info(msg, duration)    { this.show(msg, 'info', duration); },
    success(msg, duration) { this.show(msg, 'success', duration); },
    warning(msg, duration) { this.show(msg, 'warning', duration); },
    error(msg, duration)   { this.show(msg, 'error', duration); },
};


// ─────────────────────────────────────────────
// Modal Dialog
// ─────────────────────────────────────────────

const Modal = {
    _overlay: null,
    _title: null,
    _body: null,
    _footer: null,

    _init() {
        this._overlay = document.getElementById('modalOverlay');
        this._title = document.getElementById('modalTitle');
        this._body = document.getElementById('modalBody');
        this._footer = document.getElementById('modalFooter');

        document.getElementById('modalClose').addEventListener('click', () => this.close());
        this._overlay.addEventListener('click', (e) => {
            if (e.target === this._overlay) this.close();
        });
    },

    open(title, bodyHtml, footerHtml = '', size = '') {
        if (!this._overlay) this._init();
        this._title.textContent = title;
        this._body.innerHTML = bodyHtml;
        this._footer.innerHTML = footerHtml;
        const modal = this._overlay.querySelector('.modal');
        if (modal) {
            modal.classList.remove('modal-lg');
            if (size === 'lg') modal.classList.add('modal-lg');
        }
        this._overlay.classList.add('active');
        document.body.style.overflow = 'hidden';
    },

    close() {
        if (this._overlay) {
            this._overlay.classList.remove('active');
            document.body.style.overflow = '';
            // Empty body to stop any playing videos/audio
            setTimeout(() => {
                if (!this._overlay.classList.contains('active')) {
                    this._body.innerHTML = '';
                    this._footer.innerHTML = '';
                }
            }, 300); // Wait for fade out animation
        }
    },

    confirm(title, message, onConfirm) {
        this.open(
            title,
            `<p style="color: var(--text-secondary); line-height: 1.6;">${message}</p>`,
            `<button class="btn btn-secondary" onclick="Modal.close()">Cancel</button>
             <button class="btn btn-danger" id="modalConfirmBtn">Confirm</button>`
        );
        document.getElementById('modalConfirmBtn').addEventListener('click', () => {
            this.close();
            onConfirm();
        });
    },
};


// ─────────────────────────────────────────────
// Component Renderers
// ─────────────────────────────────────────────

// ── Status pill — colored dot + sentence-case label, no emoji ────────────
// Tokens: ready/warning/ai = auto; the "kind" maps a status to an accent
// tone so "the model is working" stays visually distinct from "click this".
function renderStatusBadge(status) {
    const kindMap = {
        pending:    { kind: 'pending' },
        processing: { kind: 'processing' },
        uploaded:   { kind: 'pending' },
        uploading:  { kind: 'processing' },
        completed:  { kind: 'success' },
        generated:  { kind: 'success' },
        published:  { kind: 'success' },
        ready:      { kind: 'success' },
        scheduled:  { kind: 'pending' },
        failed:     { kind: 'error' },
        error:      { kind: 'error' },
        generating: { kind: 'ai' },
    };
    const kind = (kindMap[status] || { kind: 'neutral' }).kind;
    const label = (status || 'unknown')
        // "PENDING" → "Pending", "PROCESSING" → "Processing"
        .toLowerCase().replace(/_/g, ' ')
        .replace(/\b\w/g, c => c.toUpperCase());
    return `<span class="status-pill status-pill--${kind}"><i class="status-pill__dot"></i>${label}</span>`;
}

// True when |percent| is a real value/max ratio we can show; avoids a
// full-width bar for an unrelated absolute count (Part A #1).
function renderStatCard(value, label, category = 'neutral') {
    const cat = ['neutral', 'videos', 'clips', 'completed', 'published'].includes(category)
        ? category : 'neutral';
    return `
        <div class="stat-card stat-card--${cat}">
            <span class="stat-card__number mono">${value}</span>
            <span class="stat-card__label">${label}</span>
        </div>
    `;
}

function renderProgressBar(percent, label = '', animated = false) {
    const clampedPct = Math.max(0, Math.min(100, percent));
    return `
        <div class="progress-container">
            ${label ? `<div class="progress-label"><span>${label}</span><span class="mono">${Math.round(clampedPct)}%</span></div>` : ''}
            <div class="progress-bar"><div class="progress-fill ${animated ? 'progress-animated' : ''}" style="width: ${clampedPct}%"></div></div>
        </div>
    `;
}

/**
 * Build a browser-loadable URL for a thumbnail whose DB filepath is an
 * absolute on-disk path (e.g. C:\...\thumbnails\clip_5\thumb_001.jpg).
 * The backend serves /thumbnails → the thumbnail root dir, so we extract the
 * sub-path after the "thumbnails" segment and prefix the route. Falls back
 * to just the filename, then empty (→ video/placeholder), on any mismatch.
 */
function clipThumbUrl(filepath) {
    if (!filepath) return '';
    const fp = String(filepath).replace(/\\/g, '/');
    const marker = '/thumbnails/';
    const idx = fp.indexOf(marker);
    if (idx >= 0) return fp.slice(idx);                       // /thumbnails/clip_5/thumb_001.jpg
    const name = fp.split('/').pop();
    if (name) return '/thumbnails/' + name;
    return '';
}

/**
 * Build the browser-loadable URL for a stored clip/edited path (relative or
 * absolute). The backend serves /outputs → <root>/outputs, so we extract the
 * filename after the last separator and prefix the route. Returns '' if none.
 */
function clipVideoUrl(filepath) {
    if (!filepath) return '';
    return `/outputs/${String(filepath).replace(/\\/g, '/').split('/').pop()}`;
}

// Short label + emoji for an edit's subtitle language chip.
function editLangChip(lang) {
    return lang === 'zh'
        ? { flag: '🇨🇳', text: '中文' }
        : { flag: '🇺🇸', text: 'EN' };
}

/**
 * Render the labelled edit tags under a clip card: e.g.
 *   🎞️ Original   ✨ Edit 1 - English   ✨ Edit 2 - 中文
 * The original is always listed; each AI edit is its own selectable chip.
 */
function renderEditTags(clip) {
    const tags = [];
    tags.push(`<button type="button" class="edit-tag edit-tag-original" data-cvidx="-1"
        onclick="event.stopPropagation(); App.viewClip(${clip.id}, -1)"><i data-lucide="film"></i> Original</button>`);
    const edits = clip.edits || [];
    edits.forEach((e, i) => {
        const chip = editLangChip(e.language);
        tags.push(`<button type="button" class="edit-tag" data-cvidx="${i}"
            onclick="event.stopPropagation(); App.viewClip(${clip.id}, ${i})">
            ✨ ${escapeHtml(e.label)} <span class="edit-tag-lang">${chip.flag} ${chip.text}</span></button>`);
    });
    return tags.join('');
}

function renderClipCard(clip) {
    // The latest edit is the most recent thing the user made — make it the
    // primary preview so the edited video is the first thing they SEE. The
    // ORIGINAL (clip.output_path) is always preserved and reachable via the
    // "Original" tag / version switcher.
    const edits = clip.edits || [];
    const latestEdit = edits.length ? edits[edits.length - 1] : null;
    const previewPath = latestEdit ? latestEdit.output_path
        : (clip.edited_output_path || clip.output_path);
    const videoSrc = clipVideoUrl(previewPath);
    const thumbSrc = latestEdit && latestEdit.thumbnail_path
        ? clipThumbUrl(latestEdit.thumbnail_path)
        : (clip.thumbnails && clip.thumbnails.length > 0
            ? clipThumbUrl(clip.thumbnails[0].filepath)
            : '');
    const score = clip.total_score != null ? (clip.total_score * 100).toFixed(0) : '—';
    const duration = clip.duration ? formatDuration(clip.duration) : '—';
    const downloadUrl = `/api/clips/${clip.id}/download`;

    // Video-first: the edited video is an actual playable element, with the
    // composited thumbnail as its poster frame. If no video exists yet show
    // the thumbnail (or a placeholder).
    let mediaContent;
    if (videoSrc) {
        mediaContent = `
            <video class="clip-media" src="${videoSrc}" poster="${thumbSrc}"
                   controls preload="metadata" onloadeddata="this.currentTime=0.1">
            </video>`;
    } else if (thumbSrc) {
        mediaContent = `<img src="${thumbSrc}" class="clip-media" alt="Clip ${clip.clip_number}" loading="lazy"
            onerror="this.style.display='none'">`;
    } else {
        mediaContent = `<div class="clip-thumb-placeholder">🎬</div>`;
    }

    return `
        <div class="clip-card" data-clip-id="${clip.id}">
            <label class="clip-select" onclick="event.stopPropagation()" title="Select for bulk actions">
                <input type="checkbox" class="clip-select-cb" data-clip-id="${clip.id}" onchange="App._toggleClipSelect(this)">
            </label>
            <div class="clip-card-thumbnail" onclick="App.viewClip(${clip.id})">
                ${mediaContent}
                <div class="clip-card-overlay">
                    <button class="play-button" title="Preview clip">▶</button>
                </div>
                <div class="clip-card-duration">${duration}</div>
                <div class="clip-card-score"><span class="badge badge-info">🎯 ${score}%</span></div>
                ${latestEdit ? '<div class="clip-card-edited-badge">✨ Edited</div>' : ''}
            </div>
            <div class="clip-card-body">
                <h3 class="clip-card-title">${clip.title || `Clip #${clip.clip_number}`}</h3>
                ${clip.hook_sentence
                    ? `<p class="clip-card-hook">🎣 ${escapeHtml(clip.hook_sentence)}</p>`
                    : ''}
                <div class="clip-card-tags">${renderEditTags(clip)}</div>
                <div class="clip-card-meta">
                    <span>${formatDuration(clip.start_time)} → ${formatDuration(clip.end_time)}</span>
                    ${renderStatusBadge(clip.status)}
                </div>
                <div class="clip-card-actions">
                    <button class="btn btn-sm btn-accent" onclick="event.stopPropagation(); App.openAIEditor(${clip.id})" title="AI Editor Studio">✨ AI Editor</button>
                    <button class="btn btn-sm btn-secondary" onclick="event.stopPropagation(); App.viewClip(${clip.id})" title="Preview">▶</button>
                    <a href="${downloadUrl}" class="btn btn-sm btn-secondary" onclick="event.stopPropagation()" download title="Download video">⬇️</a>
                    <a href="/api/clips/${clip.id}/thumbnail" class="btn btn-sm btn-secondary" onclick="event.stopPropagation()" download title="Download thumbnail">🖼️</a>
                    <button class="btn btn-sm btn-danger" onclick="event.stopPropagation(); App.deleteClip(${clip.id})" title="Delete">🗑️</button>
                </div>
            </div>
        </div>
    `;
}

// Flag a video as a re-processed duplicate of an earlier row with the same
// filename (see Part A #4). setVideoList() records the current list; the card
// labels any row after the first with the same filename "Re-uploaded".
let __videoList = [];
function setVideoList(list) { __videoList = Array.isArray(list) ? list : []; }
function __isReupload(video) {
    if (!video || !video.filename) return false;
    let firstIdx = -1;
    for (let i = 0; i < __videoList.length; i++) {
        if (__videoList[i] && __videoList[i].filename === video.filename) {
            if (firstIdx === -1) firstIdx = i;
            else if (i === __videoList.indexOf(video)) return true;
        }
    }
    return false;
}

function renderVideoCard(video, videoIdx = -1) {
    const duration = video.duration ? formatDuration(video.duration) : '—';
    const size = video.filesize ? formatFileSize(video.filesize) : '—';
    // Resolution from real backend fields (Part A #2) — never "?x?".
    const res = (video.width && video.height)
        ? `${video.width}×${video.height}`
        : '';
    const meta = [duration, size, res].filter(Boolean).join(' · ');
    const reuploaded = videoIdx >= 0
        ? __videoList.findIndex((v) => v && v.filename === video.filename && v.id !== video.id) >= 0
        : __isReupload(video);

    return `
        <div class="video-card" data-video-id="${video.id}">
            <div class="video-card-thumb">
                <i data-lucide="film"></i>
                ${reuploaded ? '<span class="video-card-reup">Re-uploaded</span>' : ''}
            </div>
            <div class="video-card-body">
                <h3 class="video-title" title="${escapeHtml(video.filename)}">${escapeHtml(video.filename)}</h3>
                <p class="video-meta mono">${meta}</p>
                ${renderStatusBadge(video.status)}
            </div>
            ${video.status === 'processing'
                ? renderProgressBar(video.processing_progress || 0, video.processing_step || 'Processing', true)
                : ''}
            <div class="video-card-actions">
                ${video.status === 'pending'
                    ? `<button class="btn btn-accent btn-sm" onclick="App.processVideo(${video.id})">Process</button>`
                    : ''}
                ${video.status === 'completed'
                    ? `<button class="btn btn-secondary btn-sm" onclick="App.navigate('clips', {video_id: ${video.id}})">View Clips</button>`
                    : ''}
                <button class="btn btn-icon btn-danger-outline btn-sm" title="Delete video"
                        onclick="App._deleteVideoPrompt(${video.id}, '${escapeHtml(String(video.filename)).replace(/'/g, "\\'")}')">
                    <i data-lucide="trash-2"></i>
                </button>
            </div>
        </div>
    `;
}

function renderDropzone() {
    return `
        <div class="dropzone" id="dropzone">
            <div class="dropzone-content">
                <div class="dropzone-icon" data-lucide="folder-plus"></div>
                <h3>Drag & Drop Video Here</h3>
                <p>or click to browse</p>
                <p class="dropzone-formats">Supports MP4, MKV, AVI, MOV · Max 4GB</p>
            </div>
            <input type="file" id="fileInput" accept=".mp4,.mkv,.avi,.mov" hidden>
        </div>
    `;
}

function renderSpinner(text = 'Loading...') {
    return `
        <div class="loading-container">
            <div class="spinner"></div>
            <p>${text}</p>
        </div>
    `;
}

function renderEmptyState(icon, message, actionHtml = '') {
    // Pass a Lucide icon *name* (e.g. 'film') and it renders as an icon;
    // pass a full element string and it is injected as-is.
    const iconHtml = String(icon).indexOf('<') === -1
        ? `<i data-lucide="${icon}"></i>`
        : icon;
    return `
        <div class="empty-state">
            <div class="empty-icon">${iconHtml}</div>
            <p>${message}</p>
            ${actionHtml}
        </div>
    `;
}

function renderProcessingPanel(video) {
    const PIPELINE_STEPS = [
        { key: 'transcription', label: 'Transcription', icon: '🎙️', range: [0, 20], desc: 'Extracting speech with Whisper AI' },
        { key: 'scene_detection', label: 'Scene Detection', icon: '🎬', range: [20, 35], desc: 'Finding scene boundaries' },
        { key: 'audio_analysis', label: 'Audio Analysis', icon: '🔊', range: [35, 50], desc: 'Analyzing energy, laughter, emotion' },
        { key: 'face_tracking', label: 'Face Tracking', icon: '👤', range: [50, 65], desc: 'Detecting faces for smart cropping' },
        { key: 'clip_scoring', label: 'Clip Scoring', icon: '🎯', range: [65, 70], desc: 'Ranking best moments' },
        { key: 'clip_generation', label: 'Clip Generation', icon: '✂️', range: [70, 85], desc: 'Cutting & converting clips to 9:16' },
        { key: 'subtitles', label: 'Subtitles', icon: '💬', range: [85, 90], desc: 'Generating SRT/VTT files' },
        { key: 'metadata', label: 'Metadata', icon: '🏷️', range: [90, 95], desc: 'AI-generated titles & hashtags' },
        { key: 'thumbnails', label: 'Thumbnails', icon: '🖼️', range: [95, 100], desc: 'Selecting best frames' },
    ];

    const progress = video.processing_progress || 0;
    const step = video.processing_step || 'Initializing...';
    const duration = video.duration ? formatDuration(video.duration) : '—';
    // Resolution from real backend fields (Part A #2) — never "?x?".
    const res = (video.width && video.height)
        ? `${video.width}×${video.height}`
        : '';
    // Machine-readable active stage + its 0-100 sub-progress (null = indeterminate).
    const curStage = video.current_stage || null;
    const stageProg = (typeof video.stage_progress === 'number') ? video.stage_progress : null;

    const stepsHtml = PIPELINE_STEPS.map(s => {
        let state = 'pending';
        if (progress >= s.range[1]) state = 'done';
        else if (progress >= s.range[0]) state = 'active';

        // Per-row progress bar (#88): done=full, active+real value=filled %,
        // active+indeterminate=animated pulse, pending=visible empty track.
        // Issues 1+2 (2026-09-12): every row gets a visible bar state AND a
        // right-aligned live % beside the title when it's genuinely running.
        let barHtml;
        let pctHtml = '';
        if (state === 'done') {
            barHtml = '<div class="step-bar step-bar--done"><div class="step-bar__fill" style="width:100%"></div></div>';
        } else if (state === 'active') {
            const live = (curStage === s.key) && stageProg !== null && !Number.isNaN(stageProg);
            if (live) {
                const sp = Math.max(0, Math.min(100, stageProg));
                barHtml = `<div class="step-bar step-bar--live"><div class="step-bar__fill" style="width:${sp}%"></div><span class="step-bar__pct mono">${Math.round(sp)}%</span></div>`;
                // Issue 1: clear, right-aligned number in the row header itself.
                pctHtml = `<span class="step-header__pct mono">${Math.round(sp)}%</span>`;
            } else {
                barHtml = '<div class="step-bar step-bar--indeterminate"><div class="step-bar__fill step-bar__fill--slide"></div></div>';
                // Indeterminate → pulsing bar, no fake number.
            }
        } else {
            // Upcoming / not-started: visible hollow track + muted 0%.
            barHtml = '<div class="step-bar step-bar--empty"></div>';
            pctHtml = '<span class="step-header__pct step-header__pct--idle mono">0%</span>';
        }

        return `
            <div class="pipeline-step pipeline-step-${state}" data-step="${s.key}">
                <div class="step-indicator">
                    <div class="step-dot">${state === 'done' ? '✓' : state === 'active' ? '<div class="step-pulse"></div>' : ''}</div>
                    ${s !== PIPELINE_STEPS[PIPELINE_STEPS.length - 1] ? '<div class="step-line"></div>' : ''}
                </div>
                <div class="step-content">
                    <div class="step-header">
                        <span class="step-icon">${s.icon}</span>
                        <span class="step-label">${s.label}</span>
                        ${state === 'active' ? '<span class="step-active-badge">Running</span>' : ''}
                        ${state === 'done' ? '<span class="step-done-badge">Done</span>' : ''}
                        ${pctHtml}
                    </div>
                    <p class="step-desc">${s.desc}</p>
                    ${barHtml}
                </div>
            </div>
        `;
    }).join('');

    return `
        <div class="processing-detail-panel">
            <div class="processing-header">
                <div class="processing-file-info">
                    <span class="processing-file-icon" data-lucide="film"></span>
                    <div>
                        <h3>${escapeHtml(video.filename)}</h3>
                        <p class="text-muted">${duration}${res ? ' · ' + res : ''}</p>
                    </div>
                </div>
                <div class="processing-timer" id="processingTimer">
                    <span class="timer-icon" data-lucide="timer"></span>
                    <span class="timer-value" id="elapsedTime">00:00</span>
                </div>
            </div>

            <div class="processing-progress-section">
                <div class="progress-big-label">
                    <span id="currentStepText">${step}</span>
                    <span class="progress-pct" id="progressPct">${progress}%</span>
                </div>
                <div class="progress-bar progress-bar-lg">
                    <div class="progress-fill progress-animated" id="progressFill" style="width: ${progress}%"></div>
                </div>
            </div>

            <div class="processing-body">
                <div class="pipeline-timeline" id="pipelineTimeline">
                    ${stepsHtml}
                </div>

                <div class="processing-activity">
                    <div class="activity-header">
                        <h4>📋 Activity Log</h4>
                        <span class="activity-count" id="logCount">0 events</span>
                    </div>
                    <div class="activity-feed" id="activityFeed">
                        <div class="activity-entry">
                            <span class="activity-time">${new Date().toLocaleTimeString()}</span>
                            <span class="activity-msg">Pipeline started...</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function renderPagination(currentPage, totalPages, onPageChange) {
    if (totalPages <= 1) return '';
    let html = '<div class="pagination">';
    html += `<button class="btn btn-sm btn-secondary" ${currentPage <= 1 ? 'disabled' : ''} onclick="${onPageChange}(${currentPage - 1})">← Prev</button>`;
    html += `<span class="page-info">Page ${currentPage} of ${totalPages}</span>`;
    html += `<button class="btn btn-sm btn-secondary" ${currentPage >= totalPages ? 'disabled' : ''} onclick="${onPageChange}(${currentPage + 1})">Next →</button>`;
    html += '</div>';
    return html;
}


// ─────────────────────────────────────────────
// Utility Functions
// ─────────────────────────────────────────────

function formatDuration(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return m > 0 ? `${m}:${s.toString().padStart(2, '0')}` : `${s}s`;
}

function formatFileSize(bytes) {
    if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GB';
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MB';
    if (bytes >= 1024) return (bytes / 1024).toFixed(0) + ' KB';
    return bytes + ' B';
}

function formatDate(dateStr) {
    if (!dateStr) return '—';
    const d = new Date(dateStr);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str || '';
    return div.innerHTML;
}

function debounce(fn, delay = 300) {
    let timer;
    return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => fn(...args), delay);
    };
}
