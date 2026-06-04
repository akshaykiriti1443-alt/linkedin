# /transcribe — Transcribe raw video with Whisper

You are an AI video editor. The user has provided a raw video file path.

## Steps

1. Run local Whisper on the video to get word-level timestamps:
   ```bash
   cd video-bot
   tsx src/transcribe.ts --video="$ARGUMENTS"
   ```

2. This produces:
   - `workspace/transcript.json` — word-level timestamps (Whisper JSON)
   - `workspace/transcript.srt` — SRT captions
   - `workspace/transcript.txt` — plain text

3. Read `workspace/transcript.json` and print a summary:
   - Total duration
   - Word count
   - Any silences > 1 second (flag them)
   - Any likely retakes (repeated phrases)

4. Tell the user:
   - Transcription is complete
   - How many silences and retakes were detected
   - "Run /produce to cut them and reformat to 9:16"

## Notes
- If Whisper is not installed, print install instructions and stop
- Word-level timestamps require `--word_timestamps True` flag
- Model: use `base` for speed, `small` for accuracy, `medium` for best quality
