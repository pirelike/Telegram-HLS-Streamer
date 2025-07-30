#!/usr/bin/env python3
"""
Hardware Acceleration Support for Video Processing

This module provides comprehensive hardware acceleration support for:
- NVIDIA GPUs (NVENC/NVDEC)
- Intel GPUs (QuickSync/VAAPI) 
- AMD GPUs (VCE/VAAPI)
- macOS VideoToolbox

It automatically detects available hardware and provides optimized FFmpeg parameters.
"""

import os
import subprocess
import platform
import re
from typing import Dict, List, Optional, Tuple
from src.utils.logging import logger


class HardwareAccelerator:
    """Hardware acceleration detection and configuration."""
    
    def __init__(self):
        self.system = platform.system().lower()
        self.available_accelerators = {}
        self.preferred_accel = None
        self._detect_hardware()
    
    def _detect_hardware(self):
        """Detect available hardware acceleration methods."""
        logger.info("🔍 Detecting hardware acceleration capabilities...")
        
        # Check FFmpeg codecs
        self._check_ffmpeg_codecs()
        
        # Detect GPU hardware
        if self.system == "linux":
            self._detect_linux_gpus()
        elif self.system == "windows":
            self._detect_windows_gpus()
        elif self.system == "darwin":
            self._detect_macos_acceleration()
        
        # Determine preferred accelerator
        self._select_preferred_accelerator()
        
        if self.available_accelerators:
            logger.info(f"✅ Hardware acceleration available: {list(self.available_accelerators.keys())}")
            if self.preferred_accel:
                logger.info(f"🚀 Preferred accelerator: {self.preferred_accel}")
        else:
            logger.info("ℹ️ No hardware acceleration detected, using software encoding")
    
    def _check_ffmpeg_codecs(self):
        """Check which hardware codecs are available in FFmpeg."""
        try:
            result = subprocess.run(['ffmpeg', '-codecs'], 
                                  capture_output=True, text=True, timeout=10)
            codecs_output = result.stdout
            
            # Check for hardware encoders
            hw_codecs = {
                'nvenc': ['h264_nvenc', 'hevc_nvenc'],
                'vaapi': ['h264_vaapi', 'hevc_vaapi'], 
                'qsv': ['h264_qsv', 'hevc_qsv'],
                'videotoolbox': ['h264_videotoolbox', 'hevc_videotoolbox'],
                'amf': ['h264_amf', 'hevc_amf']
            }
            
            for accel_type, codec_list in hw_codecs.items():
                available_codecs = []
                for codec in codec_list:
                    if codec in codecs_output:
                        available_codecs.append(codec)
                
                if available_codecs:
                    self.available_accelerators[accel_type] = {
                        'codecs': available_codecs,
                        'type': accel_type
                    }
                    
        except Exception as e:
            logger.warning(f"Could not check FFmpeg codecs: {e}")
    
    def _detect_linux_gpus(self):
        """Detect GPU hardware on Linux."""
        try:
            # Check for NVIDIA GPU
            try:
                subprocess.run(['nvidia-smi'], capture_output=True, check=True, timeout=5)
                if 'nvenc' in self.available_accelerators:
                    self.available_accelerators['nvenc']['gpu_detected'] = True
                    logger.info("🟢 NVIDIA GPU detected")
            except (subprocess.CalledProcessError, FileNotFoundError):
                if 'nvenc' in self.available_accelerators:
                    self.available_accelerators['nvenc']['gpu_detected'] = False
            
            # Check for Intel GPU (integrated)
            intel_gpu_paths = ['/dev/dri/renderD128', '/dev/dri/card0']
            intel_detected = any(os.path.exists(path) for path in intel_gpu_paths)
            
            if intel_detected and ('vaapi' in self.available_accelerators or 'qsv' in self.available_accelerators):
                if 'vaapi' in self.available_accelerators:
                    self.available_accelerators['vaapi']['gpu_detected'] = True
                if 'qsv' in self.available_accelerators:
                    self.available_accelerators['qsv']['gpu_detected'] = True
                logger.info("🔵 Intel GPU detected")
            
            # Check for AMD GPU
            try:
                result = subprocess.run(['lspci'], capture_output=True, text=True, timeout=5)
                if 'AMD' in result.stdout or 'ATI' in result.stdout:
                    if 'vaapi' in self.available_accelerators:
                        self.available_accelerators['vaapi']['gpu_detected'] = True
                    if 'amf' in self.available_accelerators:
                        self.available_accelerators['amf']['gpu_detected'] = True
                    logger.info("🔴 AMD GPU detected")
            except Exception:
                pass
                
        except Exception as e:
            logger.warning(f"GPU detection failed: {e}")
    
    def _detect_windows_gpus(self):
        """Detect GPU hardware on Windows."""
        try:
            # Use wmic to detect GPUs
            result = subprocess.run(['wmic', 'path', 'win32_VideoController', 'get', 'name'], 
                                  capture_output=True, text=True, timeout=10)
            gpu_info = result.stdout.lower()
            
            if 'nvidia' in gpu_info and 'nvenc' in self.available_accelerators:
                self.available_accelerators['nvenc']['gpu_detected'] = True
                logger.info("🟢 NVIDIA GPU detected")
            
            if 'intel' in gpu_info:
                if 'qsv' in self.available_accelerators:
                    self.available_accelerators['qsv']['gpu_detected'] = True
                if 'vaapi' in self.available_accelerators:
                    self.available_accelerators['vaapi']['gpu_detected'] = True
                logger.info("🔵 Intel GPU detected")
            
            if ('amd' in gpu_info or 'radeon' in gpu_info):
                if 'amf' in self.available_accelerators:
                    self.available_accelerators['amf']['gpu_detected'] = True
                if 'vaapi' in self.available_accelerators:
                    self.available_accelerators['vaapi']['gpu_detected'] = True
                logger.info("🔴 AMD GPU detected")
                
        except Exception as e:
            logger.warning(f"Windows GPU detection failed: {e}")
    
    def _detect_macos_acceleration(self):
        """Detect VideoToolbox acceleration on macOS."""
        if 'videotoolbox' in self.available_accelerators:
            self.available_accelerators['videotoolbox']['gpu_detected'] = True
            logger.info("🍎 macOS VideoToolbox detected")
    
    def _select_preferred_accelerator(self):
        """Select the best available hardware accelerator."""
        # Priority order based on performance and compatibility
        priority_order = ['nvenc', 'qsv', 'videotoolbox', 'amf', 'vaapi']
        
        for accel_type in priority_order:
            if (accel_type in self.available_accelerators and 
                self.available_accelerators[accel_type].get('gpu_detected', False)):
                self.preferred_accel = accel_type
                break
    
    def is_available(self, accel_type: str = None) -> bool:
        """Check if hardware acceleration is available."""
        if accel_type:
            return (accel_type in self.available_accelerators and 
                   self.available_accelerators[accel_type].get('gpu_detected', False))
        return bool(self.preferred_accel)
    
    def get_acceleration_params(self, accel_type: str = None, quality_preset: str = 'balanced') -> Dict:
        """Get FFmpeg parameters for hardware acceleration."""
        if not accel_type:
            accel_type = self.preferred_accel
        
        if not self.is_available(accel_type):
            return self._get_software_params(quality_preset)
        
        params_map = {
            'nvenc': self._get_nvenc_params,
            'qsv': self._get_qsv_params,
            'vaapi': self._get_vaapi_params,
            'videotoolbox': self._get_videotoolbox_params,
            'amf': self._get_amf_params
        }
        
        if accel_type in params_map:
            return params_map[accel_type](quality_preset)
        
        return self._get_software_params(quality_preset)
    
    def _get_nvenc_params(self, quality_preset: str) -> Dict:
        """Get NVIDIA NVENC parameters."""
        base_params = {
            'video_codec': 'h264_nvenc',
            'hwaccel': 'cuda',
            'hwaccel_output_format': 'cuda',
            'extra_input_args': ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'],
            'extra_output_args': []
        }
        
        if quality_preset == 'fast':
            base_params['extra_output_args'].extend([
                '-preset', 'p1',  # Fastest
                '-tune', 'hq',
                '-rc', 'vbr',
                '-cq', '28'
            ])
        elif quality_preset == 'quality':
            base_params['extra_output_args'].extend([
                '-preset', 'p7',  # Slowest but best quality
                '-tune', 'hq',
                '-rc', 'vbr',
                '-cq', '20'
            ])
        else:  # balanced
            base_params['extra_output_args'].extend([
                '-preset', 'p4',  # Medium
                '-tune', 'hq',
                '-rc', 'vbr',
                '-cq', '24'
            ])
        
        return base_params
    
    def _get_qsv_params(self, quality_preset: str) -> Dict:
        """Get Intel QuickSync parameters."""
        base_params = {
            'video_codec': 'h264_qsv',
            'hwaccel': 'qsv',
            'hwaccel_output_format': 'qsv',
            'extra_input_args': ['-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv'],
            'extra_output_args': []
        }
        
        if quality_preset == 'fast':
            base_params['extra_output_args'].extend([
                '-preset', 'veryfast',
                '-global_quality', '28'
            ])
        elif quality_preset == 'quality':
            base_params['extra_output_args'].extend([
                '-preset', 'veryslow',
                '-global_quality', '20'
            ])
        else:  # balanced
            base_params['extra_output_args'].extend([
                '-preset', 'medium',
                '-global_quality', '24'
            ])
        
        return base_params
    
    def _get_vaapi_params(self, quality_preset: str) -> Dict:
        """Get VAAPI parameters (Intel/AMD on Linux)."""
        base_params = {
            'video_codec': 'h264_vaapi',
            'hwaccel': 'vaapi',
            'hwaccel_device': '/dev/dri/renderD128',
            'hwaccel_output_format': 'vaapi',
            'extra_input_args': [
                '-hwaccel', 'vaapi',
                '-hwaccel_device', '/dev/dri/renderD128',
                '-hwaccel_output_format', 'vaapi'
            ],
            'extra_output_args': ['-vf', 'format=nv12,hwupload']
        }
        
        if quality_preset == 'fast':
            base_params['extra_output_args'].extend(['-compression_level', '1'])
        elif quality_preset == 'quality':
            base_params['extra_output_args'].extend(['-compression_level', '7'])
        else:  # balanced
            base_params['extra_output_args'].extend(['-compression_level', '4'])
        
        return base_params
    
    def _get_videotoolbox_params(self, quality_preset: str) -> Dict:
        """Get macOS VideoToolbox parameters."""
        base_params = {
            'video_codec': 'h264_videotoolbox',
            'hwaccel': 'videotoolbox',
            'extra_input_args': ['-hwaccel', 'videotoolbox'],
            'extra_output_args': []
        }
        
        if quality_preset == 'fast':
            base_params['extra_output_args'].extend([
                '-q:v', '60',
                '-realtime', '1'
            ])
        elif quality_preset == 'quality':
            base_params['extra_output_args'].extend([
                '-q:v', '40',
                '-realtime', '0'
            ])
        else:  # balanced
            base_params['extra_output_args'].extend([
                '-q:v', '50',
                '-realtime', '0'
            ])
        
        return base_params
    
    def _get_amf_params(self, quality_preset: str) -> Dict:
        """Get AMD AMF parameters."""
        base_params = {
            'video_codec': 'h264_amf',
            'extra_input_args': [],
            'extra_output_args': []
        }
        
        if quality_preset == 'fast':
            base_params['extra_output_args'].extend([
                '-quality', 'speed',
                '-rc', 'cqp',
                '-qp_i', '28',
                '-qp_p', '28'
            ])
        elif quality_preset == 'quality':
            base_params['extra_output_args'].extend([
                '-quality', 'quality',
                '-rc', 'cqp',
                '-qp_i', '20',
                '-qp_p', '20'
            ])
        else:  # balanced
            base_params['extra_output_args'].extend([
                '-quality', 'balanced',
                '-rc', 'cqp',
                '-qp_i', '24',
                '-qp_p', '24'
            ])
        
        return base_params
    
    def _get_software_params(self, quality_preset: str) -> Dict:
        """Get software encoding parameters as fallback."""
        base_params = {
            'video_codec': 'libx264',
            'extra_input_args': [],
            'extra_output_args': []
        }
        
        if quality_preset == 'fast':
            base_params['extra_output_args'].extend([
                '-preset', 'veryfast',
                '-crf', '28'
            ])
        elif quality_preset == 'quality':
            base_params['extra_output_args'].extend([
                '-preset', 'slow',
                '-crf', '20'
            ])
        else:  # balanced
            base_params['extra_output_args'].extend([
                '-preset', 'fast',
                '-crf', '23'
            ])
        
        return base_params
    
    def get_status_report(self) -> Dict:
        """Get detailed status report of hardware acceleration."""
        return {
            'system': self.system,
            'available_accelerators': self.available_accelerators,
            'preferred_accelerator': self.preferred_accel,
            'has_hardware_accel': bool(self.preferred_accel)
        }


# Global instance
_hardware_accelerator = None

def get_hardware_accelerator() -> HardwareAccelerator:
    """Get the global hardware accelerator instance."""
    global _hardware_accelerator
    if _hardware_accelerator is None:
        _hardware_accelerator = HardwareAccelerator()
    return _hardware_accelerator

def is_hardware_acceleration_available(accel_type: str = None) -> bool:
    """Check if hardware acceleration is available."""
    return get_hardware_accelerator().is_available(accel_type)

def get_acceleration_params(accel_type: str = None, quality_preset: str = 'balanced') -> Dict:
    """Get FFmpeg parameters for hardware acceleration."""
    return get_hardware_accelerator().get_acceleration_params(accel_type, quality_preset)