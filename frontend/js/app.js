/**
 * AIClipper Main Application
 * SPA router, page renderers, event handling.
 */

const App = {
    currentPage: 'dashboard',
    state: {},

    // ─────────────────────────────────────────────
    // Initialization
    // ─────────────────────────────────────────────

    async init() {
        this._setupNavigation();
        this._setupSidebar();
        this._setupKeyboard();
        this._initAiModels();

        // Route to current hash or default
        const hash = window.location.hash.slice(1) || 'dashboard';
        await this.navigate(hash);

        // Hide loading
        const loading = document.getElementById('loadingScreen');
        if (loading) loading.style.display = 'none';
    },

    // ─────────────────────────────────────────────
    // AI Models widget (sidebar) — shows the model team
    // coming up with the app and their live status.
    // ─────────────────────────────────────────────

    _initAiModels() {
        // Open the roster by default so the user can SEE the models coming up
        // with the app (they asked for a visible "models starting" panel).
        this._aiModelsOpen = true;
        const roster = document.getElementById('aiModelsRoster');
        if (roster) roster.style.display = 'block';
        this._refreshAiModels();
        // Refresh periodically so the user sees Ollama + the team "starting".
        this._aiModelsTimer = setInterval(() => this._refreshAiModels(), 5000);
    },

    _toggleAiModels() {
        const roster = document.getElementById('aiModelsRoster');
        if (!roster) return;
        this._aiModelsOpen = !this._aiModelsOpen;
        roster.style.display = this._aiModelsOpen ? 'block' : 'none';
    },

    async _refreshAiModels() {
        const stateEl = document.getElementById('aiModelsState');
        const rosterEl = document.getElementById('aiModelsRoster');
        if (!stateEl) return;

        // Ollama reachability ping (the AI brain must be up for the team).
        let ollamaUp = false;
        let roster = null;
        let installed = [];
        let providers = null;
        try {
            const res = await api.teamStatus();
            if (res && res.roster && typeof res.roster === 'object') roster = res.roster;
            if (res && typeof res.ollama_ready === 'boolean') ollamaUp = res.ollama_ready;
            if (res && Array.isArray(res.models_installed)) installed = res.models_installed;
            if (res && res.providers && typeof res.providers === 'object') providers = res.providers;
        } catch (e) { /* keep ollamaUp false */ }

        // Header state chip: ready (success green) / starting (warning amber).
        stateEl.style.color = ollamaUp
            ? 'var(--accent-success)'
            : 'var(--status-warning)';
        stateEl.innerHTML = `<span class="status-pill__dot" style="background:${ollamaUp ? 'var(--accent-success)' : 'var(--status-warning)'}"></span> ${ollamaUp ? 'ready' : 'starting'}`;

        if (rosterEl && this._aiModelsOpen) {
            const PROVIDER_TIER = { local: 'free', openai: 'paid', anthropic: 'paid', gemini: 'paid', openai_compatible: 'paid' };
            const PROVIDER_SHORT = { local: '', openai: 'OpenAI', anthropic: 'Claude', gemini: 'Gemini', openai_compatible: 'API' };
            const roleProviders = (providers && providers.roles) || {};

            const rows = Object.entries(roster || {}).map(([role, model]) => {
                const rp = roleProviders[role] || {};
                const provName = rp.provider || 'local';
                const isApi = provName !== 'local';
                const tier = PROVIDER_TIER[provName] || 'free';
                const provLabel = isApi ? (PROVIDER_SHORT[provName] || provName) : '';
                const installedThis = installed.some(m => String(m).split(':')[0] === String(model).split(':')[0]);

                let kind = 'pending';
                if (!ollamaUp && !isApi) kind = 'pending';
                else if (isApi) kind = 'ready'; // API providers are always "ready" if configured
                else if (installedThis) kind = 'ready';
                else kind = 'error';

                const dotColor = !ollamaUp && !isApi
                    ? 'var(--status-warning)'
                    : (installedThis || isApi)
                        ? 'var(--accent-success)'
                        : 'var(--status-error)';

                const tierBadge = isApi
                    ? `<span class="ai-tier-badge ai-tier--paid" title="API provider (usage billed)">paid</span>`
                    : '';

                const provTag = provLabel
                    ? `<span class="ai-provider-tag">${escapeHtml(provLabel)}</span>`
                    : '';

                return `<div class="ai-row">
                    <span class="ai-role">${escapeHtml(role)}${provTag}</span>
                    <code class="ai-model mono">${escapeHtml(model)}</code>
                    ${tierBadge}
                    <span class="ai-state" style="color:${dotColor}" title="${kind}">
                        <i class="status-pill__dot" style="background:${dotColor}"></i>
                    </span>
                </div>`;
            }).join('');
            if (!rows) {
                rosterEl.innerHTML = '<div class="ai-row"><span class="ai-role">No models configured.</span></div>';
            } else {
                rosterEl.innerHTML = rows;
            }
            // Show session usage if any API calls happened.
            if (providers && providers.usage && providers.usage.total_calls > 0) {
                const u = providers.usage;
                const cost = u.est_usd != null ? `$${u.est_usd.toFixed(4)}` : '';
                rosterEl.innerHTML += `<div class="ai-row ai-usage-row">
                    <span class="ai-role" style="font-size:0.75rem;opacity:0.7;">Session: ${u.total_calls} call(s) ${cost}</span>
                </div>`;
            }
            if (!ollamaUp) {
                rosterEl.innerHTML += '<div class="ai-row"><span class="ai-role">Ollama not reachable — models still starting…</span></div>';
            }
        }
    },

    _initTeamStatusFallback() {
        // Unused hook kept for editor team-status consumers that expect it.
        return this._loadTeamStatus();
    },

    _setupNavigation() {
        window.addEventListener('hashchange', () => {
            const hash = window.location.hash.slice(1) || 'dashboard';
            this.navigate(hash);
        });

        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                const page = item.dataset.page;
                window.location.hash = page;
            });
        });
    },

    _setupSidebar() {
        const toggle = document.getElementById('sidebarToggle');
        const sidebar = document.getElementById('sidebar');
        const mobile = document.getElementById('mobileMenuBtn');

        if (toggle) toggle.addEventListener('click', () => sidebar.classList.toggle('collapsed'));
        if (mobile) mobile.addEventListener('click', () => sidebar.classList.toggle('mobile-open'));
    },

    _setupKeyboard() {
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') Modal.close();
        });
    },

    // ─────────────────────────────────────────────
    // Router
    // ─────────────────────────────────────────────

    async navigate(page, params = {}) {
        this.state = params;
        this.currentPage = page;

        // Update active nav
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        const activeNav = document.querySelector(`.nav-item[data-page="${page}"]`);
        if (activeNav) activeNav.classList.add('active');

        // Update title
        const titles = {
            dashboard: 'Dashboard', projects: 'Projects', upload: 'Upload Video',
            processing: 'Processing', clips: 'Generated Clips',
            publishing: 'Publishing Center', settings: 'Settings',
        };
        document.getElementById('pageTitle').textContent = titles[page] || page;

        // Close mobile sidebar
        document.getElementById('sidebar')?.classList.remove('mobile-open');

        // Render page
        const content = document.getElementById('contentArea');
        content.innerHTML = renderSpinner('Loading...');

        try {
            switch (page) {
                case 'dashboard': await this._renderDashboard(content); break;
                case 'projects': await this._renderProjects(content); break;
                case 'upload': await this._renderUpload(content); break;
                case 'processing': await this._renderProcessing(content); break;
                case 'clips': await this._renderClips(content); break;
                case 'publishing': await this._renderPublishing(content); break;
                case 'settings': await this._renderSettings(content); break;
                default: content.innerHTML = renderEmptyState('🔍', 'Page not found');
            }
        } catch (err) {
            content.innerHTML = renderEmptyState('alert-circle', `Error: ${err.message}`);
            Toast.error(err.message);
        }

        // Resolve any <i data-lucide="..."> icons injected by page renderers.
        if (window.lucide && typeof window.lucide.createIcons === 'function') {
            try { window.lucide.createIcons(); } catch (e) { /* non-fatal */ }
        }
    },

    // ─────────────────────────────────────────────
    // Dashboard Page
    // ─────────────────────────────────────────────

    async _renderDashboard(el) {
        let stats = { total_videos: 0, total_clips: 0, completed_clips: 0, published_uploads: 0, total_projects: 0 };
        try { stats = await api.getAnalytics(); } catch { /* use defaults */ }

        const successRate = stats.total_clips > 0
            ? Math.round((stats.completed_clips / stats.total_clips) * 100) : 0;

        el.innerHTML = `
            <div class="dashboard-hero">
                <div class="hero-content">
                    <h2 class="hero-title">Welcome to <span class="gradient-text">AIClipper</span></h2>
                    <p class="hero-subtitle">Transform your videos into viral short-form content with AI</p>
                </div>
                <div class="hero-actions">
                    <button class="btn btn-primary btn-lg" onclick="App.navigate('upload')">
                        <span>⬆️</span> Upload Video
                    </button>
                    <button class="btn btn-secondary btn-lg" onclick="App.navigate('clips')">
                        <span class="action-icon" data-lucide="clapperboard"></span> Browse Clips
                    </button>
                </div>
            </div>
            <div class="dashboard-grid">
                <div class="stats-row">
                    ${renderStatCard(stats.total_videos, 'Total Videos', 'videos')}
                    ${renderStatCard(stats.total_clips, 'Clips Generated', 'clips')}
                    ${renderStatCard(stats.completed_clips, 'Completed', 'completed')}
                    ${renderStatCard(stats.published_uploads, 'Published', 'published')}
                </div>

                <div class="dashboard-panels">
                    <div class="panel">
                        <div class="panel-header">
                            <h2>Recent Videos</h2>
                            <button class="btn btn-sm btn-primary" onclick="App.navigate('upload')">+ Upload</button>
                        </div>
                        <div class="panel-body" id="recentVideos">${renderSpinner()}</div>
                    </div>

                    <div class="panel">
                        <div class="panel-header">
                            <h2>Latest Clips</h2>
                            <button class="btn btn-sm btn-secondary" onclick="App.navigate('clips')">View All</button>
                        </div>
                        <div class="panel-body" id="latestClips">${renderSpinner()}</div>
                    </div>
                </div>

                <div class="panel quick-actions-panel">
                    <div class="panel-header"><h2>Quick Actions</h2></div>
                    <div class="quick-actions">
                        <button class="action-card" onclick="App.navigate('upload')">
                            <span class="action-icon">⬆️</span>
                            <span class="action-label">Upload Video</span>
                        </button>
                        <button class="action-card" onclick="App.navigate('clips')">
                            <span class="action-icon" data-lucide="clapperboard"></span>
                            <span class="action-label">Browse Clips</span>
                        </button>
                        <button class="action-card" onclick="App.navigate('projects')">
                            <span class="action-icon" data-lucide="folder"></span>
                            <span class="action-label">New Project</span>
                        </button>
                        <button class="action-card" onclick="App.navigate('settings')">
                            <span class="action-icon">⚡</span>
                            <span class="action-label">Settings</span>
                        </button>
                    </div>
                </div>
            </div>
        `;

        // Load recent videos
        this._loadRecentVideos();
        this._loadLatestClips();
    },

    async _loadRecentVideos() {
        const container = document.getElementById('recentVideos');
        try {
            const data = await api.listVideos(0, 5);
            const videos = data.videos || [];
            if (videos.length === 0) {
                container.innerHTML = renderEmptyState('film', 'No videos yet', '<button class="btn btn-primary btn-sm" onclick="App.navigate(\'upload\')">Upload First Video</button>');
                return;
            }
            setVideoList(videos);
            container.innerHTML = videos.map((v, i) => renderVideoCard(v, i)).join('');
        } catch { container.innerHTML = '<p class="text-muted">Could not load videos</p>'; }
    },

    async _loadLatestClips() {
        const container = document.getElementById('latestClips');
        try {
            const data = await api.listClips(0, 6);
            const clips = data.clips || [];
            if (clips.length === 0) {
                container.innerHTML = renderEmptyState('clapperboard', 'No clips generated yet');
                return;
            }
            container.innerHTML = '<div class="clips-grid">' + clips.map(renderClipCard).join('') + '</div>';
        } catch { container.innerHTML = '<p class="text-muted">Could not load clips</p>'; }
    },

    // ─────────────────────────────────────────────
    // Projects Page
    // ─────────────────────────────────────────────

    async _renderProjects(el) {
        const data = await api.listProjects();
        const projects = data.projects || [];

        el.innerHTML = `
            <div class="page-actions">
                <button class="btn btn-primary" onclick="App.createProject()">+ New Project</button>
            </div>
            <div class="projects-grid" id="projectsList">
                ${projects.length === 0
                ? renderEmptyState('folder', 'No projects yet', '<button class="btn btn-primary" onclick="App.createProject()">Create Project</button>')
                : projects.map(p => `
                        <div class="project-card">
                            <div class="project-card-header">
                                <div class="project-card-icon" data-lucide="folder"></div>
                            </div>
                            <h3 class="project-card-title">${escapeHtml(p.name)}</h3>
                            <p class="project-card-desc">${escapeHtml(p.description || 'No description')}</p>
                            <div class="project-card-stats">
                                ${renderStatusBadge(p.status)}
                                <span class="text-muted">${formatDate(p.created_at)}</span>
                            </div>
                        </div>
                    `).join('')
            }
            </div>
        `;
    },

    createProject() {
        Modal.open('Create Project', `
            <div class="form-group">
                <label>Project Name</label>
                <input type="text" class="form-input" id="newProjectName" placeholder="My Project" autofocus>
            </div>
            <div class="form-group">
                <label>Description (optional)</label>
                <textarea class="form-input" id="newProjectDesc" placeholder="Project description..." rows="3"></textarea>
            </div>
        `, `
            <button class="btn btn-secondary" onclick="Modal.close()">Cancel</button>
            <button class="btn btn-primary" onclick="App._doCreateProject()">Create</button>
        `);
    },

    async _doCreateProject() {
        const name = document.getElementById('newProjectName').value.trim();
        if (!name) { Toast.warning('Please enter a project name'); return; }
        try {
            await api.createProject(name, document.getElementById('newProjectDesc').value.trim());
            Modal.close();
            Toast.success('Project created!');
            this.navigate('projects');
        } catch (e) { Toast.error(e.message); }
    },

    // ─────────────────────────────────────────────
    // Upload Page
    // ─────────────────────────────────────────────

    async _renderUpload(el) {
        el.innerHTML = `
            <div class="upload-page">
                <div class="upload-header" style="margin-bottom: 1.5rem; text-align: center;">
                    <h2 style="font-size: 2rem; margin-bottom: 0.5rem;">Import a Video for AI Clipping</h2>
                    <p class="text-muted" style="margin-bottom: 1.5rem;">Paste a YouTube/Facebook link <em>or</em> upload a file (MP4, MKV, AVI, MOV · Max 4GB)</p>
                    <div class="upload-features" style="display: flex; gap: 1rem; justify-content: center; margin-bottom: 2rem; flex-wrap: wrap;">
                        <span class="badge badge-info" style="font-size: 1rem; padding: 0.5rem 1rem;">✨ AI Brain Scoring</span>
                        <span class="badge badge-info" style="font-size: 1rem; padding: 0.5rem 1rem;"><i data-lucide="link" class="btn-icon"></i> URL Import</span>
                        <span class="badge badge-info" style="font-size: 1rem; padding: 0.5rem 1rem;">👤 Face Tracking</span>
                        <span class="badge badge-info" style="font-size: 1rem; padding: 0.5rem 1rem;">💬 Captions</span>
                    </div>
                </div>

                <div class="panel" style="border-radius: 14px; overflow: hidden;">
                    <div class="url-import" style="padding: 1.25rem; border-bottom: 1px solid var(--border-color); display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: center;">
                        <i class="url-icon" data-lucide="link"></i>
                        <input type="text" id="urlInput" class="form-input" style="flex: 1; min-width: 240px;"
                               placeholder="Paste YouTube / Facebook URL... (e.g. https://youtube.com/watch?v=...)">
                        <button class="btn btn-primary" id="importUrlBtn" style="white-space: nowrap;">⬇️ Download & Clip</button>
                    </div>
                </div>

                <div class="upload-divider" style="display: flex; align-items: center; gap: 1rem; margin: 1.5rem 0; color: var(--text-muted);">
                    <div style="flex: 1; height: 1px; background: var(--border-color);"></div>
                    <span style="font-size: 0.9rem;">OR</span>
                    <div style="flex: 1; height: 1px; background: var(--border-color);"></div>
                </div>

                <div class="clip-options" style="padding: 1.25rem; background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 14px; margin-bottom: 1.5rem;">
                    <div style="font-weight: 600; margin-bottom: 1rem;"><i data-lucide="sliders-horizontal" class="btn-icon" style="margin-right:6px"></i> Clipping Options</div>
                    <div style="display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: flex-end;">
                        <label style="display: flex; flex-direction: column; gap: 0.4rem; flex: 1; min-width: 180px;">
                            <span class="text-muted" style="font-size: 0.85rem;">Number of clips</span>
                            <select id="clipCountSelect" class="form-input">
                                <option value="">Auto (based on video length)</option>
                                <option value="4">4 clips</option>
                                <option value="6">6 clips</option>
                                <option value="8">8 clips</option>
                                <option value="10">10 clips</option>
                                <option value="15">15 clips</option>
                                <option value="20">20 clips</option>
                            </select>
                        </label>
                        <label style="display: flex; flex-direction: column; gap: 0.4rem; flex: 1; min-width: 180px;">
                            <span class="text-muted" style="font-size: 0.85rem;">Clip length</span>
                            <select id="clipDurationSelect" class="form-input">
                                <option value="75">1 minute 15 sec</option>
                                <option value="60">60 seconds</option>
                                <option value="90">1.5 minutes</option>
                                <option value="120">2 minutes</option>
                            </select>
                        </label>
                        <p class="text-muted" style="font-size: 0.82rem; flex-basis: 100%; margin: 0;">
                            Tip: long videos (2h+) work best with 10-20 clips; shorter episodes shine with 5-6.
                        </p>
                    </div>
                </div>

                ${renderDropzone()}
                <div class="upload-status" id="uploadStatus" style="display:none;">
                    <div class="upload-file-info" id="uploadFileInfo"></div>
                    ${renderProgressBar(0, 'Uploading...')}
                    <div id="uploadActions"></div>
                </div>
            </div>
        `;

        this._setupDropzone();
        this._setupUrlImport();
    },

    _setupUrlImport() {
        const input = document.getElementById('urlInput');
        const btn = document.getElementById('importUrlBtn');
        if (!input || !btn) return;

        const runImport = () => {
            const url = input.value.trim();
            if (!url) { Toast.warning('Paste a YouTube or Facebook URL first.'); return; }

            const clipOpts = App._readClipOptions() || {};
            const opts = {
                auto_process: true,
                clip_count: clipOpts.clip_count || null,
                clip_duration: clipOpts.clip_duration || null,
            };

            btn.disabled = true;
            btn.textContent = '⏳ Downloading video...';

            api.importFromUrl(url, opts)
                .then((video) => {
                    Toast.success(`Downloaded "${video.filename}". Starting AI clipping...`);
                    App.navigate('processing');
                })
                .catch((e) => {
                    Toast.error('Import failed: ' + e.message);
                    btn.disabled = false;
                    btn.textContent = '⬇️ Download & Clip';
                });
        };

        btn.addEventListener('click', runImport);
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') runImport(); });
    },

    _setupDropzone() {
        const dropzone = document.getElementById('dropzone');
        const fileInput = document.getElementById('fileInput');

        dropzone.addEventListener('click', () => fileInput.click());

        dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
        dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) this._handleUpload(e.dataTransfer.files[0]);
        });

        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) this._handleUpload(e.target.files[0]);
        });
    },

    async _handleUpload(file) {
        const status = document.getElementById('uploadStatus');
        const fileInfo = document.getElementById('uploadFileInfo');
        const dropzone = document.getElementById('dropzone');

        dropzone.style.display = 'none';
        status.style.display = 'block';
        fileInfo.innerHTML = `
            <span class="file-icon" data-lucide="film"></span>
            <div>
                <strong>${escapeHtml(file.name)}</strong>
                <span class="text-muted">${formatFileSize(file.size)}</span>
            </div>
        `;

        try {
            const result = await api.uploadVideo(file, null, (pct) => {
                const fill = status.querySelector('.progress-fill');
                const label = status.querySelector('.progress-label span:last-child');
                if (fill) fill.style.width = pct + '%';
                if (label) label.textContent = pct + '%';
            });

            Toast.success('Video uploaded successfully!');
            const opts = App._readClipOptions();
            document.getElementById('uploadActions').innerHTML = `
                <div class="upload-success">
                    <p>✅ Upload complete! Video ID: <strong>${result.id}</strong></p>
                    <div class="btn-group">
                        <button class="btn btn-primary" onclick="App.processVideo(${result.id})"><i data-lucide="play" class="btn-icon"></i> Process Now</button>
                        <button class="btn btn-secondary" onclick="App.navigate('upload')">Upload Another</button>
                    </div>
                </div>
            `;
            App._lastClipOptions = opts;
        } catch (err) {
            Toast.error('Upload failed: ' + err.message);
            dropzone.style.display = 'block';
            status.style.display = 'none';
        }
    },

    // ─────────────────────────────────────────────
    // Processing Page
    // ─────────────────────────────────────────────

    async _renderProcessing(el) {
        const data = await api.listVideos(0, 50);
        const allVideos = data.videos || [];
        // Record the full catalog so renderVideoCard can flag genuinely
        // re-uploaded rows (duplicate filename, different id) as "Re-uploaded".
        setVideoList(allVideos);
        const processingVideos = allVideos.filter(v => v.status === 'processing');
        const pendingVideos = allVideos.filter(v => v.status === 'pending');
        const completedVideos = allVideos.filter(v => v.status === 'completed' || v.status === 'failed');

        // If there's an active processing video, show the detailed panel
        if (processingVideos.length > 0) {
            const activeVideo = processingVideos[0];
            // Fetch full detail for the active video
            let videoDetail = activeVideo;
            try {
                videoDetail = await api.getVideo(activeVideo.id);
            } catch { /* use list data */ }

            const PIPELINE_STEPS = [
                { key: 'transcription', label: 'Transcription', icon: '🎙️', range: [0, 20] },
                { key: 'scene_detection', label: 'Scenes', icon: '🎬', range: [20, 35] },
                { key: 'audio_analysis', label: 'Audio', icon: '🔊', range: [35, 50] },
                { key: 'face_tracking', label: 'Faces', icon: '👤', range: [50, 65] },
                { key: 'clip_scoring', label: 'Scoring', icon: '🎯', range: [65, 70] },
                { key: 'clip_generation', label: 'Clips', icon: '✂️', range: [70, 85] },
                { key: 'subtitles', label: 'Subs', icon: '💬', range: [85, 90] },
                { key: 'metadata', label: 'Meta', icon: '🏷️', range: [90, 95] },
                { key: 'thumbnails', label: 'Thumbs', icon: '🖼️', range: [95, 100] },
            ];

            const progress = videoDetail.processing_progress || 0;
            const stepsHtml = PIPELINE_STEPS.map(s => {
                let state = 'pending';
                if (progress >= s.range[1]) state = 'done';
                else if (progress >= s.range[0]) state = 'active';
                return `
                    <div class="stepper-item stepper-${state}" data-stepper="${s.key}" style="display: flex; flex-direction: column; align-items: center; gap: 5px; flex: 1;">
                        <div class="stepper-icon" style="font-size: 1.5rem; background: var(--bg-secondary); padding: 10px; border-radius: 50%; border: 2px solid ${state === 'done' ? 'var(--accent-success)' : state === 'active' ? 'var(--accent-primary)' : 'transparent'}; transition: all 0.3s ease;">${s.icon}</div>
                        <div class="stepper-label" style="font-size: 0.8rem; color: ${state === 'pending' ? 'var(--text-muted)' : 'var(--text-primary)'}; transition: color 0.3s ease;">${s.label}</div>
                    </div>
                `;
            }).join('');

            el.innerHTML = `
                <div class="processing-page">
                    <div class="visual-stepper" style="display: flex; justify-content: space-between; margin-bottom: 2rem; background: var(--bg-surface); padding: 1.5rem; border-radius: 12px; border: 1px solid var(--border-color); overflow-x: auto;">
                        ${stepsHtml}
                    </div>
                    ${renderProcessingPanel(videoDetail)}

                    ${pendingVideos.length > 0 ? `
                        <div class="panel" style="margin-top: 1.5rem;">
                            <div class="panel-header"><h2>⏳ Queued (${pendingVideos.length})</h2></div>
                            <div class="panel-body">${pendingVideos.map((v, i) => renderVideoCard(v, i)).join('')}</div>
                        </div>
                    ` : ''}

                    ${completedVideos.length > 0 ? `
                        <div class="panel" style="margin-top: 1.5rem;">
                            <div class="panel-header"><h2>📦 History</h2></div>
                            <div class="panel-body">${completedVideos.slice(0, 5).map((v, i) => renderVideoCard(v, i)).join('')}</div>
                        </div>
                    ` : ''}
                </div>
            `;

            // Start elapsed timer
            this._processingStartTime = this._processingStartTime || Date.now();
            this._startElapsedTimer();

            // Connect WebSocket for real-time updates
            this._lastLogStep = '';
            this._logCount = 1;
            api.connectProgress(activeVideo.id, (wsData) => {
                const progress = wsData.progress || 0;
                const step = wsData.step || 'Processing...';

                // Update progress bar
                const fill = document.getElementById('progressFill');
                const pct = document.getElementById('progressPct');
                const stepText = document.getElementById('currentStepText');
                if (fill) fill.style.width = progress + '%';
                if (pct) pct.textContent = progress + '%';
                if (stepText) stepText.textContent = step;

                // Update pipeline timeline steps
                const STEP_RANGES = [
                    { key: 'transcription', range: [0, 20] },
                    { key: 'scene_detection', range: [20, 35] },
                    { key: 'audio_analysis', range: [35, 50] },
                    { key: 'face_tracking', range: [50, 65] },
                    { key: 'clip_scoring', range: [65, 70] },
                    { key: 'clip_generation', range: [70, 85] },
                    { key: 'subtitles', range: [85, 90] },
                    { key: 'metadata', range: [90, 95] },
                    { key: 'thumbnails', range: [95, 100] },
                ];

                STEP_RANGES.forEach(s => {
                    const stepEl = document.querySelector(`[data-step="${s.key}"]`);
                    if (stepEl) {
                        stepEl.classList.remove('pipeline-step-pending', 'pipeline-step-active', 'pipeline-step-done');
                        if (progress >= s.range[1]) {
                            stepEl.classList.add('pipeline-step-done');
                            const dot = stepEl.querySelector('.step-dot');
                            if (dot) dot.innerHTML = '✓';
                            // Update badges
                            const hdr = stepEl.querySelector('.step-header');
                            if (hdr && !hdr.querySelector('.step-done-badge')) {
                                const activeBadge = hdr.querySelector('.step-active-badge');
                                if (activeBadge) activeBadge.remove();
                                hdr.insertAdjacentHTML('beforeend', '<span class="step-done-badge">Done</span>');
                            }
                        } else if (progress >= s.range[0]) {
                            stepEl.classList.add('pipeline-step-active');
                            const dot = stepEl.querySelector('.step-dot');
                            if (dot && !dot.querySelector('.step-pulse')) dot.innerHTML = '<div class="step-pulse"></div>';
                            const hdr = stepEl.querySelector('.step-header');
                            if (hdr && !hdr.querySelector('.step-active-badge')) {
                                hdr.insertAdjacentHTML('beforeend', '<span class="step-active-badge">Running</span>');
                            }
                        } else {
                            stepEl.classList.add('pipeline-step-pending');
                            const dot = stepEl.querySelector('.step-dot');
                            if (dot) dot.innerHTML = '';
                        }
                    }

                    // Update visual stepper
                    const stepperEl = document.querySelector(`[data-stepper="${s.key}"]`);
                    if (stepperEl) {
                        const iconEl = stepperEl.querySelector('.stepper-icon');
                        const labelEl = stepperEl.querySelector('.stepper-label');
                        if (progress >= s.range[1]) {
                            stepperEl.className = 'stepper-item stepper-done';
                            if (iconEl) iconEl.style.border = '2px solid var(--accent-success)';
                            if (labelEl) labelEl.style.color = 'var(--text-primary)';
                        } else if (progress >= s.range[0]) {
                            stepperEl.className = 'stepper-item stepper-active';
                            if (iconEl) iconEl.style.border = '2px solid var(--accent-primary)';
                            if (labelEl) labelEl.style.color = 'var(--text-primary)';
                        } else {
                            stepperEl.className = 'stepper-item stepper-pending';
                            if (iconEl) iconEl.style.border = '2px solid transparent';
                            if (labelEl) labelEl.style.color = 'var(--text-muted)';
                        }
                    }

                    // Update the per-row stage progress bar + header % (Issues 1+2, 2026-09-12).
                    // Every row now has a distinct visual state: visible empty track
                    // (pending), filled + live % (active), full check (done).
                    if (stepEl) {
                        const bar = stepEl.querySelector('.step-bar');
                        const headerPct = stepEl.querySelector('.step-header__pct');
                        const st = progress >= s.range[1] ? 'done'
                            : progress >= s.range[0] ? 'active' : 'pending';

                        if (bar) {
                            if (st === 'done') {
                                bar.className = 'step-bar step-bar--done';
                                bar.innerHTML = '<div class="step-bar__fill" style="width:100%"></div>';
                            } else if (wsData.status === 'failed' && st === 'active') {
                                bar.className = 'step-bar step-bar--error';
                                const reason = (wsData.error_message || 'Failed').slice(0, 60);
                                bar.innerHTML = `<div class="step-bar__fill" style="width:100%"></div><span class="step-bar__pct step-bar__err mono" title="${escapeHtml(reason)}">⚠ ${escapeHtml(reason)}</span>`;
                            } else if (st === 'active') {
                                const live = wsData.stage === s.key
                                    && typeof wsData.stage_progress === 'number'
                                    && wsData.stage_progress !== null;
                                if (live) {
                                    const sp = Math.max(0, Math.min(100, wsData.stage_progress));
                                    bar.className = 'step-bar step-bar--live';
                                    bar.innerHTML = `<div class="step-bar__fill" style="width:${sp}%"></div><span class="step-bar__pct mono">${Math.round(sp)}%</span>`;
                                } else {
                                    bar.className = 'step-bar step-bar--indeterminate';
                                    bar.innerHTML = '<div class="step-bar__fill step-bar__fill--slide"></div>';
                                }
                            } else {
                                bar.className = 'step-bar step-bar--empty';
                                bar.innerHTML = '';
                            }
                        }
                        // Header % (Issue 1): right-aligned next to the stage title.
                        if (headerPct) {
                            if (st === 'done') {
                                headerPct.className = 'step-header__pct step-header__pct--done mono';
                                headerPct.textContent = '';
                            } else if (st === 'active' && wsData.stage === s.key
                                && typeof wsData.stage_progress === 'number'
                                && wsData.stage_progress !== null) {
                                const sp = Math.max(0, Math.min(100, wsData.stage_progress));
                                headerPct.className = 'step-header__pct mono';
                                headerPct.textContent = Math.round(sp) + '%';
                            } else if (st === 'pending') {
                                headerPct.className = 'step-header__pct step-header__pct--idle mono';
                                headerPct.textContent = '0%';
                            } else {
                                // Indeterminate active or no progress data: clear the pct.
                                headerPct.className = 'step-header__pct step-header__pct--idle mono';
                                headerPct.textContent = '';
                            }
                        }
                    }
                });

                // Add activity log entries when step changes
                if (step !== this._lastLogStep) {
                    this._addLogEntry(step, progress);
                    this._lastLogStep = step;
                }

                // Handle completion
                if (wsData.status === 'completed' || wsData.status === 'failed') {
                    api.disconnectProgress(activeVideo.id);
                    this._stopElapsedTimer();
                    this._processingStartTime = null;
                    const isSuccess = wsData.status === 'completed';
                    // ONE deliberate motion: brief highlight pulse when a job
                    // finishes. Removed after the animation so it never replays.
                    const pulseHost = document.querySelector('.processing-detail-panel, .processing-page');
                    if (pulseHost) {
                        pulseHost.classList.remove('status-pulse');
                        void pulseHost.offsetWidth;            // restart animation
                        pulseHost.classList.add('status-pulse');
                    }
                    Toast.show(
                        isSuccess ? 'Processing complete! Check your clips.' : 'Processing failed.',
                        isSuccess ? 'success' : 'error',
                        6000
                    );
                    this._addLogEntry(isSuccess ? '✅ Pipeline finished!' : '❌ Pipeline failed: ' + (wsData.error_message || 'Unknown error'), progress);
                    // Refresh page after a delay
                    setTimeout(() => this.navigate(isSuccess ? 'clips' : 'processing'), 3000);
                }
            });

        } else {
            // No active processing — show queue and history
            el.innerHTML = `
                <div class="processing-page">
                    <div class="panel">
                        <div class="panel-header"><h2>Active Processing</h2></div>
                        <div class="panel-body">
                            ${renderEmptyState('loader-circle', 'No videos are currently processing',
                '<button class="btn btn-primary" onclick="App.navigate(\'upload\')">Upload a Video</button>')}
                        </div>
                    </div>
                    ${allVideos.length > 0 ? `
                        <div class="panel" style="margin-top: 1.5rem;">
                            <div class="panel-header"><h2>All Videos</h2></div>
                            <div class="panel-body">${allVideos.map((v, i) => renderVideoCard(v, i)).join('')}</div>
                        </div>
                    ` : ''}
                </div>
            `;
        }
    },

    _elapsedInterval: null,

    _startElapsedTimer() {
        this._stopElapsedTimer();
        this._elapsedInterval = setInterval(() => {
            const el = document.getElementById('elapsedTime');
            if (!el || !this._processingStartTime) return;
            const elapsed = Math.floor((Date.now() - this._processingStartTime) / 1000);
            const m = Math.floor(elapsed / 60).toString().padStart(2, '0');
            const s = (elapsed % 60).toString().padStart(2, '0');
            el.textContent = `${m}:${s}`;
        }, 1000);
    },

    _stopElapsedTimer() {
        if (this._elapsedInterval) {
            clearInterval(this._elapsedInterval);
            this._elapsedInterval = null;
        }
    },

    _addLogEntry(message, progress) {
        const feed = document.getElementById('activityFeed');
        const countEl = document.getElementById('logCount');
        if (!feed) return;
        this._logCount = (this._logCount || 0) + 1;
        const time = new Date().toLocaleTimeString();
        const entry = document.createElement('div');
        entry.className = 'activity-entry activity-entry-new';
        entry.innerHTML = `
            <span class="activity-time">${time}</span>
            <span class="activity-progress">${progress}%</span>
            <span class="activity-msg">${message}</span>
        `;
        feed.insertBefore(entry, feed.firstChild);
        if (countEl) countEl.textContent = `${this._logCount} events`;
        // Keep max 50 entries
        while (feed.children.length > 50) feed.removeChild(feed.lastChild);
    },

    // ─────────────────────────────────────────────
    // Clips Page
    // ─────────────────────────────────────────────

    async _renderClips(el) {
        const videoId = this.state.video_id || null;
        const data = await api.listClips(0, 50, videoId);
        const clips = data.clips || [];

        el.innerHTML = `
            <div class="clips-page">
                <div class="page-actions" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem;">
                    <span class="text-muted">${clips.length} clip${clips.length !== 1 ? 's' : ''} found</span>
                    <div style="display: flex; gap: 0.5rem; align-items: center;">
                        <button class="btn btn-danger" id="bulkDeleteBtn" onclick="App.bulkDeleteSelected()" style="display:none;">🗑️ Delete Selected</button>
                        ${clips.length > 0 ? '<button class="btn btn-primary" onclick="App.batchUploadToYouTube()"><i data-lucide="upload" class="btn-icon"></i> Upload All to YouTube</button>' : ''}
                    </div>
                </div>
                <div class="clips-grid" id="clipsGrid">
                    ${clips.length === 0
                ? renderEmptyState('clapperboard', 'No clips generated yet', '<button class="btn btn-primary" onclick="App.navigate(\'upload\')">Upload & Process a Video</button>')
                : clips.map(renderClipCard).join('')
            }
                </div>
            </div>
        `;
    },

    // Toggle a clip card's selected state and refresh the bulk-delete bar.
    _toggleClipSelect(cb) {
        const card = cb.closest('.clip-card');
        if (card) card.classList.toggle('clip-selected', cb.checked);
        this._updateClipDeleteBar();
    },

    _selectedClipIds() {
        return Array.from(document.querySelectorAll('.clip-select-cb:checked'))
            .map(cb => Number(cb.dataset.clipId));
    },

    _updateClipDeleteBar() {
        const n = this._selectedClipIds().length;
        const btn = document.getElementById('bulkDeleteBtn');
        if (btn) {
            btn.style.display = n > 0 ? 'inline-flex' : 'none';
            btn.textContent = `🗑️ Delete Selected (${n})`;
        }
    },

    async bulkDeleteSelected() {
        const ids = this._selectedClipIds();
        if (!ids.length) return;
        Modal.confirm('Delete Selected Clips',
            `Delete ${ids.length} selected clip${ids.length > 1 ? 's' : ''}? This cannot be undone.`,
            async () => {
                try {
                    const res = await api.bulkDeleteClips(ids);
                    const del = res.deleted || 0;
                    Toast.success(`Deleted ${del} clip${del === 1 ? '' : 's'}`);
                    this.navigate('clips');
                } catch (e) { Toast.error(e.message); }
            });
    },

    async viewClip(clipId, editIdx = null) {
        try {
            const clip = await api.getClip(clipId);
            const downloadUrl = api.getClipDownloadUrl(clipId);
            const score = clip.total_score != null ? (clip.total_score * 100).toFixed(1) : '—';
            const edits = clip.edits || [];

            // The "versions" a user can preview: the original, then every
            // labelled edit. Default stays on the latest edit when present.
            const versions = [
                { name: 'Original', path: clip.output_path, thumb: '' },
                ...edits.map(e => ({
                    name: `✨ ${e.label}`,
                    path: e.output_path,
                    thumb: e.thumbnail_path || '',
                })),
            ];
            let sel = 0;
            if (edits.length) sel = editIdx != null ? Number(editIdx) + 1 : edits.length;
            if (sel < 0 || sel >= versions.length) sel = versions.length ? versions.length - 1 : 0;
            const active = versions[sel] || { path: null };

            // Build score breakdown HTML
            let breakdownHtml = '';
            if (clip.score_breakdown_json) {
                const bd = clip.score_breakdown_json;
                const items = [
                    { label: 'Emotion', val: bd.emotion || 0, icon: '💡' },
                    { label: 'Dialogue', val: bd.dialogue || 0, icon: '💬' },
                    { label: 'Scene', val: bd.scene_change || 0, icon: '🎬' },
                    { label: 'Audio', val: bd.audio || 0, icon: '🔊' },
                    { label: 'Face', val: bd.face || 0, icon: '👤' },
                ];
                breakdownHtml = `
                    <div class="clip-score-breakdown">
                        ${items.map(it => `
                            <div class="clip-score-item">
                                <span class="score-label">${it.icon} ${it.label}</span>
                                <div class="score-bar"><div class="score-fill" style="width:${(it.val * 100).toFixed(0)}%"></div></div>
                            </div>
                        `).join('')}
                    </div>
                `;
            }

            // Version-switcher chips (Original + each labelled edit).
            const switchHtml = versions.length > 1 ? `
                <div class="clip-version-switch">
                    ${versions.map((v, i) => `
                        <button type="button" class="edit-tag ${i === sel ? 'edit-tag-active' : (i === 0 ? 'edit-tag-original' : '')}"
                            onclick="App.viewClip(${clipId}, ${i - 1})">${v.name}</button>
                    `).join('')}
                </div>` : '';

            const activeSrc = clipVideoUrl(active.path);

            Modal.open(clip.title || `Clip #${clip.clip_number}`, `
                <div class="clip-detail">
                    ${switchHtml}
                    ${activeSrc ? `
                        <video controls class="clip-video-player" preload="metadata" autoplay>
                            <source src="${activeSrc}" type="video/mp4">
                        </video>
                    ` : '<div class="clip-thumb-placeholder" style="height:400px;">🎬 No preview</div>'}

                    <div>
                        <div class="clip-detail-meta">
                            <div class="detail-row"><span>Duration</span><span>${formatDuration(clip.duration)}</span></div>
                            <div class="detail-row"><span>AI Score</span><span>🎯 ${score}%</span></div>
                            <div class="detail-row"><span>Time Range</span><span>${formatDuration(clip.start_time)} → ${formatDuration(clip.end_time)}</span></div>
                            <div class="detail-row"><span>Status</span>${renderStatusBadge(clip.status)}</div>
                        </div>

                        ${breakdownHtml}

                        ${clip.hook_sentence ? `
                            <div class="clip-hook">
                                <span class="clip-hook-label">🎣 Hook Sentence</span>
                                <p class="clip-hook-text">"${escapeHtml(clip.hook_sentence)}"</p>
                            </div>` : ''}
                        ${clip.virality_reason ? `
                            <div class="clip-virality">
                                <span class="clip-virality-label">🔥 Why It's Viral</span>
                                <p class="clip-virality-text">${escapeHtml(clip.virality_reason)}</p>
                            </div>` : ''}

                        ${clip.description ? `<p class="clip-description">${escapeHtml(clip.description)}</p>` : ''}
                        ${clip.hashtags ? `<div class="clip-hashtags">${escapeHtml(clip.hashtags)}</div>` : ''}
                    </div>
                </div>
            `, `
                <a href="${downloadUrl}" class="btn btn-primary" download>⬇️ Download Video</a>
                <a href="/api/clips/${clipId}/thumbnail" class="btn btn-secondary" download>🖼️ Download Thumbnail</a>
                <button class="btn btn-secondary" onclick="App.publishClipDialog(${clipId})"><i data-lucide="send" class="btn-icon"></i> Publish</button>
                <button class="btn btn-danger" onclick="App.deleteClip(${clipId})">🗑️ Delete</button>
            `, 'lg');
        } catch (e) { Toast.error(e.message); }
    },

    async deleteClip(clipId) {
        Modal.confirm('Delete Clip', 'Are you sure you want to delete this clip? This cannot be undone.', async () => {
            try {
                await api.deleteClip(clipId);
                Toast.success('Clip deleted');
                this.navigate('clips');
            } catch (e) { Toast.error(e.message); }
        });
    },

    async autoEditClip(clipId, options = {}) {
        Modal.open('✨ Auto-Editing Clip', `
            <div style="text-align: center; padding: 2rem;">
                <div style="font-size: 3rem; margin-bottom: 1rem; animation: pulse 2s ease-in-out infinite;">✨</div>
                <h3 style="color: var(--text-primary); margin-bottom: 0.5rem;">Adding Subtitle + Thumbnail</h3>
                <p style="color: var(--text-secondary); margin-bottom: 1.5rem;">Burning captions and generating thumbnail...</p>
                <div id="autoEditProgress">
                    <div class="progress-bar" style="margin-bottom: 1rem;">
                        <div class="progress-fill progress-animated" style="width: 0%" id="autoEditProgressBar"></div>
                    </div>
                    <div id="autoEditSteps" style="text-align: left; max-width: 360px; margin: 0 auto;">
                        <div class="auto-edit-step" id="step-captions" style="padding: 0.4rem 0; color: var(--text-muted); font-size: 0.85rem;">⏳ Burning subtitles...</div>
                        <div class="auto-edit-step" id="step-thumb" style="padding: 0.4rem 0; color: var(--text-muted); font-size: 0.85rem;">⏳ Creating thumbnail...</div>
                        <div class="auto-edit-step" id="step-final" style="padding: 0.4rem 0; color: var(--text-muted); font-size: 0.85rem;">⏳ Finishing up...</div>
                    </div>
                </div>
            </div>
        `, '', '');

        // Simulate progress animation
        const bar = document.getElementById('autoEditProgressBar');
        let progress = 0;
        const progressInterval = setInterval(() => {
            if (progress < 85) {
                progress += Math.random() * 3;
                if (bar) bar.style.width = Math.min(progress, 85) + '%';
            }
        }, 500);

        // Animate steps
        const steps = ['step-captions', 'step-thumb', 'step-final'];
        let stepIdx = 0;
        const stepInterval = setInterval(() => {
            if (stepIdx < steps.length) {
                const el = document.getElementById(steps[stepIdx]);
                if (el) {
                    el.style.color = 'var(--accent-cyan)';
                    el.textContent = '⚙️ ' + el.textContent.substring(2);
                }
                // Mark previous as done
                if (stepIdx > 0) {
                    const prev = document.getElementById(steps[stepIdx - 1]);
                    if (prev) {
                        prev.style.color = 'var(--success)';
                        prev.textContent = '✅ ' + prev.textContent.substring(3);
                    }
                }
                stepIdx++;
            }
        }, 5000);

        try {
            const data = await api.autoEditClip(clipId, options);
            const responseOk = true;

            clearInterval(progressInterval);
            clearInterval(stepInterval);

            if (responseOk) {
                // Mark all steps done
                steps.forEach(s => {
                    const el = document.getElementById(s);
                    if (el) { el.style.color = 'var(--success)'; el.textContent = '✅ ' + el.textContent.substring(2).replace(/^\s*/, ''); }
                });
                if (bar) bar.style.width = '100%';

                Toast.success('Auto-edit complete! Your Short is ready.');

                // Re-render clips list to update the thumbnail and card state
                if (App.currentPage === 'clips') {
                    App._renderClips(document.getElementById('contentArea'));
                }

                // Drop back into the studio with the freshly edited preview and
                // a result summary instead of navigating away.
                setTimeout(() => {
                    Modal.close();
                    const res = (data && data.result) ? data.result : {};
                    App._openEditorWithResult(clipId, res);
                }, 900);
            } else {
                Modal.close();
                Toast.error(data.detail || 'Auto-edit failed');
            }
        } catch (e) {
            clearInterval(progressInterval);
            clearInterval(stepInterval);
            Modal.close();
            Toast.error('Auto-edit failed: ' + e.message);
        }
    },

    // ─────────────────────────────────────────────
    // Premium AI Editor Studio
    // ─────────────────────────────────────────────

    async openAIEditor(clipId, result = null) {
        let clip = null;
        try { clip = await api.getClip(clipId); } catch (e) { Toast.error(e.message); return; }

        const effectivePath = clip.edited_output_path || clip.output_path;
        const outputName = effectivePath ? effectivePath.replace(/\\/g, '/').split('/').pop() : '';
        const previewSrc = effectivePath ? `/outputs/${outputName}` : '';
        const languages = {
            en: { name: 'English', flag: '🇺🇸' },
            ko: { name: 'Korean', flag: '🇰🇷' },
            hi: { name: 'Hindi', flag: '🇮🇳' },
            ja: { name: 'Japanese', flag: '🇯🇵' },
            zh: { name: 'Chinese', flag: '🇨🇳' },
        };
        // Caption/subtitle target languages — exactly two, per user request.
        // English keeps the audio language; Chinese translates via the
        // model-team translate specialist (qwen2.5:3b).
        const captionLangs = {
            en: { name: 'English', flag: '🇺🇸', note: 'Audio stays as-is' },
            zh: { name: '简体中文 Chinese', flag: '🇨🇳', note: 'Translate on-screen text to Chinese (Bilibili)' },
        };

        Modal.open('✨ AI Editor Studio', `
            <div class="editor-studio">
                <div class="editor-preview">
                    ${previewSrc
                ? `<video controls class="editor-player" src="${previewSrc}" preload="metadata"></video>`
                : '<div class="editor-no-video">🎬 No preview available</div>'}
                </div>
                <div class="editor-tabs">
                    <button class="editor-tab active" data-tab="edit" onclick="App._switchEditorTab('edit')">🎬 Auto-Edit</button>
                    <button class="editor-tab" data-tab="dub" onclick="App._switchEditorTab('dub')">🎙️ AI Voice-Over</button>
                </div>
                <div class="editor-panel" id="editorPanelEdit">
                    <div class="editor-section">
                        <h4><i data-lucide="wand-sparkles" class="btn-icon" style="margin-right:6px"></i> One-Click AI Edit</h4>
                        <p class="text-muted">Generates a thumbnail and burns subtitles across the whole clip. Nothing else.</p>

                        <!-- Subtitle (the only option) -->
                        <div class="editor-subsection" style="margin-top:0.3rem; padding:0.85rem 1rem; background:var(--bg-soft,#10141f); border:1px solid var(--line,#2a3040); border-radius:12px;">
                            <h5 style="margin:0 0 0.55rem 0; color:var(--accent-cyan,#22d3ee); font-size:0.95rem;">📝 Subtitle language</h5>
                            <div class="form-group" style="margin:0;">
                                <select class="form-input" id="aeSubtitleLang" onchange="App._onSubtitleLangChange()">
                                    ${Object.keys(captionLangs).map(c => `<option value="${c}">${captionLangs[c].flag} ${captionLangs[c].name}</option>`).join('')}
                                </select>
                                <small class="text-muted" id="aeLangNote">Subtitles follow every spoken line, timed to the audio.</small>
                            </div>
                        </div>

                        <!-- Subtitle style picker (Part A) -->
                        <div class="editor-subsection" style="margin-top:0.5rem; padding:0.85rem 1rem; background:var(--bg-soft,#10141f); border:1px solid var(--line,#2a3040); border-radius:12px;">
                            <h5 style="margin:0 0 0.55rem 0; color:var(--accent-cyan,#22d3ee); font-size:0.95rem;">🎨 Subtitle style <span style="color:var(--text-secondary);font-size:0.74rem;font-weight:400;">· live preview below</span></h5>

                            <!-- Preset templates -->
                            <div class="ae-style-presets" style="display:grid;grid-template-columns:repeat(3,1fr);gap:0.5rem;margin-bottom:0.7rem;">
                                <button type="button" class="ae-style-preset active" data-style="fancy" onclick="App._setSubtitleStyle('fancy')">
                                    <b>✨ Fancy</b>
                                    <span class="text-muted" style="display:block;font-size:0.72rem;margin-top:2px;">Karaoke glow</span>
                                </button>
                                <button type="button" class="ae-style-preset" data-style="normal" onclick="App._setSubtitleStyle('normal')">
                                    <b>Normal</b>
                                    <span class="text-muted" style="display:block;font-size:0.72rem;margin-top:2px;">Plain &amp; clean</span>
                                </button>
                                <button type="button" class="ae-style-preset" data-style="bold" onclick="App._setSubtitleStyle('bold')">
                                    <b>💪 Bold Caption</b>
                                    <span class="text-muted" style="display:block;font-size:0.72rem;margin-top:2px;">Big &amp; heavy</span>
                                </button>
                            </div>

                            <!-- Controls -->
                            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.6rem 0.8rem;" class="ae-style-controls">
                                <div class="form-group" style="margin:0;">
                                    <label style="font-size:0.82rem;">Font size <span id="aeStyleSizeVal">100%</span></label>
                                    <input type="range" class="form-range" id="aeStyleSize" min="60" max="180" value="100"
                                        oninput="App._setSubtitleSize(this.value)" style="width:100%;">
                                </div>
                                <div class="form-group" style="margin:0;">
                                    <label style="font-size:0.82rem;">Position</label>
                                    <select class="form-input" id="aeStylePosition" onchange="App._refreshSubtitlePreview()">
                                        <option value="bottom">Bottom</option>
                                        <option value="top">Top</option>
                                        <option value="center">Center</option>
                                    </select>
                                </div>
                                <div class="form-group" style="margin:0;">
                                    <label style="font-size:0.82rem;">Accent color</label>
                                    <input type="color" class="form-input" id="aeStyleColor" value="#FFFFFF"
                                        oninput="App._refreshSubtitlePreview()" style="height:34px;padding:3px;cursor:pointer;">
                                </div>
                                <div class="form-group" style="margin:0;">
                                    <label style="font-size:0.82rem;">Font family</label>
                                    <select class="form-input" id="aeStyleFont" onchange="App._refreshSubtitlePreview()">
                                        ${this._subtitleFontOptions()}
                                    </select>
                                </div>
                            </div>
                            <div class="form-check" style="margin-top:0.55rem;">
                                <label style="font-size:0.84rem;">
                                    <input type="checkbox" id="aeStyleOutline" checked onchange="App._refreshSubtitlePreview()">
                                    Outline / drop shadow
                                </label>
                            </div>

                            <!-- Live preview (client-side, instant) -->
                            <div class="ae-style-preview" id="aeStylePreview" style="position:relative;margin-top:0.7rem;height:96px;border-radius:10px;overflow:hidden;background:linear-gradient(180deg,#141a26,#0b0e14);">
                                <div id="aeStylePreviewText" style="position:absolute;left:0;right:0;padding:0 12px;text-align:center;">This is how your subtitles will look</div>
                            </div>
                        </div>

                        <!-- Thumbnail editor (Part B) -->
                        <div class="editor-subsection" style="margin-top:0.5rem; padding:0.85rem 1rem; background:var(--bg-soft,#10141f); border:1px solid var(--line,#2a3040); border-radius:12px;">
                            <h5 style="margin:0 0 0.55rem 0; color:var(--accent-cyan,#22d3ee); font-size:0.95rem;">🖼️ Thumbnail style <span class="text-muted" style="font-size:0.74rem;font-weight:400;">· you pick, we render</span></h5>

                            <!-- Template variants -->
                            <div class="ae-thumb-templates" style="display:grid;grid-template-columns:repeat(2,1fr);gap:0.5rem;margin-bottom:0.6rem;">
                                <button type="button" class="ae-thumb-template active" data-template="full_bleed" onclick="App._setThumbTemplate('full_bleed')">
                                    <b>🖼️ Full-bleed</b>
                                    <span class="text-muted" style="display:block;font-size:0.72rem;">Big bold title + CTA</span>
                                </button>
                                <button type="button" class="ae-thumb-template" data-template="minimal" onclick="App._setThumbTemplate('minimal')">
                                    <b>🔹 Minimal</b>
                                    <span class="text-muted" style="display:block;font-size:0.72rem;">Clean, less text</span>
                                </button>
                            </div>

                            <!-- Editable headline -->
                            <div class="form-group" style="margin:0 0 0.6rem 0;">
                                <label style="font-size:0.85rem;">Headline <span class="text-muted" style="font-weight:400;">· edit freely</span></label>
                                <input type="text" class="form-input" id="aeThumbHeadline"
                                    value="${escapeHtml(((clip && (clip.hook_sentence || clip.title)) || '').split(' ').slice(0, 18).join(' '))}"
                                    oninput="App._refreshThumbPreview()" maxlength="120"
                                    placeholder="Your thumbnail title">
                            </div>

                            <!-- Frame filmstrip -->
                            <label style="font-size:0.85rem;">Frame <span class="text-muted" style="font-weight:400;">· click to override the auto-pick</span></label>
                            <div class="ae-thumb-frames" id="aeThumbFrames" style="display:flex;gap:0.45rem;margin:0.4rem 0 0.25rem 0;overflow-x:auto;padding-bottom:0.3rem;min-height:66px;">
                                <span class="text-muted" style="font-size:0.8rem;">Loading frames…</span>
                            </div>
                            <small class="text-muted" id="aeThumbFrameNote" style="display:block;margin-top:0.15rem;">No frame chosen — the best frame is picked automatically.</small>

                            <!-- Live preview (fast client-side approximation) -->
                            <div class="ae-thumb-preview" id="aeThumbPreview" style="margin-top:0.6rem;border-radius:10px;overflow:hidden;background:#000;aspect-ratio:9/16;max-height:260px;position:relative;">
                                <div class="text-muted ae-thumb-preview-empty" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:1rem;font-size:0.8rem;">Frame preview appears here once frames load (or after Generate).</div>
                            </div>
                        </div>

                        <!-- YOUR OWN MUSIC — Audio Remix -->
                        <div class="editor-subsection" id="aeAudioRemix" style="margin-top:0.5rem; padding:0.85rem 1rem; background:var(--bg-soft,#10141f); border:1px solid var(--line,#2a3040); border-radius:12px;">
                            <h5 style="margin:0 0 0.4rem 0; color:var(--accent-cyan,#22d3ee); font-size:0.95rem;"><i data-lucide="music" class="btn-icon" style="margin-right:6px"></i> Audio & Music</h5>
                            <small class="text-muted" style="display:block; margin-bottom:0.6rem;">Adjust voice volume, remove background music, or swap with custom / copyright-free tracks.</small>
                            <div class="form-group" style="margin-bottom:0.5rem;">
                                <label style="font-size:0.85rem;">Audio mode</label>
                                <select class="form-input" id="aeAudioMode" onchange="App._onAudioModeChange()">
                                    <option value="keep_original" selected>Keep original audio</option>
                                    <option value="mute_music">Mute background music (voice only)</option>
                                    <option value="replace_music">Replace with custom music</option>
                                    <option value="copyright_free">Auto-swap with copyright-free music</option>
                                </select>
                            </div>
                            <div id="aeMusicUploadRow" style="display:none; margin-bottom:0.45rem;">
                                <label class="btn btn-sm btn-secondary" style="cursor:pointer; display:inline-block; padding:0.3rem 0.8rem; font-size:0.82rem;">
                                    Choose music file (.mp3 / .wav / .m4a)
                                    <input type="file" id="aeMusicFile" accept="audio/*" style="display:none;"
                                        onchange="App._onMusicFileChosen(this)">
                                </label>
                                <span id="aeMusicName" class="text-muted" style="margin-left:0.4rem; font-size:0.82rem;"></span>
                            </div>
                            <div id="aeVocalsGainRow" style="margin-bottom:0.45rem;">
                                <label style="font-size:0.85rem;">Voice volume <span id="aeVocalsVolVal">100%</span></label>
                                <input type="range" class="form-range" id="aeVocalsGain" min="0" max="200" value="100"
                                    oninput="document.getElementById('aeVocalsVolVal').textContent=this.value+'%'"
                                    style="width:100%;">
                            </div>
                            <div id="aeMusicGainRow" style="display:none;">
                                <label style="font-size:0.85rem;">Music volume <span id="aeMusicVolVal">100%</span></label>
                                <input type="range" class="form-range" id="aeMusicGain" min="0" max="200" value="100"
                                    oninput="document.getElementById('aeMusicVolVal').textContent=this.value+'%'"
                                    style="width:100%;">
                            </div>
                            <small id="aeAudioNote" class="text-muted" style="display:block; margin-top:0.3rem;"></small>
                        </div>

                        <button class="btn btn-accent btn-lg editor-cta" onclick="App._runAutoEdit(${clipId})" style="margin-top:0.75rem;">✨ Generate Subtitle + Thumbnail</button>

                        <!-- Result -->
                        <div id="aeResult" style="display:none; margin-top:0.75rem; padding:0.85rem 1rem; background:var(--bg-soft,#10141f); border:1px solid var(--accent-success,#22c55e); border-radius:12px;">
                            <h5 style="margin:0 0 0.4rem 0; color:var(--accent-success,#22c55e); font-size:0.95rem;">✅ Edit Complete</h5>
                            <div id="aeResultMeta" style="font-size:0.82rem; color:var(--text-secondary);"></div>
                        </div>
                    </div>
                </div>
                <div class="editor-panel" id="editorPanelDub" style="display:none;">
                    <div class="editor-section">
                        <h4>🎙️ Multilingual Neural Voice-Over</h4>
                        <p class="text-muted">Dub your clip into English, Korean, Hindi, Japanese or Chinese with premium neural voices (edge-tts).</p>
                        <div class="editor-row">
                            <div class="form-group">
                                <label>Language</label>
                                <select class="form-input" id="dubLanguage" onchange="App._populateVoicesFor(this.value)">
                                    ${Object.keys(languages).map(c => `<option value="${c}">${languages[c].flag} ${languages[c].name}</option>`).join('')}
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Voice</label>
                                <select class="form-input" id="dubVoice"></select>
                            </div>
                        </div>
                        <div class="editor-row">
                            <div class="form-group">
                                <label>Audio mode</label>
                                <select class="form-input" id="dubMode">
                                    <option value="replace">◀ Replace original audio</option>
                                    <option value="mix">▶ Mix on top (commentary)</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Voice-over volume <span id="dubVolVal">100%</span></label>
                                <input type="range" class="form-range" id="dubVolume" min="0" max="200" value="100" oninput="document.getElementById('dubVolVal').textContent=this.value+'%'">
                            </div>
                        </div>
                        <div class="form-check">
                            <label><input type="checkbox" id="dubTranslate" checked> Translate script (needs Ollama running)</label>
                        </div>
                        <button class="btn btn-primary btn-lg editor-cta" onclick="App._runDub(${clipId})">🎙️ Generate AI Voice-Over</button>
                        <div id="dubStatus"></div>
                        <div id="dubHistory"></div>
                    </div>
                </div>
            </div>
        `, `
            <button class="btn btn-secondary" onclick="Modal.close()">Close</button>
        `, 'lg');

        this._loadEditorVoices();
        this._loadDubHistory(clipId);
        this._loadTeamStatus();
        this._onSubtitleLangChange();
        this._onCaptionsToggle();
        this._pendingMusicFile = null;
        this._onAudioModeChange();
        this._initSubtitleStyle();
        this._initThumbEditor(clipId);

        // After an auto-edit, surface the result banner with generated copy.
        if (result) this._renderEditResult(result);
    },

    _openEditorWithResult(clipId, result) {
        this.openAIEditor(clipId, result);
    },

    _renderEditResult(result) {
        const box = document.getElementById('aeResult');
        if (!box) return;
        box.style.display = 'block';
        const title = result.title || '';
        const editLabel = result.edit_label || '';
        const thumb = result.thumbnail_path;
        const thumbName = thumb ? thumb.replace(/\\/g, '/').split('/').pop() : null;
        let html = '';
        if (editLabel) html += `<p><span class="edit-tag edit-tag-active">✨ ${escapeHtml(editLabel)}</span></p>`;
        if (title) html += `<p><b style="color:var(--text-primary);">Title:</b> ${escapeHtml(title)}</p>`;
        if (thumbName) html += `<p><img src="/thumbnails/${thumbName}" alt="thumbnail" style="max-height:180px;border-radius:8px;margin-top:0.25rem;"></p>`;
        box.innerHTML = html || '<p>Edit finished — playback below.</p>';
    },

    _switchEditorTab(tab) {
        document.querySelectorAll('.editor-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
        const editPanel = document.getElementById('editorPanelEdit');
        const dubPanel = document.getElementById('editorPanelDub');
        if (editPanel) editPanel.style.display = tab === 'edit' ? 'block' : 'none';
        if (dubPanel) dubPanel.style.display = tab === 'dub' ? 'block' : 'none';
    },

    async _loadEditorVoices() {
        try {
            const res = await api.getVoices();
            this._voicesByLang = {};
            (res.languages || []).forEach(l => { this._voicesByLang[l.code] = l; });
            const sel = document.getElementById('dubLanguage');
            if (sel) this._populateVoicesFor(sel.value);
        } catch (e) {
            // Voices unavailable — fall back to a generic default
            const dsel = document.getElementById('dubVoice');
            if (dsel) dsel.innerHTML = '<option value="">Default voice</option>';
        }
    },

    _populateVoicesFor(code) {
        const sel = document.getElementById('dubVoice');
        if (!sel) return;
        const lang = this._voicesByLang && this._voicesByLang[code];
        if (!lang || !lang.voices || !lang.voices.length) {
            sel.innerHTML = '<option value="">Default voice</option>';
            return;
        }
        sel.innerHTML = lang.voices.map(v => `<option value="${v.id}">${escapeHtml(v.label)}</option>`).join('');
        if (lang.default_voice) {
            const opt = sel.querySelector(`option[value="${lang.default_voice}"]`);
            if (opt) opt.selected = true;
        }
    },

    // Show/hide caption controls when the burn checkbox is toggled.
    _onCaptionsToggle() {
        const burn = document.getElementById('aeCaptions') ? document.getElementById('aeCaptions').checked : true;
        const ctl = document.getElementById('aeCaptionControls');
        if (ctl) ctl.style.display = burn ? 'block' : 'none';
    },

    _onSubtitleLangChange() {
        const sel = document.getElementById('aeSubtitleLang');
        const note = document.getElementById('aeLangNote');
        if (sel) {
            const lang = sel.value;
            if (note) {
                const notes = {
                    en: 'Audio stays as-is; only the on-screen text is translated.',
                    zh: 'Translate captions to 简体中文 — great for Bilibili.',
                };
                note.textContent = notes[lang] || '';
            }
        }
    },

    // ── Subtitle style picker (Part A) ────────────────────────────────────
    // Preset-relevant font/copy sizes are tuned so the LIVE PREVIEW (CSS) and
    // the real FFmpeg drawtext burn agree closely.  Size is a 60–180% slider
    // mapped to a backend `caption_size_scale` of 0.6–1.8.  Fonts are a curated
    // list of fonts that exist on every Windows box (see backend _FONT_MAP), so
    // the preview font and the burned font are the same face — no download, no
    // mismatch.

    // Confirmed-available Windows fonts (must mirror backend services/auto_editor._FONT_MAP).
    _subtitleFonts() {
        return ['Arial', 'Arial Black', 'Arial Bold', 'Verdana', 'Tahoma', 'Impact', 'Segoe UI', 'Courier New', 'Times New Roman'];
    },

    _subtitleFontOptions() {
        return this._subtitleFonts().map(f => `<option value="${f}">${escapeHtml(f)}</option>`).join('');
    },

    // Defaults for each preset template (live-preview intent, not exhaustive).
    _subtitlePresetDefaults() {
        return {
            fancy: { size: 120, position: 'bottom', color: '#FFFFFF', font: 'Arial', outline: true, effect: 'glow' },
            normal: { size: 95, position: 'bottom', color: '#FFFFFF', font: 'Verdana', outline: true, effect: 'plain' },
            bold: { size: 155, position: 'center', color: '#FFD700', font: 'Arial Black', outline: true, effect: 'karaoke' },
        };
    },

    // Restore last-used style (from the previous session) or the fancy default.
    _initSubtitleStyle() {
        let saved = null;
        try { saved = JSON.parse(localStorage.getItem('aiclipper.subtitleStyle.v1') || 'null'); }
        catch (e) { saved = null; }
        const preset = saved && saved.preset ? saved.preset : 'fancy';
        this._applySubtitlePreset(preset, saved || null);
        this._refreshSubtitlePreview();
    },

    _setSubtitleStyle(name) {
        const defaults = this._subtitlePresetDefaults()[name];
        if (!defaults) return;
        this._applySubtitlePreset(name, null);
        this._refreshSubtitlePreview();
    },

    // Push a preset (or the last-saved overrides) onto the DOM controls.
    _applySubtitlePreset(name, overrides) {
        const d = this._subtitlePresetDefaults()[name] || this._subtitlePresetDefaults().fancy;
        document.querySelectorAll('.ae-style-preset').forEach(btn =>
            btn.classList.toggle('active', btn.dataset.style === name));
        const setVal = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.value = val; };
        setVal('aeStyleSize', overrides && overrides.size != null ? overrides.size : d.size);
        setVal('aeStylePosition', overrides && overrides.position ? overrides.position : d.position);
        setVal('aeStyleColor', overrides && overrides.color ? overrides.color : d.color);
        setVal('aeStyleFont', overrides && overrides.font ? overrides.font : d.font);
        const outline = overrides && overrides.outline != null ? overrides.outline : d.outline;
        const obx = document.getElementById('aeStyleOutline');
        if (obx) obx.checked = !!outline;
        const lbl = document.getElementById('aeStyleSizeVal');
        if (lbl) lbl.textContent = `${document.getElementById('aeStyleSize').value}%`;
        this._activeStylePreset = name;
    },

    _setSubtitleSize(val) {
        const lbl = document.getElementById('aeStyleSizeVal');
        if (lbl) lbl.textContent = `${val}%`;
        this._refreshSubtitlePreview();
    },

    // Any control change => re-render preview + remember the current style.
    _refreshSubtitlePreview() {
        this._applySubtitleStylePreview();
        try {
            localStorage.setItem('aiclipper.subtitleStyle.v1', JSON.stringify(this._subtitleStyleOptions()));
        } catch (e) { /* storage unavailable — fine */ }
    },

    // Read the current controls into the object the backend + preview consume.
    _subtitleStyleOptions() {
        const size = parseInt(document.getElementById('aeStyleSize').value || '100', 10);
        const color = document.getElementById('aeStyleColor').value || '#FFFFFF';
        const font = document.getElementById('aeStyleFont').value || 'Arial';
        const position = document.getElementById('aeStylePosition').value || 'bottom';
        const outline = document.getElementById('aeStyleOutline') ? document.getElementById('aeStyleOutline').checked : true;
        return {
            preset: this._activeStylePreset || 'fancy',
            size,          // percent (60–180) — UI only
            sizeScale: +(size / 100).toFixed(2),  // 0.6–1.8 for the backend
            color, font, position, outline,
        };
    },

    // Client-side caption preview.  Renders the same sample line with CSS so
    // size / color / position / outline / font changes are visible INSTANTLY
    // (no server round-trip).  This is the "live preview" the spec asks for.
    _applySubtitleStylePreview() {
        const s = this._subtitleStyleOptions();
        const el = document.getElementById('aeStylePreviewText');
        const box = document.getElementById('aeStylePreview');
        if (!el || !box) return;

        const preset = this._subtitlePresetDefaults()[s.preset] || {};
        // fontSize scale ~ matches the backend drawtext size_scale multiplier.
        const base = 34; // preview base px
        const px = Math.round(base * (s.size / 100));
        el.style.fontSize = `${px}px`;
        el.style.fontFamily = `"${s.font}", sans-serif`;
        el.style.color = s.color;
        el.style.fontWeight = (s.font === 'Arial Black' || s.font === 'Arial Bold' || s.font === 'Impact') ? '700' : '600';

        // position: bottom/top/center of the preview box (mirrors backend y=).
        el.style.top = s.position === 'top' ? '16px'
            : s.position === 'center' ? '50%'
                : 'auto';
        el.style.bottom = s.position === 'bottom' ? '12px' : 'auto';
        el.style.transform = s.position === 'center' ? 'translateY(-50%)' : 'none';

        // outline: drop shadow (+ strong border look) or a faint inner stroke.
        if (s.outline) {
            el.style.textShadow = '0 0 6px rgba(0,0,0,0.85), 0 3px 8px rgba(0,0,0,0.7)';
            el.style.WebkitTextStroke = '1.5px rgba(0,0,0,0.9)';
        } else {
            el.style.textShadow = '0 0 4px rgba(0,0,0,0.5)';
            el.style.WebkitTextStroke = '0px';
        }

        // per-preset flourish so the three templates look distinct in preview
        el.classList.remove('ae-fx-glow', 'ae-fx-karaoke');
        if (preset.effect === 'glow') el.classList.add('ae-fx-glow');
        else if (preset.effect === 'karaoke') el.classList.add('ae-fx-karaoke');
    },

    // ── Thumbnail editor (Part B) ─────────────────────────────────────────
    // Lets the user pick a template variant, edit the headline (prefilled from
    // the clip's hook_sentence), and override the auto-picked frame via a
    // scored filmstrip.  The live preview is a fast client-side approximation
    // (selected frame + overlaid headline in CSS) so edits are visible instantly;
    // the real compositor render happens once Generate is clicked.

    _initThumbEditor(clipId) {
        this._thumbTemplate = 'full_bleed';
        this._thumbFrameIndex = null;   // null = auto best frame
        this._thumbCandidates = [];
        this._thumbSelectedDataUri = null;
        this._loadThumbCandidates(clipId);
    },

    async _loadThumbCandidates(clipId) {
        const box = document.getElementById('aeThumbFrames');
        if (!box) return;
        try {
            const res = await api.thumbnailCandidates(clipId);
            const list = (res && res.candidates) || [];
            this._thumbCandidates = list;
            if (!list.length) {
                box.innerHTML = '<span class="text-muted" style="font-size:0.8rem;">No candidate frames available.</span>';
                return;
            }
            box.innerHTML = '';
            list.forEach(c => {
                const el = document.createElement('button');
                el.type = 'button';
                el.className = 'ae-thumb-frame' + (c.best ? ' best' : '');
                el.title = `${c.index} @ ${c.time}s · score ${(c.score || 0).toFixed(2)}${c.best ? ' (auto best)' : ''}`;
                el.onclick = () => App._selectThumbFrame(c.index);
                el.style.backgroundImage = c.data_uri ? `url("${c.data_uri}")` : '';
                el.style.backgroundSize = 'cover';
                el.style.backgroundPosition = 'center';
                el.innerHTML = `<span class="ae-thumb-frame-t"></span>`;
                box.appendChild(el);
            });
            // If the user hasn't chosen, keep "best" as the preview frame.
            if (this._thumbFrameIndex === null) {
                const best = list.find(c => c.best) || list[0];
                if (best) { this._thumbSelectedDataUri = best.data_uri; this._refreshThumbPreview(); }
            }
            const note = document.getElementById('aeThumbFrameNote');
            if (note) this._updateThumbFrameNote();
        } catch (e) {
            box.innerHTML = `<span class="text-muted" style="font-size:0.8rem;">Could not load frames: ${escapeHtml(e.message)}</span>`;
        }
    },

    _setThumbTemplate(name) {
        this._thumbTemplate = name === 'minimal' ? 'minimal' : 'full_bleed';
        document.querySelectorAll('.ae-thumb-template').forEach(b =>
            b.classList.toggle('active', b.dataset.template === this._thumbTemplate));
        this._refreshThumbPreview();
    },

    _selectThumbFrame(index) {
        if (this._thumbFrameIndex === index) { index = null; } // toggle off
        this._thumbFrameIndex = index;
        const cand = index === null
            ? (this._thumbCandidates.find(c => c.best) || this._thumbCandidates[0])
            : (this._thumbCandidates.find(c => c.index === index) || null);
        this._thumbSelectedDataUri = cand ? cand.data_uri : '';
        document.querySelectorAll('.ae-thumb-frame').forEach(b => {
            const isSelected = b.title && b.title.indexOf(`${this._thumbFrameIndex} @`) === 0;
            b.classList.toggle('selected', index !== null && isSelected);
        });
        this._updateThumbFrameNote();
        this._refreshThumbPreview();
    },

    _updateThumbFrameNote() {
        const note = document.getElementById('aeThumbFrameNote');
        if (!note) return;
        note.innerHTML = this._thumbFrameIndex === null
            ? 'No frame chosen — the best frame is picked automatically.'
            : `Frame <b>${this._thumbFrameIndex}</b> selected — this exact frame will be used.`;
    },

    // Fast client-side preview: show the chosen frame with the headline overlaid per template.
    _refreshThumbPreview() {
        const box = document.getElementById('aeThumbPreview');
        if (!box) return;
        const headline = (document.getElementById('aeThumbHeadline')?.value || '').trim() || 'Your Title Here';
        const empty = box.querySelector('.ae-thumb-preview-empty');
        const frameUri = this._thumbSelectedDataUri;
        if (!frameUri) {
            if (empty) empty.style.display = '';
            box.querySelectorAll('.ae-thumb-preview-frame').forEach(n => n.remove());
            return;
        }
        if (empty) empty.style.display = 'none';
        let f = box.querySelector('.ae-thumb-preview-frame');
        if (!f) { f = document.createElement('div'); f.className = 'ae-thumb-preview-frame'; box.appendChild(f); }
        f.style.backgroundImage = `url("${frameUri}")`;
        f.style.backgroundSize = 'cover';
        f.style.backgroundPosition = 'center';
        // Headline overlay per template
        const minimal = this._thumbTemplate === 'minimal';
        f.innerHTML = `
            <div class="ae-tp-scrim"></div>
            <div class="ae-tp-text ${minimal ? 'minimal' : 'full'}">${escapeHtml(headline)}</div>`;
    },

    // Build the thumbnail_style dict sent to the backend on Generate.
    _thumbStyleOptions() {
        const headline = (document.getElementById('aeThumbHeadline')?.value || '').trim() || null;
        const style = {
            template: this._thumbTemplate || 'full_bleed',
        };
        if (headline) style.headline = headline;
        if (this._thumbFrameIndex !== null && this._thumbFrameIndex !== undefined) {
            style.frame_index = this._thumbFrameIndex;
        }
        return style;
    },

    // Store the chosen music file + show its name (upload happens on Generate).
    _onMusicFileChosen(input) {
        const file = input.files && input.files[0];
        const nameEl = document.getElementById('aeMusicName');
        if (nameEl) nameEl.textContent = file ? `${file.name}` : '';
        if (file) this._pendingMusicFile = file;
    },

    // Show/hide the "your music" upload + volume controls based on the mode.
    _onAudioModeChange() {
        const mode = document.getElementById('aeAudioMode') ? document.getElementById('aeAudioMode').value : 'keep_original';
        const uploadRow = document.getElementById('aeMusicUploadRow');
        const vocalsGainRow = document.getElementById('aeVocalsGainRow');
        const musicGainRow = document.getElementById('aeMusicGainRow');
        const note = document.getElementById('aeAudioNote');

        if (uploadRow) uploadRow.style.display = (mode === 'replace_music') ? 'block' : 'none';
        if (vocalsGainRow) vocalsGainRow.style.display = 'block';
        if (musicGainRow) musicGainRow.style.display = (mode === 'replace_music' || mode === 'copyright_free') ? 'block' : 'none';

        if (note) {
            if (mode === 'keep_original') note.textContent = 'Original audio preserved. You can adjust the overall volume.';
            else if (mode === 'mute_music') note.textContent = 'Background music is removed; dialogue and voice are kept crystal clear.';
            else if (mode === 'copyright_free') note.textContent = 'Automatically removes copyrighted music and layers a royalty-free ambient track.';
            else note.textContent = 'Upload your music file above — your track replaces background audio and auto-ducks under speech.';
        }
    },

    async _runAutoEdit(clipId) {
        // Subtitle + thumbnail are always generated. The user can also choose
        // how to handle the audio — including uploading their OWN music.
        const rawMode = document.getElementById('aeAudioMode') ? document.getElementById('aeAudioMode').value : 'keep_original';
        const backendMode = (rawMode === 'copyright_free') ? 'replace_music' : rawMode;

        const vocalsGainEl = document.getElementById('aeVocalsGain');
        const musicGainEl = document.getElementById('aeMusicGain');

        const options = {
            with_captions: true,
            subtitle_language: document.getElementById('aeSubtitleLang') ? document.getElementById('aeSubtitleLang').value : 'en',
            audio_mode: backendMode,
            vocals_gain: vocalsGainEl ? (parseInt(vocalsGainEl.value, 10) / 100) : 1.0,
            music_gain: musicGainEl ? (parseInt(musicGainEl.value, 10) / 100) : 1.0,
        };

        // Part A: apply the chosen subtitle STYLE to the real caption burn.
        const style = this._subtitleStyleOptions();
        options.subtitle_style = style.preset;
        options.caption_font = style.font;
        options.caption_color = style.color;
        options.caption_size_scale = style.sizeScale;
        options.caption_position = style.position;
        options.caption_outline = style.outline;

        // Part B: thumbnail style (template/headline/frame override).
        options.thumbnail_style = this._thumbStyleOptions();

        // If the user picked a fresh music file, upload it first so the backend
        // has a path to swap in during the render.
        if (rawMode === 'replace_music' && this._pendingMusicFile) {
            try {
                const up = await api.uploadClipMusic(clipId, this._pendingMusicFile);
                options.replacement_track = up.replacement_track;
                this._pendingMusicFile = null;
                Toast.success('Your music uploaded — applying it now.');
            } catch (e) {
                Toast.error('Could not upload your music: ' + e.message);
                return;
            }
        }
        await this.autoEditClip(clipId, options);
    },

    // Fetch the active model-team roster and show model names per role.
    async _loadTeamStatus() {
        const box = document.getElementById('aeTeamStatus');
        if (!box) return;
        try {
            const res = await api.teamStatus && (await api.teamStatus());
            if (res && res.roster) {
                const rows = Object.entries(res.roster).map(([role, model]) =>
                    `<span><b style="color:var(--text-primary);text-transform:capitalize;">${role}</b> → <code>${escapeHtml(model)}</code></span>`
                ).join('');
                box.innerHTML = rows;
            } else {
                box.innerHTML = '<span>Model team configured — specialists load on demand.</span>';
            }
        } catch (e) {
            box.innerHTML = '<span>Model team status unavailable.</span>';
        }
    },

    async _runDub(clipId) {
        const status = document.getElementById('dubStatus');
        const btn = document.querySelector('#editorPanelDub .editor-cta');
        const language = document.getElementById('dubLanguage').value;
        const voice = document.getElementById('dubVoice').value;
        const mode = document.getElementById('dubMode').value;
        const volume = parseInt(document.getElementById('dubVolume').value || '100', 10) / 100;
        const translate = document.getElementById('dubTranslate').checked;

        if (btn) { btn.disabled = true; btn.textContent = `⏳ Generating ${language.toUpperCase()} voice-over...`; }
        if (status) status.innerHTML = '<p style="color:var(--accent-cyan);">🎙️ Synthesizing speech & muxing audio (this can take 10-30s)...</p>';

        try {
            const res = await api.dubClip(clipId, {
                clip_id: clipId, language, voice, mode, mix_volume: volume, translate,
            });
            if (status) status.innerHTML = `<p style="color:var(--accent-success);">✅ ${escapeHtml(res.message || 'Voice-over ready!')}</p>`;
            Toast.success(`Voice-over ready in ${res.language.toUpperCase()}`);
            this._showDubbedPreview(res);
            this._loadDubHistory(clipId);
        } catch (e) {
            if (status) status.innerHTML = `<p style="color:var(--accent-danger);">❌ ${escapeHtml(e.message)}</p>`;
            Toast.error(e.message);
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = '🎙️ Generate AI Voice-Over'; }
        }
    },

    _showDubbedPreview(res) {
        let status = document.getElementById('dubStatus');
        if (!status || !res.output_path) return;
        const name = res.output_path.replace(/\\/g, '/').split('/').pop();
        status.insertAdjacentHTML('beforeend', `
            <div class="dubbed-result">
                <video controls class="editor-player" src="/outputs/${name}" preload="metadata"></video>
                <div class="editor-row">
                    <a href="/outputs/${name}" class="btn btn-sm btn-primary" download>⬇️ Download ${escapeHtml(res.language.toUpperCase())}</a>
                    <a href="/api/clips/${res.clip_id}/download" class="btn btn-sm btn-secondary">◀ Download Original</a>
                </div>
            </div>
        `);
    },

    async _loadDubHistory(clipId) {
        const container = document.getElementById('dubHistory');
        if (!container) return;
        try {
            const res = await api.getClipVoiceovers(clipId);
            const items = res.voiceovers || [];
            if (items.length === 0) { container.innerHTML = ''; return; }
            container.innerHTML = `
                <div class="dub-history">
                    <h5>🔁 Previous renders</h5>
                    ${items.map(v => {
                const name = v.output_path ? v.output_path.replace(/\\/g, '/').split('/').pop() : '';
                const badge = v.status === 'completed' ? 'success'
                    : v.status === 'failed' ? 'danger' : 'info';
                return `
                            <div class="dub-history-item">
                                <span>🗣️ ${escapeHtml((v.language || '?').toUpperCase())} · ${escapeHtml((v.voice || '').split('-').pop() || 'voice')}</span>
                                <span class="badge badge-${badge}">${escapeHtml(v.status)}</span>
                                ${name ? `<a href="/outputs/${name}" download class="btn btn-sm btn-accent">⬇️</a>` : ''}
                            </div>`;
            }).join('')}
                </div>`;
        } catch (e) { container.innerHTML = ''; }
    },

    async batchUploadToYouTube() {
        let vid = this.state.video_id;
        if (!vid) {
            try {
                const data = await api.listClips(0, 1);
                if (data.clips && data.clips.length > 0) vid = data.clips[0].video_id;
            } catch (e) { }
        }
        if (!vid) { Toast.error('No video found'); return; }

        Modal.open('Upload All Clips to YouTube', `
            <div style="text-align: center; padding: 1rem;">
                <p style="color: var(--text-secondary); margin-bottom: 1.5rem; line-height: 1.6;">This will upload all completed clips to YouTube as Shorts with their AI-generated titles, descriptions, and hashtags.</p>
                <div style="margin-bottom: 1.5rem;">
                    <label style="color: var(--text-secondary); font-size: 0.9rem;">Privacy Setting:
                        <select id="ytPrivacy" class="form-input" style="margin-left: 0.5rem; width: auto; display: inline-block; min-width: 140px;">
                            <option value="public">🌍 Public</option>
                            <option value="unlisted">Unlisted</option>
                            <option value="private">🔒 Private</option>
                        </select>
                    </label>
                </div>
                <div id="batchUploadStatus"></div>
            </div>
        `, `
            <button class="btn btn-secondary" onclick="Modal.close()">Cancel</button>
            <button class="btn btn-primary" id="startBatchUploadBtn" onclick="App._startBatchUpload(${vid})"><i data-lucide="upload" class="btn-icon"></i> Start Upload</button>
        `, 'lg');
    },

    async _startBatchUpload(videoId) {
        const btn = document.getElementById('startBatchUploadBtn');
        const status = document.getElementById('batchUploadStatus');
        const privacy = document.getElementById('ytPrivacy')?.value || 'public';
        if (btn) { btn.disabled = true; btn.textContent = '⏳ Uploading...'; }
        status.innerHTML = '<p style="color: var(--accent-cyan);">Starting batch upload...</p>';
        try {
            const res = await fetch(`/api/publish/batch/${videoId}?privacy=${privacy}`, { method: 'POST' });
            const data = await res.json();
            if (res.ok) {
                status.innerHTML = `<p style="color: var(--success);">✅ ${data.queued || 0} clips queued for YouTube upload!</p>`;
                Toast.success(`${data.queued || 0} clips queued for YouTube!`);
            } else {
                status.innerHTML = `<p style="color: var(--error);">❌ ${data.detail || 'Upload failed'}</p>`;
                Toast.error(data.detail || 'Upload failed');
            }
        } catch (e) {
            status.innerHTML = `<p style="color: var(--error);">❌ ${e.message}</p>`;
            Toast.error(e.message);
        }
        if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="upload" class="btn-icon"></i> Start Upload'; }
    },

    // ─────────────────────────────────────────────
    // Publishing Page
    // ─────────────────────────────────────────────

    async _renderPublishing(el) {
        const data = await api.listClips(0, 100);
        const clips = (data.clips || []).filter(c => c.status === 'completed');

        el.innerHTML = `
            <div class="publishing-page">
                <div class="panel">
                    <div class="panel-header"><h2>Connected Accounts</h2></div>
                    <div class="panel-body accounts-grid">
                        <div class="account-card">
                            <span class="account-icon"><svg class="brand-mark" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M23.5 6.19a3.02 3.02 0 0 0-2.12-2.14C19.5 3.55 12 3.55 12 3.55s-7.5 0-9.38.5A3.02 3.02 0 0 0 .5 6.19 31.6 31.6 0 0 0 0 12a31.6 31.6 0 0 0 .5 5.81 3.02 3.02 0 0 0 2.12 2.14c1.88.5 9.38.5 9.38.5s7.5 0 9.38-.5a3.02 3.02 0 0 0 2.12-2.14A31.6 31.6 0 0 0 24 12a31.6 31.6 0 0 0-.5-5.81zM9.55 15.57V8.43L15.82 12z"/></svg></span>
                            <h3>YouTube</h3>
                            <p class="text-muted">Upload Shorts via YouTube Data API</p>
                            <button class="btn btn-sm btn-primary" onclick="App._showYouTubeSetup()" style="margin-top: 0.5rem;"><i data-lucide="plug" class="btn-icon"></i> Connect / Setup</button>
                        </div>
                        <div class="account-card">
                            <span class="account-icon"><svg class="brand-mark" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M24 12.07C24 5.4 18.63 0 12 0S0 5.4 0 12.07C0 18.1 4.39 23.09 10.13 24v-8.44H7.08v-3.49h3.05V9.41c0-3.02 1.79-4.7 4.53-4.7 1.31 0 2.68.24 2.68.24v2.97h-1.5c-1.5 0-1.96.93-1.96 1.89v2.26h3.32l-.53 3.49h-2.79V24C19.61 23.09 24 18.1 24 12.07z"/></svg></span>
                            <h3>Facebook</h3>
                            <p class="text-muted">Upload Reels via Graph API</p>
                            <span class="badge badge-warning">Coming Soon</span>
                        </div>
                        <div class="account-card">
                            <span class="account-icon"><span class='account-icon account-icon--soon'><svg class="brand-mark" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12.53.02C13.84 0 15.14.01 16.44 0c.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.2-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.15 1.07-.14 1.61.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z"/></svg></span></span>
                            <h3>TikTok</h3>
                            <p class="text-muted">Coming soon</p>
                            <span class="badge badge-default">Planned</span>
                        </div>
                        <div class="account-card">
                            <span class="account-icon"><span class='account-icon account-icon--soon'><svg class="brand-mark" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2.16c3.2 0 3.58.01 4.85.07 1.17.05 1.8.25 2.23.41.56.22.96.48 1.38.9.42.42.68.82.9 1.38.16.42.36 1.06.41 2.23.06 1.27.07 1.65.07 4.85s-.01 3.58-.07 4.85c-.05 1.17-.25 1.8-.41 2.23-.22.56-.48.96-.9 1.38-.42.42-.82.68-1.38.9-.42.16-1.06.36-2.23.41-1.27.06-1.65.07-4.85.07s-3.58-.01-4.85-.07c-1.17-.05-1.8-.25-2.23-.41-.56-.22-.96-.48-1.38-.9-.42-.42-.68-.82-.9-1.38-.16-.42-.36-1.06-.41-2.23-.06-1.27-.07-1.65-.07-4.85s.01-3.58.07-4.85c.05-1.17.25-1.8.41-2.23.22-.56.48-.96.9-1.38.42-.42.82-.68 1.38-.9.42-.16 1.06-.36 2.23-.41 1.27-.06 1.65-.07 4.85-.07zM12 0C8.74 0 8.33.01 7.05.07 5.78.13 4.9.33 4.14.63c-.79.31-1.46.72-2.13 1.38C1.35 2.68.94 3.35.63 4.14.33 4.9.13 5.78.07 7.05.01 8.33 0 8.74 0 12s.01 3.67.07 4.95c.06 1.27.26 2.15.56 2.91.31.79.72 1.46 1.38 2.13.67.66 1.34 1.07 2.13 1.38.76.3 1.64.5 2.91.56C8.33 23.99 8.74 24 12 24s3.67-.01 4.95-.07c1.27-.06 2.15-.26 2.91-.56.79-.31 1.46-.72 2.13-1.38.66-.67 1.07-1.34 1.38-2.13.3-.76.5-1.64.56-2.91.06-1.28.07-1.69.07-4.95s-.01-3.67-.07-4.95c-.06-1.27-.26-2.15-.56-2.91-.31-.79-.72-1.46-1.38-2.13C20.32 1.35 19.65.94 18.86.63c-.76-.3-1.64-.5-2.91-.56C15.67.01 15.26 0 12 0zm0 5.84A6.16 6.16 0 1 0 18.16 12 6.16 6.16 0 0 0 12 5.84zm0 10.15A3.99 3.99 0 1 1 16 12a3.99 3.99 0 0 1-4 3.99zm7.86-10.4a1.44 1.44 0 1 1-2.88 0 1.44 1.44 0 0 1 2.88 0z"/></svg></span></span>
                            <h3>Instagram</h3>
                            <p class="text-muted">Coming soon</p>
                            <span class="badge badge-default">Planned</span>
                        </div>
                    </div>
                </div>

                <div class="panel">
                    <div class="panel-header" style="display:flex;justify-content:space-between;align-items:center;">
                        <h2>Ready to Publish (${clips.length})</h2>
                        ${clips.length > 0 ? `<button class="btn btn-primary" onclick="App.batchUploadToYouTube()"><i data-lucide="upload" class="btn-icon"></i> Upload All to YouTube</button>` : ''}
                    </div>
                    <div class="panel-body">
                        ${clips.length === 0
                ? renderEmptyState('cloud-upload', 'No clips ready to publish', '<button class="btn btn-primary btn-sm" onclick="App.navigate(\'upload\')">Upload & Process a Video</button>')
                : '<div class="clips-grid">' + clips.map(c => renderClipCard(c)).join('') + '</div>'
            }
                    </div>
                </div>
            </div>
        `;
    },

    _showYouTubeSetup() {
        Modal.open('YouTube Setup Guide', `
            <div style="padding: 0.5rem; line-height: 1.8;">
                <h3 style="color: var(--accent-cyan); margin-bottom: 1rem;">How to Connect YouTube</h3>
                <ol style="color: var(--text-secondary); padding-left: 1.5rem;">
                    <li>Go to <a href="https://console.cloud.google.com" target="_blank" style="color: var(--accent-cyan);">Google Cloud Console</a></li>
                    <li>Create a new project (or select existing)</li>
                    <li>Enable <strong>YouTube Data API v3</strong></li>
                    <li>Go to <strong>APIs & Services → Credentials</strong></li>
                    <li>Create <strong>OAuth 2.0 Client ID</strong> (Desktop App)</li>
                    <li>Download the JSON and save it as:<br>
                        <code style="background: var(--glass-bg); padding: 4px 8px; border-radius: 4px; color: var(--accent-cyan);">client_secret.json</code>
                        in the AIClipper project root</li>
                    <li>Click "Start OAuth" below — a browser window will open for consent</li>
                </ol>
                <div style="margin-top: 1.5rem; padding: 1rem; background: rgba(6,182,212,0.08); border: 1px solid rgba(6,182,212,0.2); border-radius: 8px;">
                    <p style="color: var(--text-secondary); font-size: 0.85rem;">
                        <strong style="color: var(--accent-cyan);">💡 Tip:</strong> After first-time OAuth, your token is cached automatically. You won't need to re-authenticate unless the token expires.
                    </p>
                </div>
            </div>
        `, `
            <button class="btn btn-secondary" onclick="Modal.close()">Close</button>
            <button class="btn btn-primary" onclick="App._triggerYouTubeOAuth()">🔐 Start OAuth</button>
        `, 'lg');
    },

    async _triggerYouTubeOAuth() {
        Toast.show('YouTube OAuth will open in a browser window when you upload. Make sure client_secret.json is in the project root.', 'info', 6000);
        Modal.close();
    },

    publishClipDialog(clipId) {
        Modal.open('Publish Clip', `
            <div class="form-group">
                <label>Select Platform</label>
                <select class="form-input" id="publishPlatform">
                    <option value="youtube">YouTube Shorts</option>
                    <option value="facebook">Facebook Reels</option>
                </select>
            </div>
        `, `
            <button class="btn btn-secondary" onclick="Modal.close()">Cancel</button>
            <button class="btn btn-primary" onclick="App._doPublish(${clipId})"><i data-lucide="send" class="btn-icon"></i> Publish</button>
        `);
    },

    async _doPublish(clipId) {
        const platform = document.getElementById('publishPlatform').value;
        try {
            await api.publishClip(clipId, platform);
            Modal.close();
            Toast.success(`Clip queued for ${platform} upload!`);
        } catch (e) { Toast.error(e.message); }
    },

    // ─────────────────────────────────────────────
    // Settings Page
    // ─────────────────────────────────────────────

    async _renderSettings(el) {
        el.innerHTML = `
            <div class="settings-page">
                <div class="settings-grid">
                    <div class="panel">
                        <div class="panel-header"><h2>Clip Generation</h2></div>
                        <div class="panel-body">
                            <div class="form-group">
                                <label>Clip Durations (seconds)</label>
                                <input type="text" class="form-input" id="setClipDurations" value="15, 30, 60" placeholder="15, 30, 60">
                            </div>
                            <div class="form-group">
                                <label>Max Clips Per Video</label>
                                <input type="number" class="form-input" id="setMaxClips" value="10" min="1" max="50">
                            </div>
                            <div class="form-group">
                                <label>Min Gap Between Clips (seconds)</label>
                                <input type="number" class="form-input" id="setMinGap" value="10" min="0" max="120">
                            </div>
                        </div>
                    </div>

                    <div class="panel">
                        <div class="panel-header"><h2>Scoring Weights</h2></div>
                        <div class="panel-body">
                            ${this._renderWeightSlider('Emotion', 'weightEmotion', 25)}
                            ${this._renderWeightSlider('Dialogue', 'weightDialogue', 20)}
                            ${this._renderWeightSlider('Scene Change', 'weightScene', 20)}
                            ${this._renderWeightSlider('Audio Energy', 'weightAudio', 20)}
                            ${this._renderWeightSlider('Face Visibility', 'weightFace', 15)}
                            <div class="weight-total">
                                <span>Total:</span>
                                <span id="weightTotal">100%</span>
                            </div>
                        </div>
                    </div>

                    <div class="panel">
                        <div class="panel-header"><h2>Subtitle Style</h2></div>
                        <div class="panel-body">
                            <div class="form-group">
                                <label>Font Family</label>
                                <select class="form-input" id="setSubFont">
                                    <option value="Arial">Arial</option>
                                    <option value="Montserrat">Montserrat</option>
                                    <option value="Roboto">Roboto</option>
                                    <option value="Inter">Inter</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Font Size</label>
                                <input type="number" class="form-input" id="setSubSize" value="24" min="12" max="48">
                            </div>
                            <div class="form-group">
                                <label>Highlight Color</label>
                                <div class="color-picker">
                                    <label class="color-swatch" id="subHighlightSwatch" title="Pick a highlight color">
                                        <input type="color" id="setSubHighlight" value="#FFD700"
                                               oninput="App._syncSubHighlight()">
                                    </label>
                                    <input type="text" class="form-input form-input-mono" id="subHighlightHex"
                                           value="#FFD700" readonly aria-label="Selected color">
                                </div>
                                <div class="form-hint">Tap the swatch to choose, or paste a hex code below.</div>
                            </div>
                            <div class="form-group">
                                <label>Position</label>
                                <select class="form-input" id="setSubPosition">
                                    <option value="bottom">Bottom</option>
                                    <option value="center">Center</option>
                                    <option value="top">Top</option>
                                </select>
                            </div>
                        </div>
                    </div>

                    <div class="panel">
                        <div class="panel-header"><h2>AI Models</h2></div>
                        <div class="panel-body">
                            <div class="form-group">
                                <label>Whisper Model</label>
                                <select class="form-input" id="setWhisperModel">
                                    <option value="tiny.en">Tiny (fastest)</option>
                                    <option value="base.en">Base</option>
                                    <option value="small.en" selected>Small (recommended)</option>
                                    <option value="medium.en">Medium (slow)</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Ollama Model</label>
                                <select class="form-input" id="setOllamaModel">
                                    <option value="qwen2.5:1.5b" selected>Qwen 2.5 (1.5B) - Very Fast CPU</option>
                                    <option value="qwen2.5:7b-instruct">Qwen 2.5 (7B) - Slower</option>
                                    <option value="llama3.2:1b">Llama 3.2 (1B) - Fastest</option>
                                    <option value="llama3">Llama 3 (8B) - Slow CPU</option>
                                </select>
                            </div>
                        </div>
                    </div>

                    <div class="panel panel--wide">
                        <div class="panel-header"><h2>AI Providers <span class="badge badge-success" style="font-size:0.65em;margin-left:6px;">BYOK</span></h2></div>
                        <div class="panel-body">
                            <p class="text-muted" style="margin-bottom:12px;">
                                Route each AI role to a local model or a cloud API. The app works fully with zero keys — API providers are optional.
                            </p>
                            <div id="providersRoleRows" class="providers-role-rows">
                                <p class="text-muted">Loading providers…</p>
                            </div>
                            <div id="providersUsageBar" class="providers-usage-bar" style="display:none;"></div>
                        </div>
                    </div>

                    <div class="panel panel--wide">
                        <div class="panel-header"><h2>API Keys</h2></div>
                        <div class="panel-body">
                            <p class="text-muted" style="margin-bottom:12px;">
                                To connect a provider, <strong>paste your key into the field below, then click the filled
                                <span style="color:var(--accent-primary);font-weight:700;">Save key</span> button</strong>. Keys are
                                stored in your OS credential manager (never in plain text). A key alone costs nothing;
                                use the <strong>Test</strong> button to confirm it works.
                            </p>
                            <div id="providersKeyRows">
                                <p class="text-muted">Loading…</p>
                            </div>
                        </div>
                    </div>

                    <div class="panel">
                        <div class="panel-header"><h2>Clip Output</h2></div>
                        <div class="panel-body">
                            <div class="form-group">
                                <label class="checkbox-label">
                                    <input type="checkbox" id="setIntroFrame" class="form-checkbox">
                                    Add thumbnail as intro frame
                                </label>
                                <div class="form-hint">Shows the clip's thumbnail as a still for a moment at the very start,
                                before the action plays. A common hook style used on Shorts/Reels.</div>
                            </div>
                            <div class="form-group">
                                <label for="setIntroDuration">Intro duration (seconds)</label>
                                <input type="number" id="setIntroDuration" class="form-control" min="0.1" max="3" step="0.1" value="0.8">
                                <div class="form-hint">How long the thumbnail intro is held. 0.8s is a good default; keep it short.</div>
                            </div>
                        </div>
                    </div>

                    <div class="panel">
                        <div class="panel-header"><h2>Storage Management</h2></div>
                        <div class="panel-body">
                            <div class="form-group">
                                <label class="checkbox-label">
                                    <input type="checkbox" id="setAutoDeleteSource" class="form-checkbox">
                                    Automatically delete the source video after all clips are generated
                                </label>
                                <div class="form-hint">Keeps your clips/thumbnails/subtitles — only the large
                                original upload is removed, and only after a fully successful run.</div>
                            </div>

                            <div class="storage-usage" id="storageUsage">
                                <p class="text-muted">Loading storage usage…</p>
                            </div>

                            <div class="form-group" style="margin-top: 12px;">
                                <label>Bulk cleanup</label>
                                <div class="btn-group" style="display:flex; flex-wrap:wrap; gap:8px;">
                                    <button class="btn btn-danger-outline btn-sm" onclick="App._cleanupPendingVideos()">Delete pending videos</button>
                                    <button class="btn btn-danger-outline btn-sm" onclick="App._clearTempFiles()">Clear temp files</button>
                                    <button class="btn btn-danger-outline btn-sm" onclick="App._cleanupOrphans()">Delete orphaned files</button>
                                </div>
                                <div class="form-hint">Each action asks you to confirm first. Deletions are permanent.</div>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="settings-actions">
                    <button class="btn btn-primary btn-lg" onclick="App._saveSettings()"><i data-lucide="save" class="btn-icon"></i> Save Settings</button>
                </div>
            </div>
        `;

        // Set up weight slider listeners
        document.querySelectorAll('.weight-slider').forEach(slider => {
            slider.addEventListener('input', () => this._updateWeightTotal());
        });

        // Load saved values back into the controls (#77).
        this._loadSettings();
    },

    // Load persisted settings into the Settings form so edits start from what
    // was actually saved, not the hardcoded defaults.
    async _loadSettings() {
        let s = {};
        try {
            const resp = await api.getSettings();
            s = (resp && resp.settings) || {};
        } catch {
            return; // backend not reachable / no saved settings — keep defaults
        }
        const setVal = (id) => {
            const el = document.getElementById(id);
            if (el && s[id] != null) { el.value = s[id]; }
        };

        // Text / number inputs, selects, and the color picker.
        setVal('setClipDurations');
        setVal('setMaxClips');
        setVal('setMinGap');
        setVal('setSubFont');
        setVal('setSubSize');
        setVal('setSubHighlight');
        setVal('setSubPosition');
        this._syncSubHighlight();

        // Model selects — if the saved model isn't an option yet, add it so the
        // current value is visible and re-saveable.
        const setModel = (id, key) => {
            const el = document.getElementById(id);
            if (!el || s[key] == null) return;
            const val = String(s[key]);
            if (![...el.options].some(o => o.value === val)) {
                const opt = document.createElement('option');
                opt.value = val; opt.textContent = val;
                el.appendChild(opt);
            }
            el.value = val;
        };
        setModel('setWhisperModel', 'whisper_model');
        setModel('setOllamaModel', 'ollama_model');

        // Auto-delete-source toggle (default OFF when unset).
        const autoDel = document.getElementById('setAutoDeleteSource');
        if (autoDel) autoDel.checked = !!s.auto_delete_source;

        // Thumbnail-intro toggle (default OFF) + duration (default 0.8s).
        const introFrame = document.getElementById('setIntroFrame');
        if (introFrame) introFrame.checked = s.intro_frame === true || s.intro_frame === 'true';

        const introDur = document.getElementById('setIntroDuration');
        if (introDur) introDur.value = (s.intro_duration != null && s.intro_duration !== '') ? s.intro_duration : 0.8;

        // Refresh the live storage-usage widget.
        this._loadStorageUsage();

        // Load BYOK provider settings.
        this._loadProvidersSettings();

        // Weight sliders + their inline percentage labels.
        const weightKeys = {
            weightEmotion: 'weight_emotion',
            weightDialogue: 'weight_dialogue',
            weightScene: 'weight_scene',
            weightAudio: 'weight_audio',
            weightFace: 'weight_face',
        };
        Object.entries(weightKeys).forEach(([id, key]) => {
            const el = document.getElementById(id);
            if (el && s[key] != null) {
                el.value = s[key];
                const label = document.getElementById(id + 'Value');
                if (label) label.textContent = s[key] + '%';
            }
        });
        this._updateWeightTotal();
    },

    // ─────────────────────────────────────────────
    // AI Providers (BYOK) — Settings panel
    // ─────────────────────────────────────────────

    async _loadProvidersSettings() {
        const rolesEl = document.getElementById('providersRoleRows');
        const keysEl = document.getElementById('providersKeyRows');
        if (!rolesEl && !keysEl) return;

        let data = null;
        try { data = await api.getProviders(); }
        catch (err) {
            // Surface the real failure on screen instead of silently doing nothing.
            const msg = (err && err.message) || String(err);
            if (rolesEl) rolesEl.innerHTML =
                `<p class="text-muted" style="color:var(--status-error);">
                    Could not load providers: <code>${escapeHtml(msg)}</code>
                 </p>`;
            if (keysEl) keysEl.innerHTML =
                `<p class="text-muted" style="color:var(--status-error);">
                    Could not load API keys: <code>${escapeHtml(msg)}</code>
                 </p>`;
            return;
        }
        if (!data) {
            if (rolesEl) rolesEl.innerHTML = '<p class="text-muted">Could not load providers (empty reply).</p>';
            if (keysEl) keysEl.innerHTML = '<p class="text-muted">Could not load API keys (empty reply).</p>';
            return;
        }

        const roles = data.roles || {};
        const opts = data.options || {};   // {provider: {label, default_model, tier}}
        const keys = data.keys_configured || {};
        const usage = data.usage || {};
        const roleList = ['brain', 'classify', 'hook', 'translate'];

        const API_PROVIDERS = ['openai', 'anthropic', 'gemini', 'openai_compatible'];
        const PROVIDER_ORDER = ['local', 'openai', 'anthropic', 'gemini', 'openai_compatible'];
        const providerLabel = (p) => (opts[p] && opts[p].label) ||
            ({ local: 'Local (Ollama)', openai: 'OpenAI', anthropic: 'Anthropic', gemini: 'Gemini', openai_compatible: 'OpenAI Compatible' }[p] || p);

        // ── Per-role rows ──
        if (rolesEl) {
            rolesEl.innerHTML = roleList.map(role => {
                const cur = roles[role] || {};
                const curProvider = cur.provider || 'local';
                const curModel = cur.model || '';
                const providerOpts = PROVIDER_ORDER.map(p =>
                    `<option value="${p}" ${p === curProvider ? 'selected' : ''}>${providerLabel(p)}</option>`
                ).join('');
                const isLocal = curProvider === 'local';
                return `<div class="providers-role-row">
                    <span class="providers-role-label">${escapeHtml(role)}</span>
                    <select class="form-input form-input-sm providers-role-select" data-role="${role}" onchange="App._onProviderRoleChange(this)">
                        ${providerOpts}
                    </select>
                    <input type="text" class="form-input form-input-sm providers-model-input" data-role="${role}"
                           value="${escapeHtml(curModel)}" placeholder="model name" ${isLocal ? 'disabled' : ''}>
                    <button class="btn btn-sm btn-outline providers-test-btn" data-role="${role}"
                            onclick="App._testProviderForRow(this)" ${isLocal ? 'disabled' : ''}>Test</button>
                    <span class="providers-test-result" data-role="${role}"></span>
                </div>`;
            }).join('');
        }

        // ── Per-provider key rows ──
        if (keysEl) {
            keysEl.innerHTML = API_PROVIDERS.map(p => {
                const has = keys[p];
                const label = providerLabel(p);
                return `<div class="providers-key-row">
                    <span class="providers-key-label">${escapeHtml(label)}</span>
                    <input type="password" class="form-input form-input-sm providers-key-input" data-provider="${p}"
                           placeholder="${has ? '•••••••• (key saved)' : 'Paste API key here…'}" autocomplete="off">
                    <button class="btn btn-primary btn-sm providers-save-btn" onclick="App._saveProviderKey('${p}')">Save key</button>
                    <button class="btn btn-sm btn-danger-outline" onclick="App._deleteProviderKey('${p}')" ${has ? '' : 'disabled'}>Remove</button>
                    ${p === 'openai_compatible' ? '<input type="text" class="form-input form-input-sm providers-base-url" data-provider="openai_compatible" placeholder="Base URL (e.g. https://api.openrouter.ai/v1)">' : ''}
                </div>`;
            }).join('');
        }

        // ── Usage bar ──
        this._updateProvidersUsage(usage);
    },

    _updateProvidersUsage(usage) {
        const bar = document.getElementById('providersUsageBar');
        if (!bar) return;
        if (!usage || !usage.total_calls) { bar.style.display = 'none'; return; }
        const cost = usage.est_usd != null ? `$${usage.est_usd.toFixed(4)}` : '—';
        bar.style.display = 'block';
        bar.innerHTML = `<span class="providers-usage-label">Session usage:</span>
            <span class="providers-usage-stat">${usage.total_calls} call(s)</span>
            <span class="providers-usage-stat">${usage.total_tokens_in || 0} in / ${usage.total_tokens_out || 0} out</span>
            <span class="providers-usage-stat providers-usage-cost">≈ ${cost} USD</span>`;
    },

    async _onProviderRoleChange(sel) {
        const role = sel.dataset.role;
        const provider = sel.value;
        const row = sel.closest('.providers-role-row');
        const modelInput = row ? row.querySelector('.providers-model-input') : null;
        const testBtn = row ? row.querySelector('.providers-test-btn') : null;
        const isLocal = provider === 'local';
        if (modelInput) modelInput.disabled = isLocal;
        if (testBtn) testBtn.disabled = isLocal;
        try {
            const model = modelInput && !isLocal ? modelInput.value.trim() || null : null;
            await api.setRoleProvider(role, provider, model);
            Toast.success(`${role} → ${provider === 'local' ? 'Local (Ollama)' : provider}`);
        } catch (e) { Toast.error('Failed: ' + e.message); }
    },

    async _testProviderForRow(btn) {
        const role = btn.dataset.role;
        const row = btn.closest('.providers-role-row');
        const sel = row ? row.querySelector('.providers-role-select') : null;
        const modelInput = row ? row.querySelector('.providers-model-input') : null;
        const resultEl = row ? row.querySelector('.providers-test-result') : null;
        if (!sel) return;
        const provider = sel.value;
        if (provider === 'local') return;
        btn.disabled = true;
        btn.textContent = '…';
        if (resultEl) resultEl.textContent = '';
        try {
            const model = modelInput ? modelInput.value.trim() || null : null;
            const res = await api.testProvider(provider, model);
            if (resultEl) {
                resultEl.textContent = res.ok ? '✓ Connected' : (res.message || 'Failed');
                resultEl.style.color = res.ok ? 'var(--accent-success)' : 'var(--status-error)';
            }
        } catch (e) {
            if (resultEl) { resultEl.textContent = 'Error'; resultEl.style.color = 'var(--status-error)'; }
        } finally { btn.disabled = false; btn.textContent = 'Test'; }
    },

    async _saveProviderKey(provider) {
        const inp = document.querySelector(`.providers-key-input[data-provider="${provider}"]`);
        const key = inp ? inp.value.trim() : '';
        if (!key) { Toast.error('Paste a key first.'); return; }
        try {
            await api.storeProviderKey(provider, key);
            Toast.success(`${provider} key saved.`);
            if (inp) inp.value = '';
            if (inp) inp.placeholder = '•••••••• (key saved)';
            // Enable the Remove button next to this input.
            const rmBtn = inp ? inp.closest('.providers-key-row')?.querySelector('.btn-danger-outline') : null;
            if (rmBtn) rmBtn.disabled = false;
        } catch (e) { Toast.error('Failed: ' + e.message); }
    },

    async _deleteProviderKey(provider) {
        try {
            await api.deleteProviderKey(provider);
            Toast.success(`${provider} key removed.`);
            const inp = document.querySelector(`.providers-key-input[data-provider="${provider}"]`);
            if (inp) inp.placeholder = 'Paste API key…';
            const rmBtn = inp ? inp.closest('.providers-key-row')?.querySelector('.btn-danger-outline') : null;
            if (rmBtn) rmBtn.disabled = true;
        } catch (e) { Toast.error('Failed: ' + e.message); }
    },

    // Keep the visible color swatch + hex readout in sync with the hidden
    // native <input type="color">. Saving reads setSubHighlight.value only.
    _syncSubHighlight() {
        const inp = document.getElementById('setSubHighlight');
        if (!inp) return;
        const sw = document.getElementById('subHighlightSwatch');
        const hex = document.getElementById('subHighlightHex');
        if (sw) sw.style.background = inp.value;
        if (hex) { hex.value = (inp.value || '#FFD700').toUpperCase(); hex.style.color = inp.value; }
    },

    _renderWeightSlider(label, id, defaultVal) {
        return `
            <div class="form-group slider-group">
                <label>${label} <span class="slider-value" id="${id}Value">${defaultVal}%</span></label>
                <input type="range" class="weight-slider form-range" id="${id}" min="0" max="50" value="${defaultVal}"
                       oninput="document.getElementById('${id}Value').textContent = this.value + '%'">
            </div>
        `;
    },

    _updateWeightTotal() {
        const ids = ['weightEmotion', 'weightDialogue', 'weightScene', 'weightAudio', 'weightFace'];
        const total = ids.reduce((sum, id) => sum + parseInt(document.getElementById(id)?.value || 0), 0);
        const el = document.getElementById('weightTotal');
        if (el) {
            el.textContent = total + '%';
            el.style.color = total === 100 ? 'var(--accent-green)' : 'var(--error)';
        }
    },

    async _saveSettings() {
        try {
            const settings = {
                clip_durations: document.getElementById('setClipDurations').value,
                max_clips: document.getElementById('setMaxClips').value,
                min_gap: document.getElementById('setMinGap').value,
                weight_emotion: document.getElementById('weightEmotion').value,
                weight_dialogue: document.getElementById('weightDialogue').value,
                weight_scene: document.getElementById('weightScene').value,
                weight_audio: document.getElementById('weightAudio').value,
                weight_face: document.getElementById('weightFace').value,
                subtitle_font: document.getElementById('setSubFont').value,
                subtitle_size: document.getElementById('setSubSize').value,
                subtitle_highlight: document.getElementById('setSubHighlight').value,
                subtitle_position: document.getElementById('setSubPosition').value,
                whisper_model: document.getElementById('setWhisperModel').value,
                ollama_model: document.getElementById('setOllamaModel').value,
                auto_delete_source: document.getElementById('setAutoDeleteSource').checked,
                intro_frame: document.getElementById('setIntroFrame').checked,
                intro_duration: parseFloat(document.getElementById('setIntroDuration').value) || 0.8,
            };

            await api.updateSettings(settings);
            Toast.success('Settings saved!');
        } catch (e) {
            Toast.error('Failed to save: ' + e.message);
        }
    },

    // ─────────────────────────────────────────────
    // Storage management — usage + bulk cleanup
    // ─────────────────────────────────────────────

    async _loadStorageUsage() {
        const el = document.getElementById('storageUsage');
        if (!el) return;
        try {
            const u = await api.getStorageUsage();
            const rows = [
                ['Source uploads', u.source_uploads],
                ['Clips / outputs', u.clips_outputs],
                ['Thumbnails', u.thumbnails],
                ['Subtitles', u.subtitles],
                ['Temp', u.temp],
            ];
            el.innerHTML = `
                <div class="storage-usage-table">
                    ${rows.map(([name, bytes]) => `
                        <div class="storage-row">
                            <span>${name}</span>
                            <span class="mono">${formatFileSize(bytes)}</span>
                        </div>`).join('')}
                    <div class="storage-row storage-row-total">
                        <span><strong>Total</strong></span>
                        <span class="mono"><strong>${formatFileSize(u.total)}</strong></span>
                    </div>
                </div>`;
        } catch {
            el.innerHTML = '<p class="text-muted">Could not load storage usage.</p>';
        }
    },

    async _cleanupPendingVideos() {
        let count = 0;
        try { count = (await api.countPendingVideos()).pending || 0; } catch { /* ignore */ }
        const what = count ? `${count} pending video(s)` : 'all pending videos';
        Modal.confirm('Delete pending videos',
            `This will permanently delete ${what} and everything they generated. This cannot be undone.`,
            async () => {
                try {
                    const r = await api.cleanupPendingVideos(true);
                    Toast.success(r.message || 'Pending videos deleted.');
                    this._loadStorageUsage();
                } catch (e) { Toast.error('Failed: ' + e.message); }
            });
    },

    async _clearTempFiles() {
        Modal.confirm('Clear temp files',
            'This deletes all temporary working files. This cannot be undone.',
            async () => {
                try {
                    const r = await api.clearTemp(true);
                    Toast.success(r.message || 'Temp files cleared.');
                    this._loadStorageUsage();
                } catch (e) { Toast.error('Failed: ' + e.message); }
            });
    },

    async _cleanupOrphans() {
        let summary = '';
        try {
            const o = await api.listOrphans();
            const n = (o.orphans || []).length;
            summary = n ? ` ${n} file(s), ${formatFileSize(o.total_size)}.` : 'Nothing orphaned found.';
        } catch { summary = ''; }
        Modal.confirm('Delete orphaned files',
            `This deletes files on disk that have no matching video in the database.${summary} This cannot be undone.`,
            async () => {
                try {
                    const r = await api.cleanupOrphans(true);
                    Toast.success(r.message || 'Orphaned files deleted.');
                    this._loadStorageUsage();
                } catch (e) { Toast.error('Failed: ' + e.message); }
            });
    },

    // ─────────────────────────────────────────────
    // Storage management — per-video delete
    // ─────────────────────────────────────────────

    _deleteVideoPrompt(videoId, filename) {
        const name = escapeHtml(filename || `video #${videoId}`);
        const body = `
            <div style="line-height: 1.7;">
                <p style="margin-bottom: 12px;"><strong>${name}</strong></p>
                <p><strong>Delete source file only</strong><br>
                Removes the large original upload from disk. All generated clips,
                thumbnails and subtitles are <em>kept</em>.</p>
                <p><strong>Delete everything</strong><br>
                Permanently deletes the source video <em>and</em> every clip,
                edit, thumbnail and subtitle it generated. <strong>This cannot be
                undone.</strong></p>
            </div>`;
        Modal.open(
            'Delete video',
            body,
            `<button class="btn btn-secondary" onclick="Modal.close()">Cancel</button>
             <button class="btn btn-secondary" onclick="App._deleteVideo(${videoId}, 'source')">Delete source only</button>
             <button class="btn btn-danger" onclick="App._deleteVideo(${videoId}, 'everything')">Delete everything</button>`,
            'lg'
        );
    },

    async _deleteVideo(videoId, mode) {
        Modal.close();
        const label = mode === 'source' ? 'source file' : 'video & everything derived from it';
        try {
            const resp = await api.deleteVideo(videoId, mode, true);
            Toast.success(`Deleted ${label}. ${resp && resp.removed_files ? `(${resp.removed_files} file${resp.removed_files === 1 ? '' : 's'} removed)` : ''}`);
            // Re-render the current page so the deleted card disappears.
            if (this.currentPage) this.navigate(this.currentPage, this.state || {});
            else this.navigate('dashboard');
        } catch (e) {
            Toast.error('Failed to delete: ' + e.message);
        }
    },

    // ─────────────────────────────────────────────
    // Actions
    // ─────────────────────────────────────────────

    async processVideo(videoId) {
        const opts = this._readClipOptions() || this._lastClipOptions || {};
        try {
            await api.startProcessing(videoId, opts);
            Toast.success('Processing started! This may take a while on CPU.');
            this.navigate('processing');
        } catch (e) { Toast.error(e.message); }
    },

    _readClipOptions() {
        const countEl = document.getElementById('clipCountSelect');
        const durEl = document.getElementById('clipDurationSelect');
        const opts = {};
        if (countEl && countEl.value) opts.clip_count = parseInt(countEl.value, 10);
        if (durEl && durEl.value) opts.clip_duration = parseInt(durEl.value, 10);
        return Object.keys(opts).length ? opts : null;
    },
};

// ─────────────────────────────────────────────
// Boot
// ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => App.init());
