# /transcribe-premiere — Transcribe using Premiere Pro's built-in AI (no pip install)

Use this instead of faster-whisper if pip install is giving you trouble.
Requires Premiere Pro to be open and the Premiere MCP connected.

## Steps

1. Ask the user: "What is the full path to your video file?"
   Example: C:\Users\Name\Videos\my_video.mp4

2. Import the video into Premiere Pro:
   Use mcp__premiere-pro-local__import_media with the video path.

3. Create a new sequence from the clip:
   Use mcp__premiere-pro-local__create_sequence_from_clips

4. Trigger Premiere's built-in speech-to-text via ExtendScript:
   Use mcp__premiere-pro-local__execute_extendscript with this script:
   ```javascript
   var seq = app.project.activeSequence;
   if (!seq) { "NO_SEQUENCE"; }
   else {
     try {
       seq.autoTranscribeSequence(
         Sequence.TRANSCRIPTION_LANGUAGE_ENGLISH,
         false, false
       );
       "TRANSCRIPTION_STARTED";
     } catch(e) { "ERROR: " + e.message; }
   }
   ```

5. Wait 30-60 seconds, then read the transcript back:
   Use mcp__premiere-pro-local__execute_extendscript with:
   ```javascript
   var seq = app.project.activeSequence;
   var result = [];
   for (var i = 0; i < seq.videoTracks.numTracks; i++) {
     var track = seq.videoTracks[i];
     for (var j = 0; j < track.clips.numItems; j++) {
       var clip = track.clips[j];
       result.push({
         start: clip.start.seconds,
         end: clip.end.seconds,
         text: clip.name
       });
     }
   }
   JSON.stringify({duration: seq.end.seconds, clips: result});
   ```

6. Convert to transcript.json and write to video-bot\workspace\transcript.json.
   For each clip segment, distribute words evenly across the time range.
   Format:
   ```json
   {
     "language": "en",
     "duration": 120.5,
     "segments": [
       {
         "id": 0, "start": 0.0, "end": 4.2,
         "text": "So today I want to talk about",
         "words": [
           {"word": "So", "start": 0.0, "end": 0.3, "probability": 0.99},
           {"word": "today", "start": 0.3, "end": 0.6, "probability": 0.99}
         ]
       }
     ]
   }
   ```

7. Tell the user transcript is saved, then say:
   "Now run run.bat — it will detect the transcript and skip the transcription step automatically."
