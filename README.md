# Alexa YouTube Music Bridge (v2.9.0)

A high-performance, private, zero-cost Alexa skill bridge for Google YouTube Music. Enables full hands-free voice control on Amazon Echo devices in English, Hindi, and Hinglish.

---

## Features
- **Official YouTube Music Liked Songs**: Directly queries the `LM` system playlist for pure music tracks (no non-music YouTube video clutter).
- **Fuzzy Library Playlist Matching**: Typo-tolerant matching (`aajkal punjabi` &rarr; `aajkl Punjabi`) with sequence matching algorithms.
- **Strict Playlist vs Song Hierarchy**: Explicit playlist requests never fall back to random video searches. If a playlist is not found, the skill explicitly notifies you.
- **Continuous Gapless Autoplay**: Pre-enqueues subsequent tracks via `AudioPlayer.PlaybackNearlyFinished` with zero latency and zero gap.
- **Full Navigation Controls**: Instant `Next`, `Previous`, `Start Over`, `Pause`, `Resume`, `Shuffle`, and `Repeat`.
- **Zero Amazon Protocol Rejections**: Complies strictly with Amazon AudioPlayer specifications (strips `outputSpeech` on playback control directives to prevent silent drops).
- **Playback Failure Auto-Recovery**: Automatically recovers and skips to the next working song if any network or stream error occurs.
- **Lightning-Fast Responses**: Uses direct `WEB_REMIX` endpoints for sub-second (~0.5s) search queries.
- **Zero Cloud Costs**: Fully automated deployment on Render.com free tier with auto-deploy hooks.

---

## Voice Commands Reference

### 1. Liked Songs
- *"Alexa, ask YouTube Music to play my liked songs"*
- *"Alexa, ask YouTube Music to play my liked songs on shuffle"*
- *"Alexa, ask YouTube Music mere liked gane bajao"*

### 2. Personal Playlists
- *"Alexa, ask YouTube Music to play my aajkal punjabi playlist"*
- *"Alexa, ask YouTube Music to play my aajkal punjabi playlist on shuffle"*
- *"Alexa, ask YouTube Music to play Z playlist"*
- *"Alexa, ask YouTube Music to play Mix 22"*
- *"Alexa, ask YouTube Music meri punjabi playlist chalao"*

### 3. Public Curated Playlists
- *"Alexa, ask YouTube Music to play Lofi playlist"*
- *"Alexa, ask YouTube Music to play Punjabi workout playlist"*
- *"Alexa, ask YouTube Music to play Bollywood party playlist"*

### 4. Direct Song & Artist Search
- *"Alexa, ask YouTube Music to play Starboy"*
- *"Alexa, ask YouTube Music Jagjit Singh ke gane lagao"*
- *"Alexa, ask YouTube Music Arijit Singh bajao"*

### 5. Playback Navigation
- *"Alexa, next"* (Skips immediately to the next track)
- *"Alexa, previous"* (Returns to previous track)
- *"Alexa, restart"* or *"Alexa, start over"*
- *"Alexa, pause"* / *"Alexa, resume"*
- *"Alexa, shuffle"* / *"Alexa, repeat"*

---

## 1-Minute Alexa Routine (To use "play ... on YouTube Music")
Amazon reserves `"play [song] on [service]"` exclusively for Amazon-certified enterprise partners. To speak naturally without saying `"ask"`, create a quick Routine in the Alexa Mobile App:
1. Open the **Amazon Alexa App** on your phone.
2. Tap **More** &rarr; **Routines** &rarr; **+**.
3. Under **When you say**: `play my liked songs on YouTube Music` (or `play aajkal punjabi on YouTube Music`).
4. Under **Add action**: Select **Custom** &rarr; `ask YouTube Music to play my liked songs`.
5. Tap **Save**.

---

## Verification & Test Suite
An automated test suite is included in `tests/test_end_to_end.py`. To run:
```bash
python -m unittest tests/test_end_to_end.py
```
Covers all Amazon ASK lifecycle events: Launch, Intent routing, AudioPlayer pre-enqueuing, playback failure recovery, and fuzzy matching.
