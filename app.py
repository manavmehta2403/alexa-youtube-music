import logging
import urllib.parse
from typing import Any, Dict, List, Optional
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("alexa-ytmusic")

app = FastAPI(title="Alexa YouTube Music Bridge", version="2.0.0")

# Pool of reliable Invidious instances for public audio stream extraction
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
# In-memory queue cache for next/previous navigation: token -> list of next songs
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


# ==============================================================================
# Google OAuth 2.0 Account Linking Handlers (User Library & Playlists)
# ==============================================================================

async def get_user_liked_songs(access_token: str) -> List[Dict[str, str]]:
    """Fetches user's Liked Music (playlistId=LL) using their Google OAuth token."""
    url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId=LL&maxResults=50"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                tracks = []
                for item in items:
                    snippet = item.get("snippet", {})
                    vid = snippet.get("resourceId", {}).get("videoId")
                    if vid:
                        tracks.append({
                            "videoId": vid,
                            "title": snippet.get("title", "Liked Song"),
                            "author": snippet.get("videoOwnerChannelTitle", "YouTube Music"),
                        })
                return tracks
            else:
                logger.error(f"Google API returned error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to fetch liked songs: {e}")
    return []


async def get_user_personal_playlist(access_token: str, playlist_name: str) -> List[Dict[str, str]]:
    """Finds a matching user playlist and returns its tracks."""
    list_url = "https://www.googleapis.com/youtube/v3/playlists?part=snippet&mine=true&maxResults=50"
    headers = {"Authorization": f"Bearer {access_token}"}
    target_name = playlist_name.lower().strip()

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(list_url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                matching_id = None
                for pl in data.get("items", []):
                    title = pl.get("snippet", {}).get("title", "").lower()
                    if target_name in title or title in target_name:
                        matching_id = pl.get("id")
                        break

                if matching_id:
                    items_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={matching_id}&maxResults=50"
                    items_resp = await client.get(items_url, headers=headers)
                    if items_resp.status_code == 200:
                        items_data = items_resp.json()
                        tracks = []
                        for item in items_data.get("items", []):
                            snippet = item.get("snippet", {})
                            vid = snippet.get("resourceId", {}).get("videoId")
                            if vid:
                                tracks.append({
                                    "videoId": vid,
                                    "title": snippet.get("title", "Track"),
                                    "author": snippet.get("videoOwnerChannelTitle", "YouTube Music"),
                                })
                        return tracks
    except Exception as e:
        logger.error(f"Failed to fetch personal playlist: {e}")
    return []


# ==============================================================================
# Alexa Skill Response Builders
# ==============================================================================

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


def build_alexa_link_account_response(text: str) -> Dict[str, Any]:
    """Returns speech prompting the user to link their Google account with a card in the Alexa App."""
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": text,
            },
            "card": {
                "type": "LinkAccount",
            },
            "shouldEndSession": True,
        },
    }


# ==============================================================================
# Endpoints
# ==============================================================================

@app.get("/")
@app.get("/health")
async def health_check():
    return {
        "status": "online",
        "service": "Alexa YouTube Music Bridge",
        "version": "2.0.0",
        "features": ["Public Search", "Google Account Linking (Multi-User)", "AudioPlayer Autoplay"],
        "instances_configured": len(INVIDIOUS_INSTANCES),
    }


@app.get("/stream/{video_id}")
async def stream_audio(video_id: str, request: Request, proxy: bool = False):
    """Resolves and streams or redirects the audio for Alexa AudioPlayer."""
    direct_url = await resolve_audio_stream(video_id)
    if not direct_url:
        return Response(status_code=404, content="Audio stream could not be resolved.")

    if not proxy:
        return RedirectResponse(url=direct_url, status_code=302)

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

    # Extract OAuth accessToken if the user linked their Google account
    access_token = body.get("context", {}).get("System", {}).get("user", {}).get("accessToken")

    logger.info(f"Received Alexa request: type={req_type}, user_authenticated={bool(access_token)}")

    # 1. Launch Request
    if req_type == "LaunchRequest":
        status_msg = "Your Google account is linked." if access_token else "You can ask for any song, or link your Google account in the Alexa app."
        return build_alexa_speech_response(
            f"YouTube Music is ready. {status_msg} What would you like to hear?",
            end_session=False,
        )

    # 2. Intent Request
    if req_type == "IntentRequest":
        intent = body.get("request", {}).get("intent", {})
        intent_name = intent.get("name")
        logger.info(f"Intent name: {intent_name}")

        # Play User's Liked Songs ("play my liked songs", "play my favorites")
        if intent_name == "PlayLikedSongsIntent":
            if not access_token:
                return build_alexa_link_account_response(
                    "To play your liked songs and library, please link your Google account using the card sent to your Alexa app."
                )

            liked_tracks = await get_user_liked_songs(access_token)
            if not liked_tracks:
                return build_alexa_speech_response("I couldn't find any liked songs in your YouTube Music library.")

            first_track = liked_tracks[0]
            # Cache remaining tracks for next button / queue
            related_cache[first_track["videoId"]] = liked_tracks[1:]

            return build_alexa_play_response(
                video_id=first_track["videoId"],
                title=first_track["title"],
                author=first_track["author"],
                base_url=base_url,
                speech_text="Playing your liked songs from YouTube Music.",
            )

        # Play Playlist ("play playlist X", "play my playlist X")
        if intent_name == "PlayPlaylistIntent":
            playlist_name = intent.get("slots", {}).get("playlist", {}).get("value")
            if not playlist_name:
                return build_alexa_speech_response("What playlist do you want to play?", end_session=False)

            # If user has linked Google account, check personal playlists first
            if access_token:
                personal_tracks = await get_user_personal_playlist(access_token, playlist_name)
                if personal_tracks:
                    first_track = personal_tracks[0]
                    related_cache[first_track["videoId"]] = personal_tracks[1:]
                    return build_alexa_play_response(
                        video_id=first_track["videoId"],
                        title=first_track["title"],
                        author=first_track["author"],
                        base_url=base_url,
                        speech_text=f"Playing your playlist {playlist_name}.",
                    )

            # Fallback to public search for the playlist / mix
            result = await search_youtube(f"{playlist_name} playlist")
            if not result:
                return build_alexa_speech_response(f"Sorry, I couldn't find a playlist for {playlist_name}.")

            return build_alexa_play_response(
                video_id=result["videoId"],
                title=result["title"],
                author=result["author"],
                base_url=base_url,
                speech_text=f"Playing {result['title']}.",
            )

        # General Music Search ("play Starboy", "play Coldplay")
        if intent_name == "PlayMusicIntent":
            query = intent.get("slots", {}).get("query", {}).get("value")
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
                # Keep remaining queue for subsequent skips
                related_cache[next_song["videoId"]] = related
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
                "You can say, play Starboy by The Weeknd, play my liked songs, play playlist Chill, or say next, previous, or pause. What would you like to hear?",
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

    # AudioPlayer status notifications
    return {"version": "1.0", "response": {}}
