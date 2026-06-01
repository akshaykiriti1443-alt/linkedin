# /produce — Rough cut: silence removal, retake removal, 9:16 reformat

You are an expert FFmpeg video editor. Use the transcript at `video-bot/workspace/transcript.json`.

## Steps

1. Run the produce script:
   ```bash
   cd video-bot
   tsx src/produce.ts
   ```

2. This script:
   - Reads `workspace/transcript.json`
   - Detects silences > 0.5s between words
   - Detects retakes (Claude analyzes repeated phrases in transcript)
   - Generates an FFmpeg concat filter that splices them all out
   - Reformats from 16:9 → 9:16 (1080x1920):
     - Crops speaker face into bottom 50% (y: 960–1920)
     - Top 50% (y: 0–960) = black, ready for animations
   - Outputs `workspace/rough_cut.mp4`

3. After the script runs, read and print the edit report from `workspace/edit_report.json`:
   - How many silences were cut
   - How many retakes were removed
   - Final duration vs original duration
   - Time saved

4. Tell the user: "Run /new-series to generate top-half animations from your transcript."

## Key FFmpeg filter logic
```
# Crop face to bottom half of 9:16 frame
[0:v]crop=ih*(9/16):ih,scale=1080:1920,pad=1080:1920:0:960:black[bottom]
# Combine with black top half
color=black:1080x960[top]
[top][bottom]vstack[out]
```

## Notes
- Always preview the edit report before claiming done
- If a retake detection looks wrong, show the user the flagged phrases and ask to confirm before cutting
