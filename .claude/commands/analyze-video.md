Analyze a video file and show what the processing pipeline would do.

Usage: /analyze-video <path-to-video-file>

1. Run `ffprobe -v quiet -print_format json -show_format -show_streams` on the provided file path: $ARGUMENTS
2. Parse and display:
   - Container format, duration, total file size
   - Video streams: codec, resolution, bitrate, frame rate
   - Audio streams: codec, channels, sample rate, language, title
   - Subtitle streams: codec, language, title, whether text-based (extractable to WebVTT)
3. Determine what the pipeline would produce:
   - Whether video would use copy mode or re-encode (based on codec compatibility)
   - Which ABR tiers would be generated (based on source resolution vs ABR_TIERS)
   - Which audio tracks would use copy mode vs AAC re-encode
   - Which subtitle tracks would be extracted vs skipped (bitmap subtitles)
4. Estimate number of HLS segments (duration / HLS_SEGMENT_DURATION)
5. Estimate total files to upload to Telegram (video segments per tier + audio segments per track + subtitle files)
