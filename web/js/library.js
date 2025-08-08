/**
 * Library Manager - Handles video library display and management
 */

class LibraryManager {
    constructor(app) {
        this.app = app;
        this.videos = [];
        this.currentFilter = '';
        
        this.init();
    }
    
    init() {
        this.setupEventListeners();
    }
    
    setupEventListeners() {
        const refreshBtn = document.getElementById('refreshLibrary');
        const statusFilter = document.getElementById('statusFilter');
        
        if (refreshBtn) {
            refreshBtn.addEventListener('click', () => this.loadVideos());
        }
        
        if (statusFilter) {
            statusFilter.addEventListener('change', (e) => {
                this.currentFilter = e.target.value;
                this.renderVideos();
            });
        }
    }
    
    async loadVideos() {
        const grid = document.getElementById('videoGrid');
        const loading = document.getElementById('libraryLoading');
        
        if (loading) loading.style.display = 'block';
        
        try {
            const response = await this.app.apiRequest('/api/videos');
            this.videos = response.videos || [];
            this.renderVideos();
        } catch (error) {
            console.error('Failed to load videos:', error);
            this.app.showToast('error', 'Load Failed', 'Could not load video library');
        } finally {
            if (loading) loading.style.display = 'none';
        }
    }
    
    renderVideos() {
        const grid = document.getElementById('videoGrid');
        if (!grid) return;
        
        let filteredVideos = this.videos;
        if (this.currentFilter) {
            filteredVideos = this.videos.filter(video => video.status === this.currentFilter);
        }
        
        if (filteredVideos.length === 0) {
            grid.innerHTML = `
                <div style="grid-column: 1 / -1; text-align: center; padding: 3rem;">
                    <div style="font-size: 3rem; margin-bottom: 1rem;">📁</div>
                    <h3>No Videos Found</h3>
                    <p style="color: var(--text-secondary);">
                        ${this.currentFilter ? 'No videos match the current filter.' : 'Upload some videos to get started!'}
                    </p>
                </div>
            `;
            return;
        }
        
        grid.innerHTML = filteredVideos.map(video => this.renderVideoCard(video)).join('');
    }
    
    renderVideoCard(video) {
        const statusClass = `status-${video.status}`;
        const statusIcon = {
            completed: '✅',
            processing: '⏳',
            error: '❌'
        }[video.status] || '📄';
        
        return `
            <div class="video-card" data-video-id="${video.video_id}">
                <div class="video-thumbnail">
                    🎬
                </div>
                <div class="video-info">
                    <h3 class="video-title">${video.title}</h3>
                    <div class="video-meta">
                        <span>Duration: ${this.app.formatDuration(video.duration)}</span>
                        <span>Size: ${this.app.formatBytes(video.file_size)}</span>
                        <span>Uploaded: ${this.app.formatDate(video.upload_date)}</span>
                    </div>
                    <div class="video-status ${statusClass}">
                        ${statusIcon} ${video.status.toUpperCase()}
                    </div>
                    <div class="video-actions">
                        ${video.status === 'completed' ? 
                            `<button class="btn btn-primary btn-sm" onclick="app.streamingManager?.playVideo('${video.video_id}')">
                                ▶️ Play
                            </button>` : ''
                        }
                        <button class="btn btn-secondary btn-sm" onclick="app.libraryManager.showVideoDetails('${video.video_id}')">
                            ℹ️ Details
                        </button>
                        <button class="btn btn-warning btn-sm" onclick="app.libraryManager.deleteVideo('${video.video_id}')">
                            🗑️ Delete
                        </button>
                    </div>
                </div>
            </div>
        `;
    }
    
    async showVideoDetails(videoId) {
        try {
            const response = await this.app.apiRequest(`/api/videos/${videoId}`);
            const video = response.video;
            
            const content = `
                <div class="video-details">
                    <h3>${video.title}</h3>
                    <div class="details-grid">
                        <div class="detail-item">
                            <strong>Duration:</strong> ${this.app.formatDuration(video.duration)}
                        </div>
                        <div class="detail-item">
                            <strong>File Size:</strong> ${this.app.formatBytes(video.file_size)}
                        </div>
                        <div class="detail-item">
                            <strong>Format:</strong> ${video.format_name}
                        </div>
                        <div class="detail-item">
                            <strong>Status:</strong> ${video.status}
                        </div>
                        <div class="detail-item">
                            <strong>Uploaded:</strong> ${this.app.formatDate(video.upload_date)}
                        </div>
                        <div class="detail-item">
                            <strong>Segments:</strong> ${video.segments_count}
                        </div>
                    </div>
                    
                    ${video.streams && video.streams.length > 0 ? `
                        <h4 style="margin-top: 1.5rem;">Streams</h4>
                        <div class="streams-list">
                            ${video.streams.map(stream => `
                                <div class="stream-item">
                                    <strong>${stream.stream_type}:</strong> 
                                    ${stream.codec_name}
                                    ${stream.width && stream.height ? `${stream.width}x${stream.height}` : ''}
                                    ${stream.language ? `(${stream.language})` : ''}
                                </div>
                            `).join('')}
                        </div>
                    ` : ''}
                    
                    ${video.status === 'completed' ? `
                        <div style="margin-top: 1.5rem;">
                            <h4>Streaming URL</h4>
                            <div class="url-copy">
                                <input type="text" value="${this.app.getStreamingUrl(videoId)}" readonly style="width: 100%; padding: 0.5rem;">
                                <button class="btn btn-secondary btn-sm" onclick="app.copyToClipboard('${this.app.getStreamingUrl(videoId)}')">
                                    📋 Copy
                                </button>
                            </div>
                        </div>
                    ` : ''}
                </div>
            `;
            
            this.app.openModal('videoModal', content);
            
        } catch (error) {
            console.error('Failed to load video details:', error);
            this.app.showToast('error', 'Load Failed', 'Could not load video details');
        }
    }
    
    async deleteVideo(videoId) {
        if (!confirm('Are you sure you want to delete this video? This action cannot be undone.')) {
            return;
        }
        
        try {
            await this.app.apiRequest(`/api/videos/${videoId}`, {
                method: 'DELETE'
            });
            
            this.app.showToast('success', 'Deleted', 'Video deleted successfully');
            this.loadVideos(); // Refresh list
            
        } catch (error) {
            console.error('Failed to delete video:', error);
            this.app.showToast('error', 'Delete Failed', 'Could not delete video');
        }
    }
}

// Make it globally available
window.LibraryManager = LibraryManager;