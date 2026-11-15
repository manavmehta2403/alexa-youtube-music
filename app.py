import logging
import urllib.parse
from typing import Any, Dict, List, Optional
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("alexa-ytmusic")

app = FastAPI(title="Alexa YouTube Music Bridge", version="1.0.0")

# List of reliable Invidious instances to query for search & stream extraction
INVIDIOUS_INSTANCES = [
    "https://yt.chocolatemoo53.com",
    "https://inv.tux.pizza",
    "https://invidious.nerdvpn.de",
    "https://iv.melmac.space",
    "https://invidious.jing.rocks",
    "https://invidious.tiekoetter.com",
]

# In-memory stream cache: video_id -> direct stream url
stream_cache: Dict[str, str] = {}
# In-memory history for next/previous navigation: token -> next_token
related_cache: Dict[str, List[Dict[str, str]]] = {}


async def fetch_with_failover(path: str, timeout: float = 6.0) -> Optional[Any]:
    """Tries fetching from the instance pool with automatic failover."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for instance in INVIDIOUS_INSTANCES:
            try:
                url = f"{instance}{path}"
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as e:
                logger.warning(f"Instance {instance} failed for {path}: {e}")
                continue
    return None


async def search_youtube(query: str) -> Optional[Dict[str, Any]]:
    """Searches YouTube via Invidious instances."""
    encoded_query = urllib.parse.quote_plus(query)
    data = await fetch_with_failover(f"/api/v1/search?q={encoded_query}&type=video")
    if data and isinstance(data, list) and len(data) > 0:
        top = data[0]
        return {
            "videoId": top.get("videoId"),
            "title": top.get("title", query),
            "author": top.get("author", "YouTube Music"),
        }
    return None


async def resolve_audio_stream(video_id: str) -> Optional[str]:
    """Extracts direct audio stream URL for video_id."""
    if video_id in stream_cache:
        return stream_cache[video_id]

    data = await fetch_with_failover(f"/api/v1/videos/{video_id}")
    if not data or not isinstance(data, dict):
        return None

    # Cache recommended videos for autoplay queueing
    recommended = data.get("recommendedVideos", [])
    if recommended:
        related_cache[video_id] = [
            {"videoId": r.get("videoId"), "title": r.get("title", ""), "author": r.get("author", "")}
            for r in recommended
            if r.get("videoId")
        ]

    # Look for best audio stream (preferably audio/mp4 for Alexa native AAC support)
    adaptive_formats = data.get("adaptiveFormats", [])
    audio_formats = [f for f in adaptive_formats if "audio" in f.get("type", "")]

    best_url = None
    # 1. Prefer audio/mp4 (AAC)
    for f in audio_formats:
        if "audio/mp4" in f.get("type", ""):
            best_url = f.get("url")
            break

    # 2. Fallback to any audio stream
    if not best_url and audio_formats:
        best_url = audio_formats[0].get("url")

    # 3. Fallback to formatStreams (combined mp4 audio/video)
    if not best_url:
        format_streams = data.get("formatStreams", [])
        for f in format_streams:
            if "mp4" in f.get("type", "") or "video/mp4" in f.get("container", ""):
                best_url = f.get("url")
                break

    if best_url:
        stream_cache[video_id] = best_url
        return best_url

    return None


def build_alexa_play_response(
    video_id: str,
    title: str,
    author: str,
    base_url: str,
    play_behavior: str = "REPLACE_ALL",
    offset: int = 0,
    expected_previous_token: Optional[str] = None,
    speech_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Constructs an Alexa AudioPlayer.Play response directive."""
    stream_url = f"{base_url}/stream/{video_id}"

    stream_obj: Dict[str, Any] = {
        "token": video_id,
        "url": stream_url,
        "offsetInMilliseconds": offset,
    }
    if expected_previous_token:
        stream_obj["expectedPreviousToken"] = expected_previous_token

    directive: Dict[str, Any] = {
        "type": "AudioPlayer.Play",
        "playBehavior": play_behavior,
        "audioItem": {
            "stream": stream_obj,
            "metadata": {
                "title": title,
                "subtitle": author,
                "art": {
                    "sources": [
                        {"url": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"}
                    ]
                },
            },
        },
    }

    response: Dict[str, Any] = {
        "version": "1.0",
        "response": {
            "directives": [directive],
            "shouldEndSession": True,
        },
    }

    if speech_text:
        response["response"]["outputSpeech"] = {
            "type": "PlainText",
            "text": speech_text,
        }

    return response


def build_alexa_stop_response() -> Dict[str, Any]:
    """Constructs an Alexa AudioPlayer.Stop response directive."""
    return {
        "version": "1.0",
        "response": {
            "directives": [{"type": "AudioPlayer.Stop"}],
            "shouldEndSession": True,
        },
    }


def build_alexa_speech_response(text: str, end_session: bool = True) -> Dict[str, Any]:
    """Constructs a basic Alexa speech response."""
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": text,
            },
            "shouldEndSession": end_session,
        },
    }


@app.get("/")
@app.get("/health")
async def health_check():
    return {
        "status": "online",
        "service": "Alexa YouTube Music Bridge",
        "instances_configured": len(INVIDIOUS_INSTANCES),
    }


@app.get("/stream/{video_id}")
async def stream_audio(video_id: str, request: Request, proxy: bool = False):
    """Resolves and streams or redirects the audio for Alexa AudioPlayer."""
    direct_url = await resolve_audio_stream(video_id)
    if not direct_url:
        return Response(status_code=404, content="Audio stream could not be resolved.")

    if not proxy:
        # Alexa natively supports HTTP 302 redirects to HTTPS streams
        return RedirectResponse(url=direct_url, status_code=302)

    # Proxy mode (forwarding stream chunks with range headers if requested)
    headers = {"User-Agent": "Mozilla/5.0"}
    client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    req = client.build_request("GET", direct_url, headers=headers)
    resp = await client.send(req, stream=True)

    async def iter_content():
        async for chunk in resp.aiter_bytes():
            yield chunk
        await client.aclose()

    return StreamingResponse(
        iter_content(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "audio/mp4"),
    )


@app.post("/")
@app.post("/alexa")
async def alexa_webhook(request: Request):
    """Main webhook receiver for Alexa Skill requests."""
    body = await request.json()
    req_type = body.get("request", {}).get("type")
    base_url = str(request.base_url).rstrip("/")

    logger.info(f"Received Alexa request type: {req_type}")

    # 1. Launch Request ("Alexa, open YouTube Music")
    if req_type == "LaunchRequest":
        return build_alexa_speech_response(
            "YouTube Music is ready. What song or artist would you like to hear?",
            end_session=False,
        )

    # 2. Intent Request ("Alexa, ask YouTube Music to play...")
    if req_type == "IntentRequest":
        intent = body.get("request", {}).get("intent", {})
        intent_name = intent.get("name")
        logger.info(f"Intent name: {intent_name}")

        # Play Music
        if intent_name in ("PlayMusicIntent", "PlayPlaylistIntent"):
            query = (
                intent.get("slots", {}).get("query", {}).get("value")
                or intent.get("slots", {}).get("playlist", {}).get("value")
            )
            if not query:
                return build_alexa_speech_response("What song do you want to play?", end_session=False)

            result = await search_youtube(query)
            if not result:
                return build_alexa_speech_response(f"Sorry, I couldn't find {query} on YouTube Music.")

            video_id = result["videoId"]
            title = result["title"]
            author = result["author"]

            logger.info(f"Starting playback for: {title} ({video_id})")
            return build_alexa_play_response(
                video_id=video_id,
                title=title,
                author=author,
                base_url=base_url,
                speech_text=f"Playing {title} on YouTube Music.",
            )

        # Pause / Stop
        if intent_name in ("AMAZON.PauseIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"):
            return build_alexa_stop_response()

        # Resume
        if intent_name == "AMAZON.ResumeIntent":
            audio_state = body.get("context", {}).get("AudioPlayer", {})
            token = audio_state.get("token")
            offset = audio_state.get("offsetInMilliseconds", 0)
            if token:
                return build_alexa_play_response(
                    video_id=token,
                    title="Resuming",
                    author="YouTube Music",
                    base_url=base_url,
                    offset=offset,
                )
            return build_alexa_speech_response("Nothing is currently paused to resume.")

        # Next Track (Autoplay skip)
        if intent_name == "AMAZON.NextIntent":
            current_token = body.get("context", {}).get("AudioPlayer", {}).get("token")
            related = related_cache.get(current_token, [])
            if related:
                next_song = related.pop(0)
                return build_alexa_play_response(
                    video_id=next_song["videoId"],
                    title=next_song["title"],
                    author=next_song["author"],
                    base_url=base_url,
                    speech_text=f"Next track, {next_song['title']}.",
                )
            return build_alexa_speech_response("No more tracks in the current queue.")

        # Previous / Start Over
        if intent_name in ("AMAZON.PreviousIntent", "AMAZON.StartOverIntent", "AMAZON.RepeatIntent"):
            current_token = body.get("context", {}).get("AudioPlayer", {}).get("token")
            if current_token:
                return build_alexa_play_response(
                    video_id=current_token,
                    title="Restarting",
                    author="YouTube Music",
                    base_url=base_url,
                    offset=0,
                )
            return build_alexa_speech_response("No track is currently playing.")

        # Help
        if intent_name == "AMAZON.HelpIntent":
            return build_alexa_speech_response(
                "You can say, play Starboy by The Weeknd, or say next, previous, or pause. What would you like to hear?",
                end_session=False,
            )

    # 3. AudioPlayer Queueing (Gapless Continuous Playback)
    if req_type == "AudioPlayer.PlaybackNearlyFinished":
        current_token = body.get("request", {}).get("token")
        related = related_cache.get(current_token, [])
        if related:
            next_song = related[0]
            logger.info(f"Enqueuing next song: {next_song['title']} ({next_song['videoId']})")
            return build_alexa_play_response(
                video_id=next_song["videoId"],
                title=next_song["title"],
                author=next_song["author"],
                base_url=base_url,
                play_behavior="ENQUEUE",
                expected_previous_token=current_token,
            )

    # AudioPlayer status notifications (PlaybackStarted, Finished, Stopped, Failed)
    return {"version": "1.0", "response": {}}
