# ClipForge Local

ClipForge is a local-first Windows desktop video clipping studio. It reuses installed Ollama models for story analysis, runs FFmpeg and yt-dlp from the machine, and stores projects under `Videos\ClipForge` by default.

## Your machine and detected setup

- HP Victus, Ryzen 5 5600H, 8 GB RAM
- NVIDIA GeForce GTX 1650, 4 GB VRAM (NVENC available; rendering defaults to CPU x264 for reliability)
- Ollama 0.40.16, with `qwen3:4b-instruct`, `qwen3-vl:2b-instruct`, and `qwen2.5-coder:1.5b`
- FFmpeg/FFprobe and yt-dlp are installed
- Qwen3 4B responded through `http://127.0.0.1:11434/api/generate`
- Faster-Whisper was not installed during the initial inspection. The app offers to install its Python package with a compatible PyAV version. It downloads the selected Base speech model only when processing the first video.

## Start on Windows

1. Install Python 3.11 or newer if it is not already on PATH.
2. Double-click **Run ClipForge.bat**. It installs the desktop dependencies on first launch and opens ClipForge in its own Windows app window, without browser tabs or browser controls.
3. Open **AI settings**. The installed Qwen models are discovered from Ollama. Qwen3-VL 2B is the recommended visual analysis model; Qwen3 4B remains available for text-only fallback on this 8 GB machine.
4. Install the optional faster-whisper engine from this page before processing the first video.
5. Start a project with a local video or a video URL you have permission to use. Choose **Live stream clips** to monitor a current public YouTube stream or analyze an available ended-stream chat replay.

Python command line alternative: `python -m pip install -r requirements.txt`, then `python launch.py`.

## Processing pipeline

1. Import a local MP4, MKV, MOV, WebM, AVI, or M4V file, or download a supported URL with yt-dlp.
2. Probe and extract 16 kHz mono audio using FFmpeg.
3. Transcribe on CPU with faster-whisper using int8, VAD, word timestamps, and configurable thread count. The default model is Base.
4. Sample up to 72 low-resolution scene frames and estimate coarse motion between them.
5. Send timestamped transcript chunks plus up to four scene frames per time span to the selected local Ollama vision model (default) or a configured image-capable OpenAI-compatible API endpoint.
6. Both local and API analysis use a fun-first rubric: laughs, big reactions, surprises, playful banter, impressive wins or fails, and punchline payoffs score highest, then hook, context, energy, and shareability. Scores describe moments inside the video; they are not real platform popularity data.
7. Open a clip in the project editor and use the **Caption & text** button beside the preview to jump to its font, color, and position controls. Review movement and visible-reaction scores, change clip boundaries and titles, and edit each subtitle segment. Caption controls include font, size, position, fill and active-word colors, background box, emoji size, and one to eight words per timed caption. Add your own optional overlay text and preview captions against black while adjusting them.
8. Render H.264 MP4s with the default Viral Glow captions: a short white hook card, 2–4 words at a time, a bright red word highlight timed to the active Whisper word, and a contextual emoji on emphasized phrases. Other caption presets remain available. Export a matching SRT subtitle file.

## YouTube live chat clipping

Live capture uses yt-dlp to resolve the broadcast and FFmpeg to keep a short rolling buffer of stream segments. ClipForge deletes old segments, caps the temporary video buffer at 256 MB, and removes it when the session ends. When YouTube exposes the live chat replay timeline, active streams are scanned from the beginning and older spikes are fetched as selected time ranges. Ended streams use chat replay when available; otherwise ClipForge scans at most 120 short samples across the video, ranks motion/audio and visible reactions, then fetches only the selected clip windows. It never saves a full recording. Public live chat uses yt-dlp, so no separate YouTube API key is needed. Local Whisper creates timed captions and an SRT; text, active-word color, and animated emoji positions are customizable. Auto layout uses OpenCV to detect a webcam face and stacks the crop above gameplay; without a clear face it uses a vertical blurred-fill layout.

## Privacy and API keys

Local mode sends transcript text and sampled low-resolution frames to the chosen local Ollama model, keeping them on this PC. API mode sends transcript text and sampled frames to the selected provider for visual analysis. On Windows, saved API keys are protected with the current Windows account using DPAPI; they are not returned to the UI or written to application logs. Other platforms save the key as configured and should use local mode for private processing.

## Storage and dependencies

Project metadata, source video copies, transcripts, Whisper model files and exports live under the configured ClipForge storage directory. Use the Storage **Change** button to move the local data store. FFmpeg and yt-dlp are detected from PATH. The local faster-whisper engine installs from AI Settings; its speech model downloads on first use. OpenCV is installed with the application dependencies for webcam detection. The Base speech model is compact compared with a video model.

## Updates from GitHub

Set a public `owner/repository` under **AI settings → App updates from GitHub**, then choose **Check for updates**. ClipForge compares the published default-branch commit and only downloads code when the commit changes. **Install update** replaces a whitelist of app source and static UI files, then asks you to close and reopen ClipForge. The API key, config, projects, downloads, and exports are kept in the separate ClipForge storage folder and are excluded from the GitHub source archive and updater. The API key is not sent to GitHub.

## Current boundaries

- Visual understanding uses up to 72 evenly sampled low-resolution still frames and a coarse adjacent-frame difference score. Emotion tags (such as surprise, smiles, and laughter) are estimates from visible sampled frames; they can be unknown when faces are small or unclear. It is not continuous person or face tracking, and movement scores do not guarantee a clip is entertaining.
- Auto reframe in standard video projects uses a centered crop. Live stream layout uses a simple local face detector to estimate a webcam crop; it is not identity tracking and can miss unusual overlays. Starting from a live stream's beginning depends on YouTube exposing its DVR/chat replay history; without that history, only moments after monitoring starts can be detected.
- Supported API providers use OpenAI-compatible chat-completions endpoints. Gemini is available via its compatible endpoint and model identifier.
- Re-analyze reuses the saved transcript and sampled scene frames without re-importing or transcribing the source.
- Local AI is the default mode. OpenRouter and the configured DeepSeek model remain available as an optional analysis mode; both modes share the same fun-first selection and Viral Glow rendering pipeline.
- The first import is copied into the project folder so source files remain separate from clip exports.

No sample video, model, or bundled media assets are included.
