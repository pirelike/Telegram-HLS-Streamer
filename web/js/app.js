/**
 * Telegram HLS Streamer - Main Application Script
 * Handles core functionality, tabs, modals, and notifications
 */

class App {
    constructor() {
        this.baseUrl = window.location.origin;
        this.currentTab = 'upload';
        this.serverStatus = 'unknown';
        
        this.init();
    }
    
    init() {
        this.setupEventListeners();
        this.checkServerStatus();
        this.loadSystemConfig();
        
        // Initialize other modules
        if (window.UploadManager) {
            this.uploadManager = new UploadManager(this);
        }
        if (window.LibraryManager) {
            this.libraryManager = new LibraryManager(this);
        }
        if (window.StreamingManager) {
            this.streamingManager = new StreamingManager(this);
        }
        if (window.SystemManager) {
            this.systemManager = new SystemManager(this);
        }
        
        // Auto-refresh server status
        setInterval(() => this.checkServerStatus(), 30000); // Every 30 seconds
    }
    
    setupEventListeners() {
        // Tab navigation
        document.querySelectorAll('.tab-button').forEach(button => {
            button.addEventListener('click', (e) => {
                const tabName = e.target.dataset.tab;
                this.switchTab(tabName);
            });
        });
        
        // Modal handling
        this.setupModals();
        
        // Global keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            // Close modals with Escape
            if (e.key === 'Escape') {
                this.closeAllModals();
            }
            
            // Tab shortcuts (Ctrl/Cmd + number)
            if ((e.ctrlKey || e.metaKey) && e.key >= '1' && e.key <= '4') {
                e.preventDefault();
                const tabs = ['upload', 'library', 'streaming', 'system'];
                const tabIndex = parseInt(e.key) - 1;
                if (tabs[tabIndex]) {
                    this.switchTab(tabs[tabIndex]);
                }
            }
        });
    }
    
    setupModals() {
        // Close modals when clicking outside
        document.querySelectorAll('.modal').forEach(modal => {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) {
                    this.closeModal(modal.id);
                }
            });
        });
        
        // Close buttons
        document.querySelectorAll('.modal-close').forEach(button => {
            button.addEventListener('click', (e) => {
                const modal = e.target.closest('.modal');
                this.closeModal(modal.id);
            });
        });
    }
    
    switchTab(tabName) {
        // Update tab buttons
        document.querySelectorAll('.tab-button').forEach(button => {
            button.classList.remove('active');
        });
        document.querySelector(`[data-tab="${tabName}"]`).classList.add('active');
        
        // Update tab content
        document.querySelectorAll('.tab-content').forEach(content => {
            content.classList.remove('active');
        });
        document.getElementById(tabName).classList.add('active');
        
        this.currentTab = tabName;
        
        // Trigger tab-specific loading
        this.onTabSwitch(tabName);
    }
    
    onTabSwitch(tabName) {
        switch (tabName) {
            case 'library':
                if (this.libraryManager) {
                    this.libraryManager.loadVideos();
                }
                break;
            case 'streaming':
                if (this.streamingManager) {
                    this.streamingManager.loadAvailableVideos();
                }
                break;
            case 'system':
                if (this.systemManager) {
                    this.systemManager.loadSystemStatus();
                }
                break;
        }
    }
    
    async checkServerStatus() {
        try {
            const response = await this.apiRequest('/api/system/status');
            if (response.server_status === 'running') {
                this.updateServerStatus('online', 'Server Online');
                this.serverStatus = 'online';
            } else {
                this.updateServerStatus('warning', 'Server Issues');
                this.serverStatus = 'warning';
            }
        } catch (error) {
            this.updateServerStatus('error', 'Server Offline');
            this.serverStatus = 'offline';
            console.error('Server status check failed:', error);
        }
    }
    
    updateServerStatus(status, text) {
        const indicator = document.getElementById('serverStatus');
        const dot = indicator.querySelector('.status-dot');
        const textEl = indicator.querySelector('.status-text');
        
        // Remove existing status classes
        dot.classList.remove('error', 'warning');
        
        // Add new status
        if (status !== 'online') {
            dot.classList.add(status);
        }
        
        textEl.textContent = text;
    }
    
    async loadSystemConfig() {
        try {
            const config = await this.apiRequest('/api/config');
            
            // Update max file size display
            const maxSizeEl = document.getElementById('maxFileSize');
            if (maxSizeEl && config.config.limits) {
                maxSizeEl.textContent = `${config.config.limits.max_upload_size_gb} GB`;
            }
            
            this.config = config.config;
        } catch (error) {
            console.error('Failed to load config:', error);
        }
    }
    
    // API Helper Methods
    async apiRequest(endpoint, options = {}) {
        const url = `${this.baseUrl}${endpoint}`;
        const defaultOptions = {
            headers: {
                'Content-Type': 'application/json',
            },
            ...options
        };
        
        const response = await fetch(url, defaultOptions);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        
        const contentType = response.headers.get('content-type');
        if (contentType && contentType.includes('application/json')) {
            return await response.json();
        }
        
        return response;
    }
    
    async uploadFile(file, onProgress = null) {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('title', file.name.replace(/\.[^/.]+$/, "")); // Remove extension
        
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            
            // Progress tracking
            if (onProgress) {
                xhr.upload.addEventListener('progress', (e) => {
                    if (e.lengthComputable) {
                        const percentComplete = (e.loaded / e.total) * 100;
                        onProgress(percentComplete);
                    }
                });
            }
            
            xhr.addEventListener('load', () => {
                if (xhr.status === 200) {
                    try {
                        const response = JSON.parse(xhr.responseText);
                        resolve(response);
                    } catch (e) {
                        reject(new Error('Invalid JSON response'));
                    }
                } else {
                    reject(new Error(`Upload failed: ${xhr.statusText}`));
                }
            });
            
            xhr.addEventListener('error', () => {
                reject(new Error('Upload failed: Network error'));
            });
            
            xhr.open('POST', `${this.baseUrl}/api/upload`);
            xhr.send(formData);
        });
    }
    
    // Modal Management
    openModal(modalId, content = null) {
        const modal = document.getElementById(modalId);
        if (!modal) return;
        
        if (content) {
            const body = modal.querySelector('.modal-body');
            if (body) {
                body.innerHTML = content;
            }
        }
        
        modal.classList.add('active');
        document.body.style.overflow = 'hidden'; // Prevent background scrolling
    }
    
    closeModal(modalId) {
        const modal = document.getElementById(modalId);
        if (!modal) return;
        
        modal.classList.remove('active');
        document.body.style.overflow = ''; // Restore scrolling
    }
    
    closeAllModals() {
        document.querySelectorAll('.modal.active').forEach(modal => {
            this.closeModal(modal.id);
        });
    }
    
    // Toast Notifications
    showToast(type, title, message, duration = 5000) {
        const container = document.getElementById('toastContainer');
        
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        
        const icons = {
            success: '✅',
            warning: '⚠️',
            error: '❌',
            info: 'ℹ️'
        };
        
        toast.innerHTML = `
            <div class="toast-icon">${icons[type] || icons.info}</div>
            <div class="toast-content">
                <div class="toast-title">${title}</div>
                <div class="toast-message">${message}</div>
            </div>
        `;
        
        container.appendChild(toast);
        
        // Auto remove after duration
        setTimeout(() => {
            if (toast.parentNode) {
                toast.style.animation = 'toastSlideIn 0.3s ease reverse';
                setTimeout(() => {
                    container.removeChild(toast);
                }, 300);
            }
        }, duration);
        
        // Manual close on click
        toast.addEventListener('click', () => {
            if (toast.parentNode) {
                container.removeChild(toast);
            }
        });
    }
    
    // Utility Methods
    formatBytes(bytes, decimals = 2) {
        if (bytes === 0) return '0 Bytes';
        
        const k = 1024;
        const dm = decimals < 0 ? 0 : decimals;
        const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
        
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        
        return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
    }
    
    formatDuration(seconds) {
        if (!seconds || seconds < 0) return '0:00';
        
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const secs = Math.floor(seconds % 60);
        
        if (hours > 0) {
            return `${hours}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
        }
        
        return `${minutes}:${secs.toString().padStart(2, '0')}`;
    }
    
    formatDate(dateString) {
        const date = new Date(dateString);
        const now = new Date();
        const diff = now - date;
        
        // Less than a minute ago
        if (diff < 60000) {
            return 'Just now';
        }
        
        // Less than an hour ago
        if (diff < 3600000) {
            const minutes = Math.floor(diff / 60000);
            return `${minutes} minute${minutes !== 1 ? 's' : ''} ago`;
        }
        
        // Less than a day ago
        if (diff < 86400000) {
            const hours = Math.floor(diff / 3600000);
            return `${hours} hour${hours !== 1 ? 's' : ''} ago`;
        }
        
        // Less than a week ago
        if (diff < 604800000) {
            const days = Math.floor(diff / 86400000);
            return `${days} day${days !== 1 ? 's' : ''} ago`;
        }
        
        // Older than a week, show actual date
        return date.toLocaleDateString();
    }
    
    // Debounce function for search and other frequent operations
    debounce(func, wait, immediate) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                timeout = null;
                if (!immediate) func(...args);
            };
            const callNow = immediate && !timeout;
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
            if (callNow) func(...args);
        };
    }
    
    // Copy text to clipboard
    async copyToClipboard(text) {
        try {
            await navigator.clipboard.writeText(text);
            this.showToast('success', 'Copied!', 'Text copied to clipboard');
        } catch (error) {
            // Fallback for older browsers
            const textArea = document.createElement('textarea');
            textArea.value = text;
            document.body.appendChild(textArea);
            textArea.select();
            try {
                document.execCommand('copy');
                this.showToast('success', 'Copied!', 'Text copied to clipboard');
            } catch (e) {
                this.showToast('error', 'Copy Failed', 'Could not copy to clipboard');
            }
            document.body.removeChild(textArea);
        }
    }
    
    // Generate streaming URLs
    getStreamingUrl(videoId) {
        return `${this.baseUrl}/hls/${videoId}/master.m3u8`;
    }
    
    getSegmentUrl(videoId, segmentName) {
        return `${this.baseUrl}/hls/${videoId}/segments/${segmentName}`;
    }
}

// Initialize the application when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.app = new App();
});

// Export for module usage
if (typeof module !== 'undefined' && module.exports) {
    module.exports = App;
}