/**
 * Streaming Manager - Handles HLS video playback
 */

class StreamingManager {
    constructor(app) {
        this.app = app;
        this.player = null;
        this.currentVideoId = null;
        
        this.init();
    }
    
    init() {
        this.setupEventListeners();
        this.setupPlayer();
    }
    
    setupEventListeners() {
        const videoSelect = document.getElementById('videoSelect');
        
        if (videoSelect) {
            videoSelect.addEventListener('change', (e) => {
                if (e.target.value) {
                    this.playVideo(e.target.value);
                }
            });
        }
    }
    
    setupPlayer() {
        this.player = document.getElementById('hlsPlayer');
        
        if (this.player) {
            this.player.addEventListener('loadstart', () => {
                console.log('Video loading started');
            });
            
            this.player.addEventListener('loadeddata', () => {
                console.log('Video data loaded');
                this.updatePlayerInfo();
            });
            
            this.player.addEventListener('error', (e) => {
                console.error('Video playback error:', e);
                this.app.showToast('error', 'Playback Error', 'Could not play video');
            });
        }
    }
    
    async loadAvailableVideos() {
        const videoSelect = document.getElementById('videoSelect');
        if (!videoSelect) return;
        
        try {
            const response = await this.app.apiRequest('/api/videos?status=completed');
            const videos = response.videos || [];
            
            videoSelect.innerHTML = '<option value="">Select a video...</option>';
            
            videos.forEach(video => {
                const option = document.createElement('option');
                option.value = video.video_id;
                option.textContent = `${video.title} (${this.app.formatDuration(video.duration)})`;
                videoSelect.appendChild(option);
            });
            
            videoSelect.disabled = videos.length === 0;
            
        } catch (error) {
            console.error('Failed to load videos:', error);
            videoSelect.innerHTML = '<option value="">Error loading videos</option>';
            videoSelect.disabled = true;
        }
    }
    
    async playVideo(videoId) {
        if (!this.player || !videoId) return;
        
        this.currentVideoId = videoId;
        
        // Get video details first
        try {
            const response = await this.app.apiRequest(`/api/videos/${videoId}`);
            const video = response.video;
            
            if (video.status !== 'completed') {
                this.app.showToast('warning', 'Video Not Ready', 'Video is still processing');
                return;
            }
            
            // Set up HLS source
            const streamUrl = this.app.getStreamingUrl(videoId);
            
            // Check if browser supports HLS natively
            if (this.player.canPlayType('application/vnd.apple.mpegurl')) {
                this.player.src = streamUrl;
            } else if (window.Hls && window.Hls.isSupported()) {
                // Use HLS.js for browsers that don't support HLS natively
                const hls = new window.Hls();
                hls.loadSource(streamUrl);
                hls.attachMedia(this.player);
                
                hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
                    console.log('HLS manifest loaded');
                });
                
                hls.on(window.Hls.Events.ERROR, (event, data) => {
                    console.error('HLS error:', data);
                    if (data.fatal) {
                        this.app.showToast('error', 'Streaming Error', 'Failed to load video stream');
                    }
                });
                
            } else {
                // Fallback: try direct playback
                this.player.src = streamUrl;
            }
            
            // Update player info
            this.updatePlayerInfo(video);
            
            // Switch to streaming tab if not already there
            if (this.app.currentTab !== 'streaming') {
                this.app.switchTab('streaming');
            }
            
            this.app.showToast('info', 'Loading Video', `Loading ${video.title}...`);
            
        } catch (error) {
            console.error('Failed to play video:', error);
            this.app.showToast('error', 'Playback Failed', 'Could not load video');
        }
    }
    
    updatePlayerInfo(video = null) {
        const playerInfo = document.getElementById('playerInfo');
        const currentQuality = document.getElementById('currentQuality');
        const processingMode = document.getElementById('processingMode');
        
        if (!playerInfo) return;
        
        if (video) {
            playerInfo.style.display = 'block';
            
            if (currentQuality) {
                // This would be updated based on HLS quality selection
                currentQuality.textContent = 'Auto';
            }
            
            if (processingMode && video.metadata) {
                processingMode.textContent = video.metadata.used_copy_mode ? 'Copy Mode' : 'Transcoded';
            }
        } else {
            playerInfo.style.display = 'none';
        }
    }
    
    getCurrentVideoId() {
        return this.currentVideoId;
    }
    
    isPlaying() {
        return this.player && !this.player.paused && !this.player.ended;
    }
    
    pause() {
        if (this.player) {
            this.player.pause();
        }
    }
    
    play() {
        if (this.player) {
            this.player.play().catch(error => {
                console.error('Play failed:', error);
                this.app.showToast('error', 'Playback Error', 'Could not start playback');
            });
        }
    }
    
    stop() {
        if (this.player) {
            this.player.pause();
            this.player.src = '';
            this.currentVideoId = null;
            this.updatePlayerInfo();
        }
    }
    
    setVolume(volume) {
        if (this.player) {
            this.player.volume = Math.max(0, Math.min(1, volume));
        }
    }
    
    seek(time) {
        if (this.player) {
            this.player.currentTime = time;
        }
    }
}

// Make it globally available
window.StreamingManager = StreamingManager;