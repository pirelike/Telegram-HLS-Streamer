/**
 * System Manager - Handles system monitoring and configuration
 */

class SystemManager {
    constructor(app) {
        this.app = app;
        
        this.init();
    }
    
    init() {
        this.setupEventListeners();
    }
    
    setupEventListeners() {
        const refreshBtn = document.getElementById('refreshSystem');
        const testBotsBtn = document.getElementById('testBots');
        const clearCacheBtn = document.getElementById('clearCache');
        
        if (refreshBtn) {
            refreshBtn.addEventListener('click', () => this.loadSystemStatus());
        }
        
        if (testBotsBtn) {
            testBotsBtn.addEventListener('click', () => this.testBots());
        }
        
        if (clearCacheBtn) {
            clearCacheBtn.addEventListener('click', () => this.clearCache());
        }
    }
    
    async loadSystemStatus() {
        await Promise.all([
            this.loadBotStats(),
            this.loadCacheStats(),
            this.loadDatabaseStats(),
            this.loadConfigStats()
        ]);
    }
    
    async loadBotStats() {
        const container = document.getElementById('botStats');
        if (!container) return;
        
        try {
            const response = await this.app.apiRequest('/api/system/bots/status');
            const botStats = response.bot_stats;
            const botHealth = response.bot_health;
            
            container.innerHTML = `
                <div class="stat-item">
                    <span class="stat-label">Total Bots:</span>
                    <span class="stat-value">${botStats.total_bots}</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">Healthy Bots:</span>
                    <span class="stat-value ${botHealth.healthy_bots === botHealth.total_bots ? 'success' : 'warning'}">
                        ${botHealth.healthy_bots}/${botHealth.total_bots}
                    </span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">Total Storage:</span>
                    <span class="stat-value">${this.getTotalStorage(botStats.bots)} MB</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">Total Segments:</span>
                    <span class="stat-value">${this.getTotalSegments(botStats.bots)}</span>
                </div>
            `;
            
        } catch (error) {
            console.error('Failed to load bot stats:', error);
            container.innerHTML = '<div class="error">Failed to load bot statistics</div>';
        }
    }
    
    async loadCacheStats() {
        const container = document.getElementById('cacheStats');
        if (!container) return;
        
        try {
            const response = await this.app.apiRequest('/api/system/cache/stats');
            const stats = response.cache_stats;
            
            container.innerHTML = `
                <div class="stat-item">
                    <span class="stat-label">Hit Ratio:</span>
                    <span class="stat-value ${this.getHitRatioClass(stats.hit_ratio)}">
                        ${(stats.hit_ratio * 100).toFixed(1)}%
                    </span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">Cache Size:</span>
                    <span class="stat-value">${stats.size_mb.toFixed(1)} MB</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">Cached Items:</span>
                    <span class="stat-value">${stats.entries}</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">Data Served:</span>
                    <span class="stat-value">${stats.total_mb_served.toFixed(1)} MB</span>
                </div>
            `;
            
        } catch (error) {
            console.error('Failed to load cache stats:', error);
            container.innerHTML = '<div class="error">Failed to load cache statistics</div>';
        }
    }
    
    async loadDatabaseStats() {
        const container = document.getElementById('databaseStats');
        if (!container) return;
        
        try {
            const response = await this.app.apiRequest('/api/system/status');
            const dbStats = response.database;
            
            container.innerHTML = `
                <div class="stat-item">
                    <span class="stat-label">Total Videos:</span>
                    <span class="stat-value">${this.getTotalVideos(dbStats.videos)}</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">Completed:</span>
                    <span class="stat-value success">${dbStats.videos.completed || 0}</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">Processing:</span>
                    <span class="stat-value warning">${dbStats.videos.processing || 0}</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">Total Segments:</span>
                    <span class="stat-value">${dbStats.total_segments || 0}</span>
                </div>
            `;
            
        } catch (error) {
            console.error('Failed to load database stats:', error);
            container.innerHTML = '<div class="error">Failed to load database statistics</div>';
        }
    }
    
    async loadConfigStats() {
        const container = document.getElementById('configStats');
        if (!container) return;
        
        try {
            const response = await this.app.apiRequest('/api/config');
            const config = response.config;
            
            container.innerHTML = `
                <div class="stat-item">
                    <span class="stat-label">Server:</span>
                    <span class="stat-value">${config.server.host}:${config.server.port}</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">HTTPS:</span>
                    <span class="stat-value ${config.server.force_https ? 'success' : 'warning'}">
                        ${config.server.force_https ? 'Enabled' : 'Disabled'}
                    </span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">Copy Mode:</span>
                    <span class="stat-value ${config.processing.copy_mode ? 'success' : 'warning'}">
                        ${config.processing.copy_mode ? 'Enabled' : 'Disabled'}
                    </span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">HW Accel:</span>
                    <span class="stat-value">${config.processing.hardware_accel}</span>
                </div>
            `;
            
        } catch (error) {
            console.error('Failed to load config stats:', error);
            container.innerHTML = '<div class="error">Failed to load configuration</div>';
        }
    }
    
    async testBots() {
        const testBtn = document.getElementById('testBots');
        if (testBtn) {
            testBtn.disabled = true;
            testBtn.textContent = 'Testing...';
        }
        
        try {
            const response = await this.app.apiRequest('/api/system/bots/test', {
                method: 'POST'
            });
            
            const results = response.test_results;
            const working = results.filter(r => r.status === 'success').length;
            const total = results.length;
            
            if (working === total) {
                this.app.showToast('success', 'Bot Test', `All ${total} bots are working perfectly!`);
            } else if (working > 0) {
                this.app.showToast('warning', 'Bot Test', `${working}/${total} bots are working`);
            } else {
                this.app.showToast('error', 'Bot Test', 'No bots are working properly');
            }
            
            // Refresh bot stats
            await this.loadBotStats();
            
        } catch (error) {
            console.error('Bot test failed:', error);
            this.app.showToast('error', 'Test Failed', 'Could not test bot connectivity');
        } finally {
            if (testBtn) {
                testBtn.disabled = false;
                testBtn.textContent = 'Test Bots';
            }
        }
    }
    
    async clearCache() {
        if (!confirm('Are you sure you want to clear the cache? This will temporarily slow down video streaming.')) {
            return;
        }
        
        const clearBtn = document.getElementById('clearCache');
        if (clearBtn) {
            clearBtn.disabled = true;
            clearBtn.textContent = 'Clearing...';
        }
        
        try {
            await this.app.apiRequest('/api/system/cache/clear', {
                method: 'POST'
            });
            
            this.app.showToast('success', 'Cache Cleared', 'Cache has been cleared successfully');
            
            // Refresh cache stats
            await this.loadCacheStats();
            
        } catch (error) {
            console.error('Failed to clear cache:', error);
            this.app.showToast('error', 'Clear Failed', 'Could not clear cache');
        } finally {
            if (clearBtn) {
                clearBtn.disabled = false;
                clearBtn.textContent = 'Clear Cache';
            }
        }
    }
    
    // Helper methods
    getTotalStorage(bots) {
        if (!Array.isArray(bots)) return 0;
        return bots.reduce((total, bot) => total + (bot.total_size_mb || 0), 0).toFixed(1);
    }
    
    getTotalSegments(bots) {
        if (!Array.isArray(bots)) return 0;
        return bots.reduce((total, bot) => total + (bot.segment_count || 0), 0);
    }
    
    getTotalVideos(videos) {
        if (!videos || typeof videos !== 'object') return 0;
        return Object.values(videos).reduce((total, count) => total + (count || 0), 0);
    }
    
    getHitRatioClass(ratio) {
        if (ratio >= 0.8) return 'success';
        if (ratio >= 0.6) return 'warning';
        return 'error';
    }
}

// Make it globally available
window.SystemManager = SystemManager;