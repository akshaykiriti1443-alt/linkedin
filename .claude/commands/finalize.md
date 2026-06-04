# /finalize — Stitch everything + SFX + captions → final reel

You are a post-production engineer. All assets should exist in `video-bot/out/` and `video-bot/workspace/`.

## Steps

1. Run the finalize script:
   ```bash
   cd video-bot
   tsx src/finalize.ts
   ```

2. This script builds the FFmpeg complex filter that:
   a. Takes `workspace/rough_cut.mp4` as base (9:16, face in bottom 50%)
   b. Overlays `out/top_animations.mov` on top 50% (x:0, y:0)
   c. Overlays `out/overlay.mov` as alpha composite over full frame
   d. Mixes SFX audio at exact transcript timestamps:
      - Transition phrases → `assets/sfx/whoosh.mp3`
      - UI/click words → `assets/sfx/click.mp3`
      - Code/typing words → `assets/sfx/typing.mp3`
   e. Burns styled captions from `workspace/transcript.srt` into bottom-center safe zone
   f. Outputs `out/final_reel.mp4` at 1080×1920, ready to post

3. Print final stats:
   - Output file size
   - Duration
   - Resolution confirmed as 1080×1920
   - "Ready to post to Instagram Reels / TikTok / YouTube Shorts"

## FFmpeg filter structure
```
# Layer: base + top half animation + transparent overlay + SFX mix
ffmpeg \
  -i workspace/rough_cut.mp4 \
  -i out/top_animations.mov \
  -i out/overlay.mov \
  -i assets/sfx/whoosh.mp3 \
  -filter_complex "
    [0:v][1:v]overlay=0:0[v1];
    [v1][2:v]overlay=0:0[v2];
    [0:a][3:a]amix=inputs=2:duration=first[aout]
  " \
  -map "[v2]" -map "[aout]" \
  -vf "subtitles=workspace/transcript.srt:force_style='FontName=Arial,FontSize=24,Bold=1,PrimaryColour=&Hffffff,OutlineColour=&H000000,BorderStyle=3'" \
  -c:v libx264 -preset fast -crf 18 \
  -c:a aac -b:a 192k \
  out/final_reel.mp4
```

## Notes
- If any input file is missing, stop and tell the user which step to run first
- SFX volume: mix at -18dB so they don't overpower the voice
- Captions: white text, black outline, bottom 10% of frame
