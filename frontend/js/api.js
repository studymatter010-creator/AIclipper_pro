/**
 * AIClipper API Client
 * Handles all REST API calls, file uploads, and WebSocket connections.
 */

const API_BASE = '/api';

class APIClient {
    constructor() {
        this._wsConnections = new Map();
    }

    // ─────────────────────────────────────────────
    // Core HTTP Methods
    // ─────────────────────────────────────────────

    async _fetch(endpoint, options = {}) {
        const url = `${API_BASE}${endpoint}`;
        const config = {
            headers: { 'Content-Type': 'application/json', ...options.headers },
            ...options,
        };

        // Don't set Content-Type for FormData
        if (options.body instanceof FormData) {
            delete config.headers['Content-Type'];
        }

        try {
            const response = await fetch(url, config);
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                const error = new Error(errorData.detail || `HTTP ${response.status}: ${response.statusText}`);
                error.status = response.status;
                error.data = errorData;
                throw error;
            }
            if (response.status === 204) return null;
            return await response.json();
        } catch (err) {
            if (err.status) throw err;
            throw new Error(`Network error: ${err.message}`);
        }
    }

    async get(endpoint) {
        return this._fetch(endpoint, { method: 'GET' });
    }

    async post(endpoint, data = null) {
        const options = { method: 'POST' };
        if (data) options.body = JSON.stringify(data);
        return this._fetch(endpoint, options);
    }

    async put(endpoint, data) {
        return this._fetch(endpoint, { method: 'PUT', body: JSON.stringify(data) });
    }

    async del(endpoint) {
        return this._fetch(endpoint, { method: 'DELETE' });
    }

    // ─────────────────────────────────────────────
    // Health
    // ─────────────────────────────────────────────

    async health() {
        return this.get('/health');
    }

    // ─────────────────────────────────────────────
    // Videos
    // ─────────────────────────────────────────────

    async uploadVideo(file, projectId = null, onProgress = null) {
        return new Promise((resolve, reject) => {
            const formData = new FormData();
            formData.append('file', file);

            const xhr = new XMLHttpRequest();
            xhr.open('POST', `${API_BASE}/upload${projectId ? `?project_id=${projectId}` : ''}`);

            xhr.upload.addEventListener('progress', (e) => {
                if (e.lengthComputable && onProgress) {
                    const percent = Math.round((e.loaded / e.total) * 100);
                    onProgress(percent, e.loaded, e.total);
                }
            });

            xhr.addEventListener('load', () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    resolve(JSON.parse(xhr.responseText));
                } else {
                    try {
                        const err = JSON.parse(xhr.responseText);
                        reject(new Error(err.detail || `Upload failed: ${xhr.status}`));
                    } catch {
                        reject(new Error(`Upload failed: ${xhr.status}`));
                    }
                }
            });

            xhr.addEventListener('error', () => reject(new Error('Upload network error')));
            xhr.addEventListener('abort', () => reject(new Error('Upload cancelled')));
            xhr.send(formData);
        });
    }

    async listVideos(offset = 0, limit = 20, projectId = null) {
        let url = `/videos?offset=${offset}&limit=${limit}`;
        if (projectId) url += `&project_id=${projectId}`;
        return this.get(url);
    }

    async getVideo(videoId) {
        return this.get(`/videos/${videoId}`);
    }

    // ─────────────────────────────────────────────
    // Processing
    // ─────────────────────────────────────────────

    async startProcessing(videoId, options = {}) {
        const payload = {};
        if (options.clip_count) payload.clip_count = options.clip_count;
        if (options.clip_duration) payload.clip_duration = options.clip_duration;
        // Only send a body if we actually have options to send.
        return this.post(`/process/${videoId}`, Object.keys(payload).length ? payload : null);
    }

    async getStatus(videoId) {
        return this.get(`/status/${videoId}`);
    }

    async importFromUrl(url, opts = {}) {
        const payload = { url, auto_process: opts.auto_process !== false };
        if (opts.project_id) payload.project_id = opts.project_id;
        if (opts.clip_count) payload.clip_count = opts.clip_count;
        if (opts.clip_duration) payload.clip_duration = opts.clip_duration;
        return this.post('/import-url', payload);
    }

    // ─────────────────────────────────────────────
    // Clips
    // ─────────────────────────────────────────────

    async listClips(offset = 0, limit = 20, videoId = null) {
        let url = `/clips?offset=${offset}&limit=${limit}`;
        if (videoId) url += `&video_id=${videoId}`;
        return this.get(url);
    }

    async getClip(clipId) {
        return this.get(`/clips/${clipId}`);
    }

    async deleteClip(clipId) {
        return this.del(`/clips/${clipId}`);
    }

    async bulkDeleteClips(clipIds) {
        return this.post('/clips/bulk-delete', { clip_ids: clipIds });
    }

    getClipDownloadUrl(clipId) {
        return `${API_BASE}/clips/${clipId}/download`;
    }

    getClipThumbnailUrl(clipId) {
        return `${API_BASE}/clips/${clipId}/thumbnail`;
    }

    // ─────────────────────────────────────────────
    // AI Editor / Voice-Over
    // ─────────────────────────────────────────────

    getVoices() {
        return this.get('/voices');
    }

    autoEditClip(clipId, options = {}) {
        return this.post(`/clips/${clipId}/auto-edit`, options);
    }

    // Part B: 4-6 scored candidate frames for the thumbnail editor filmstrip.
    thumbnailCandidates(clipId) {
        return this.get(`/clips/${clipId}/thumbnail-candidates`);
    }

    // Upload YOUR OWN music track to swap into a clip (Audio Remix).
    async uploadClipMusic(clipId, file) {
        const formData = new FormData();
        formData.append('file', file);
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', `${API_BASE}/clips/${clipId}/music`);
            xhr.onload = () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    resolve(JSON.parse(xhr.responseText));
                } else {
                    try {
                        const err = JSON.parse(xhr.responseText);
                        reject(new Error(err.detail || `Upload failed: ${xhr.status}`));
                    } catch { reject(new Error(`Upload failed: ${xhr.status}`)); }
                }
            };
            xhr.onerror = () => reject(new Error('Upload network error'));
            xhr.send(formData);
        });
    }

    teamStatus() {
        return this.get('/team/status');
    }

    // ─────────────────────────────────────────────
    // AI Providers (BYOK)
    // ─────────────────────────────────────────────

    getProviders() {
        return this.get('/providers');
    }

    setRoleProvider(role, provider, model = null) {
        return this.put('/providers/role', { role, provider, model });
    }

    testProvider(provider, model = null) {
        return this.post('/providers/test', { provider, model });
    }

    storeProviderKey(provider, key) {
        return this.put('/providers/key', { provider, key });
    }

    deleteProviderKey(provider) {
        return this.del(`/providers/key/${provider}`);
    }

    getProviderUsage() {
        return this.get('/providers/usage');
    }

    dubClip(clipId, payload) {
        return this.post(`/clips/${clipId}/dub`, payload);
    }

    getClipVoiceovers(clipId) {
        return this.get(`/clips/${clipId}/voiceovers`);
    }

    // ─────────────────────────────────────────────
    // Publishing
    // ─────────────────────────────────────────────

    async publishClip(clipId, platform, scheduledAt = null) {
        const data = { clip_id: clipId, platform };
        if (scheduledAt) data.scheduled_at = scheduledAt;
        return this.post('/publish', data);
    }

    // ─────────────────────────────────────────────
    // Analytics
    // ─────────────────────────────────────────────

    async getAnalytics() {
        return this.get('/analytics');
    }

    // ─────────────────────────────────────────────
    // Projects
    // ─────────────────────────────────────────────

    async listProjects(offset = 0, limit = 50) {
        return this.get(`/projects?offset=${offset}&limit=${limit}`);
    }

    async createProject(name, description = '') {
        return this.post('/projects', { name, description });
    }

    // ─────────────────────────────────────────────
    // Settings
    // ─────────────────────────────────────────────

    async getSettings() {
        return this.get('/settings');
    }

    async updateSettings(settings) {
        return this.put('/settings', { settings });
    }

    // ─────────────────────────────────────────────
    // Storage & Deletion
    // ─────────────────────────────────────────────

    async getStorageUsage() {
        return this.get('/storage/usage');
    }

    async deleteVideo(videoId, mode, confirm = false) {
        return this.post(`/videos/${videoId}/delete`, { mode, confirm });
    }

    async listOrphans() {
        return this.get('/storage/orphans');
    }

    async cleanupOrphans(confirm = false) {
        return this.post('/storage/orphans/cleanup', { confirm });
    }

    async cleanupPendingVideos(confirm = false) {
        return this.post('/storage/videos/pending-cleanup', { confirm });
    }

    async clearTemp(confirm = false) {
        return this.post('/storage/temp-cleanup', { confirm });
    }

    async countPendingVideos() {
        return this.get('/storage/video-count-pending');
    }

    // ─────────────────────────────────────────────
    // WebSocket Progress
    // ─────────────────────────────────────────────

    connectProgress(videoId, onMessage, onClose = null) {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}${API_BASE}/ws/progress/${videoId}`;

        if (this._wsConnections.has(videoId)) {
            this._wsConnections.get(videoId).close();
        }

        const ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            console.log(`WebSocket connected for video ${videoId}`);
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                onMessage(data);
            } catch (e) {
                console.error('WebSocket parse error:', e);
            }
        };

        ws.onclose = () => {
            this._wsConnections.delete(videoId);
            if (onClose) onClose();
        };

        ws.onerror = (err) => {
            console.error('WebSocket error:', err);
        };

        this._wsConnections.set(videoId, ws);
        return ws;
    }

    disconnectProgress(videoId) {
        const ws = this._wsConnections.get(videoId);
        if (ws) {
            ws.close();
            this._wsConnections.delete(videoId);
        }
    }

    disconnectAll() {
        for (const [id, ws] of this._wsConnections) {
            ws.close();
        }
        this._wsConnections.clear();
    }
}

// Global singleton
const api = new APIClient();
