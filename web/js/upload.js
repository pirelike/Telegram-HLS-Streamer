/**
 * Upload Manager - Handles file uploads and processing
 */

class UploadManager {
    constructor(app) {
        this.app = app;
        this.uploadQueue = [];
        this.activeUploads = new Map();
        this.processingJobs = new Map();
        
        this.init();
    }
    
    init() {
        this.setupUploadZone();
        this.setupEventListeners();
        
        // Poll for processing status updates
        setInterval(() => this.updateProcessingStatus(), 3000);
    }
    
    setupUploadZone() {
        const uploadZone = document.getElementById('uploadZone');
        const fileInput = document.getElementById('fileInput');
        const browseBtn = document.getElementById('browseBtn');
        
        // Drag and drop events
        uploadZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadZone.classList.add('dragover');
        });
        
        uploadZone.addEventListener('dragleave', (e) => {
            e.preventDefault();
            uploadZone.classList.remove('dragover');
        });
        
        uploadZone.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadZone.classList.remove('dragover');
            
            const files = Array.from(e.dataTransfer.files);
            this.handleFiles(files);
        });
        
        // Click to browse
        uploadZone.addEventListener('click', () => {
            fileInput.click();
        });
        
        browseBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            fileInput.click();
        });
        
        // File input change
        fileInput.addEventListener('change', (e) => {
            const files = Array.from(e.target.files);
            this.handleFiles(files);
            // Reset input to allow same file selection
            e.target.value = '';
        });
    }
    
    setupEventListeners() {
        // Any additional event listeners specific to upload functionality
    }
    
    handleFiles(files) {
        const videoFiles = files.filter(file => {
            return file.type.startsWith('video/') || 
                   this.isVideoFile(file.name);
        });
        
        if (videoFiles.length === 0) {
            this.app.showToast('warning', 'No Video Files', 'Please select video files to upload');
            return;
        }
        
        const nonVideoFiles = files.length - videoFiles.length;
        if (nonVideoFiles > 0) {
            this.app.showToast('info', 'Files Filtered', 
                `${nonVideoFiles} non-video files were skipped`);
        }
        
        // Add files to upload queue
        videoFiles.forEach(file => this.addToQueue(file));
        
        // Start processing queue
        this.processQueue();
    }
    
    isVideoFile(filename) {
        const videoExtensions = [
            '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v',
            '.3gp', '.3g2', '.asf', '.m2v', '.mxf', '.roq', '.nsv', '.f4v'
        ];
        
        const ext = filename.toLowerCase().slice(filename.lastIndexOf('.'));
        return videoExtensions.includes(ext);
    }
    
    addToQueue(file) {
        // Check file size
        const maxSize = this.app.config?.limits?.max_upload_size_gb * 1024 * 1024 * 1024;
        if (maxSize && file.size > maxSize) {
            this.app.showToast('error', 'File Too Large', 
                `${file.name} exceeds maximum file size of ${this.app.config.limits.max_upload_size_gb}GB`);
            return;
        }
        
        const queueItem = {
            id: this.generateId(),
            file: file,
            status: 'queued',
            progress: 0,
            uploadId: null,
            error: null,
            startTime: null,
            processingMode: null
        };
        
        this.uploadQueue.push(queueItem);
        this.renderQueue();
        
        // Show upload queue if hidden
        const queueEl = document.getElementById('uploadQueue');
        if (queueEl.style.display === 'none') {
            queueEl.style.display = 'block';
        }
    }
    
    async processQueue() {
        const maxConcurrent = 2; // Limit concurrent uploads
        const activeCount = this.activeUploads.size;
        
        if (activeCount >= maxConcurrent) {
            return;
        }
        
        const nextItem = this.uploadQueue.find(item => item.status === 'queued');
        if (!nextItem) {
            return;
        }
        
        await this.uploadFile(nextItem);
        
        // Process next item if queue not empty
        setTimeout(() => this.processQueue(), 1000);
    }
    
    async uploadFile(queueItem) {
        queueItem.status = 'uploading';
        queueItem.startTime = Date.now();
        
        this.activeUploads.set(queueItem.id, queueItem);
        this.renderQueue();
        
        try {
            const result = await this.app.uploadFile(queueItem.file, (progress) => {
                queueItem.progress = Math.round(progress);
                this.renderQueue();
            });
            
            queueItem.uploadId = result.upload_id;
            queueItem.status = 'processing';
            queueItem.progress = 100;
            
            // Move to processing tracking
            this.processingJobs.set(result.upload_id, queueItem);
            
            this.app.showToast('success', 'Upload Complete', 
                `${queueItem.file.name} uploaded successfully and is being processed`);
            
            // Show processing modal for first upload
            if (this.processingJobs.size === 1) {
                this.showProcessingModal(queueItem);
            }
            
        } catch (error) {
            queueItem.status = 'error';
            queueItem.error = error.message;
            
            this.app.showToast('error', 'Upload Failed', 
                `Failed to upload ${queueItem.file.name}: ${error.message}`);
        } finally {
            this.activeUploads.delete(queueItem.id);
            this.renderQueue();
            
            // Continue processing queue
            setTimeout(() => this.processQueue(), 500);
        }
    }
    
    async updateProcessingStatus() {
        if (this.processingJobs.size === 0) return;
        
        for (const [uploadId, queueItem] of this.processingJobs.entries()) {
            try {
                const status = await this.app.apiRequest(`/api/videos/${uploadId}/status`);
                
                if (status.status === 'completed') {
                    queueItem.status = 'completed';
                    queueItem.videoId = status.video_id || uploadId;
                    
                    this.processingJobs.delete(uploadId);
                    
                    this.app.showToast('success', 'Processing Complete', 
                        `${queueItem.file.name} is ready for streaming`);
                    
                    // Update library if visible
                    if (this.app.currentTab === 'library' && this.app.libraryManager) {
                        this.app.libraryManager.loadVideos();
                    }
                    
                } else if (status.status === 'error') {
                    queueItem.status = 'error';
                    queueItem.error = status.error_message || 'Processing failed';
                    
                    this.processingJobs.delete(uploadId);
                    
                    this.app.showToast('error', 'Processing Failed', 
                        `Failed to process ${queueItem.file.name}`);
                        
                } else {
                    // Update progress
                    queueItem.progress = Math.round(status.progress || 0);
                    queueItem.processingMode = status.metadata?.used_copy_mode ? 'Copy Mode' : 'Transcode';
                    
                    // Update processing modal if open
                    this.updateProcessingModal(queueItem, status);
                }
                
            } catch (error) {
                console.error('Failed to get processing status:', error);
            }
        }
        
        this.renderQueue();
    }
    
    renderQueue() {
        const queueList = document.getElementById('queueList');
        const queueEl = document.getElementById('uploadQueue');
        
        if (this.uploadQueue.length === 0 && this.processingJobs.size === 0) {
            queueEl.style.display = 'none';
            return;
        }
        
        // Combine upload queue and processing jobs
        const allItems = [
            ...this.uploadQueue,
            ...Array.from(this.processingJobs.values())
        ];
        
        queueList.innerHTML = allItems.map(item => this.renderQueueItem(item)).join('');
    }
    
    renderQueueItem(item) {
        const statusIcons = {
            queued: '⏳',
            uploading: '📤',
            processing: '⚙️',
            completed: '✅',
            error: '❌'
        };
        
        const statusColors = {
            queued: '#6c757d',
            uploading: '#0088cc',
            processing: '#ff9800',
            completed: '#4caf50',
            error: '#f44336'
        };
        
        let progressHtml = '';
        if (item.status === 'uploading' || item.status === 'processing') {
            progressHtml = `
                <div class="progress-bar" style="width: 120px;">
                    <div class="progress-fill" style="width: ${item.progress}%"></div>
                </div>
            `;
        }
        
        let statusText = item.status.charAt(0).toUpperCase() + item.status.slice(1);
        if (item.status === 'processing' && item.processingMode) {
            statusText += ` (${item.processingMode})`;
        }
        
        let errorHtml = '';
        if (item.error) {
            errorHtml = `<p style="color: var(--error-color); font-size: 0.8rem;">${item.error}</p>`;
        }
        
        return `
            <div class="queue-item">
                <div class="queue-info">
                    <h4>${item.file.name}</h4>
                    <p>${this.app.formatBytes(item.file.size)}</p>
                    ${errorHtml}
                </div>
                <div class="queue-progress">
                    ${progressHtml}
                </div>
                <div class="queue-status">
                    <span style="color: ${statusColors[item.status]};">
                        ${statusIcons[item.status]} ${statusText}
                    </span>
                    ${item.status === 'completed' ? 
                        `<button class="btn btn-sm btn-secondary" onclick="app.streamingManager?.playVideo('${item.videoId}')">
                            ▶️ Play
                        </button>` : ''
                    }
                </div>
            </div>
        `;
    }
    
    showProcessingModal(queueItem) {
        const modalContent = `
            <div class="processing-details" id="processingDetails">
                <p><strong>File:</strong> <span id="processingFile">${queueItem.file.name}</span></p>
                <p><strong>Size:</strong> ${this.app.formatBytes(queueItem.file.size)}</p>
                <p><strong>Mode:</strong> <span id="processingModeDetail">Analyzing...</span></p>
                <p><strong>Status:</strong> <span id="processingStatus">Starting processing...</span></p>
            </div>
        `;
        
        this.app.openModal('processingModal', modalContent);
    }
    
    updateProcessingModal(queueItem, status) {
        const modal = document.getElementById('processingModal');
        if (!modal.classList.contains('active')) return;
        
        const progressFill = document.getElementById('progressFill');
        const progressText = document.getElementById('progressText');
        const processingMode = document.getElementById('processingModeDetail');
        const processingStatus = document.getElementById('processingStatus');
        
        if (progressFill) {
            progressFill.style.width = `${queueItem.progress}%`;
        }
        
        if (progressText) {
            progressText.textContent = `${queueItem.progress}% complete`;
        }
        
        if (processingMode && queueItem.processingMode) {
            processingMode.textContent = queueItem.processingMode;
        }
        
        if (processingStatus) {
            if (status.metadata) {
                const meta = status.metadata;
                let statusText = `Processing ${meta.video_streams} video, ${meta.audio_streams} audio`;
                if (meta.subtitle_streams > 0) {
                    statusText += `, ${meta.subtitle_streams} subtitle streams`;
                }
                processingStatus.textContent = statusText;
            } else {
                processingStatus.textContent = 'Processing...';
            }
        }
    }
    
    generateId() {
        return Math.random().toString(36).substr(2, 9);
    }
    
    clearCompleted() {
        this.uploadQueue = this.uploadQueue.filter(item => 
            item.status !== 'completed' && item.status !== 'error'
        );
        this.renderQueue();
    }
    
    retryFailed() {
        this.uploadQueue.forEach(item => {
            if (item.status === 'error') {
                item.status = 'queued';
                item.progress = 0;
                item.error = null;
            }
        });
        this.renderQueue();
        this.processQueue();
    }
}

// Make it globally available
window.UploadManager = UploadManager;