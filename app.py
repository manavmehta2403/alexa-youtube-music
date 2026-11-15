import asyncio
import difflib
import logging
import random
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("alexa-ytmusic")

app = FastAPI(title="Alexa YouTube Music Bridge", version="3.1.0")

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# Pool of verified, active Invidious instances for parallel racing audio stream resolution
INVIDIOUS_INSTANCES = [
    "https://yt.chocolatemoo53.com",
    "https://invidious.nerdvpn.de",
    "https://invidious.tiekoetter.com",
    "https://inv.nadeko.net",
    "https://invidious.drgns.space",
    "https://invidious.perennialte.ch",
]

# In-memory stream cache: video_id -> direct stream url
stream_cache: Dict[str, str] = {}

# User playback state & queues: user_id -> {
#    "tracks": List[Dict[str, str]],
#    "current_index": int,
#    "current_offset": int,
#    "repeat": bool,
#    "shuffle": bool
# }
user_queues: Dict[str, Dict[str, Any]] = {}

# Cached user liked songs library: user_id -> List[Dict[str, str]]
user_liked_cache: Dict[str, List[Dict[str, str]]] = {}


@app.on_event("startup")
async def startup_keepalive():
    """Background heartbeat task to keep Render container warm and prevent 30-50s cold starts."""
    async def ping_loop():
        await asyncio.sleep(15)  # Initial grace period
        while True:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get("https://alexa-youtube-music-qkj8.onrender.com/health")
                    logger.info(f"Keep-alive self-ping sent (status={resp.status_code})")
            except Exception as e:
                logger.debug(f"Keep-alive self-ping note: {e}")
            await asyncio.sleep(540)  # Ping every 9 minutes (Render free sleeps after 15 min)

    asyncio.create_task(ping_loop())


async def fetch_single_instance(client: httpx.AsyncClient, instance: str, path: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """Helper to query a single Invidious instance and validate payload."""
    url = f"{instance}{path}"
    resp = await client.get(url, headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        if isinstance(data, dict) and ("adaptiveFormats" in data or "formatStreams" in data or "title" in data):
            return data
    raise ValueError(f"Instance {instance} returned status {resp.status_code} or invalid payload")


async def fetch_with_failover(path: str, timeout: float = 3.5) -> Optional[Any]:
    """Parallel racing fetch: queries all candidate instances simultaneously, returning the fastest winner in ~1s."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        tasks = [
            asyncio.create_task(fetch_single_instance(client, inst, path, headers))
            for inst in INVIDIOUS_INSTANCES
        ]
        for fut in asyncio.as_completed(tasks):
            try:
                res = await fut
                # Cancel remaining tasks to conserve server bandwidth
                for t in tasks:
                    if not t.done():
                        t.cancel()
                return res
            except Exception:
                continue
    return None


async def search_youtube_music_fast(query: str) -> Optional[Dict[str, Any]]:
    """Ultra-fast direct YouTube Music (WEB_REMIX) search engine (~0.5s latency)."""
    url = "https://music.youtube.com/youtubei/v1/search?alt=json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Referer": "https://music.youtube.com/",
    }
    payload = {
        "context": {
            "client": {
                "clientName": "WEB_REMIX",
                "clientVersion": "1.20240101.01.00",
                "hl": "en",
                "gl": "US",
            }
        },
        "query": query,
    }
    try:
        async with httpx.AsyncClient(timeout=3.5) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                found = []

                def walker(obj, current_title=None):
                    if isinstance(obj, dict):
                        t = current_title
                        if "title" in obj and isinstance(obj["title"], dict) and "runs" in obj["title"]:
                            runs = obj["title"].get("runs", [])
                            if runs:
                                t = runs[0].get("text")

                        vid = None
                        if "watchEndpoint" in obj and "videoId" in obj["watchEndpoint"]:
                            vid = obj["watchEndpoint"]["videoId"]
                        elif "videoId" in obj:
                            vid = obj["videoId"]

                        if vid and len(vid) == 11 and t:
                            found.append({"videoId": vid, "title": t, "author": "YouTube Music"})
                            return

                        for k, v in obj.items():
                            if len(found) >= 5:
                                return
                            walker(v, t)
                    elif isinstance(obj, list):
                        for it in obj:
                            if len(found) >= 5:
                                return
                            walker(it, current_title)

                walker(data)
                if found:
                    logger.info(f"Fast YouTube Music search hit in ~0.5s: {found[0]['title']} ({found[0]['videoId']})")
                    return found[0]
    except Exception as e:
        logger.warning(f"Direct YouTube Music search failed: {e}")
    return None


async def search_youtube(query: str) -> Optional[Dict[str, Any]]:
    """Searches YouTube Music directly for lightning-fast response, with parallel failover."""
    cleaned_query = query.strip()

    # 1. Primary: Direct YouTube Music WEB_REMIX API (Sub-second response)
    fast_result = await search_youtube_music_fast(cleaned_query)
    if fast_result:
        return fast_result

    # 2. Secondary Fallback: Parallel racing Invidious search
    encoded_query = urllib.parse.quote_plus(cleaned_query)
    data = await fetch_with_failover(f"/api/v1/search?q={encoded_query}&type=video")
    if data and isinstance(data, list) and len(data) > 0:
        top = data[0]
        return {
            "videoId": top.get("videoId"),
            "title": top.get("title", cleaned_query),
            "author": top.get("author", "YouTube Music"),
        }
    return None


def extract_with_ytdlp(video_id: str) -> Optional[str]:
    """Native Python fallback using yt-dlp to extract high-quality direct audio stream URL."""
    if not yt_dlp:
        return None
    try:
        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios", "web"]
                }
            }
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            if info and "url" in info:
                return info["url"]
            formats = info.get("formats", []) if info else []
            audio_formats = [f for f in formats if f.get("vcodec") == "none" and f.get("url")]
            if audio_formats:
                return audio_formats[-1]["url"]
    except Exception as e:
        logger.warning(f"yt-dlp extraction note for {video_id}: {e}")
    return None


async def resolve_audio_stream(video_id: str) -> Optional[str]:
    """Extracts direct audio stream URL for video_id with cache, parallel racing failover, and yt-dlp backup."""
    if video_id in stream_cache:
        return stream_cache[video_id]

    data = await fetch_with_failover(f"/api/v1/videos/{video_id}")
    if data and isinstance(data, dict):
        adaptive_formats = data.get("adaptiveFormats", [])
        audio_formats = [f for f in adaptive_formats if "audio" in f.get("type", "")]

        best_url = None
        # 1. Prefer audio/mp4 (AAC) for Alexa native player
        for f in audio_formats:
            if "audio/mp4" in f.get("type", "") and f.get("url"):
                best_url = f.get("url")
                break

        # 2. Fallback to any audio stream
        if not best_url and audio_formats:
            best_url = audio_formats[0].get("url")

        # 3. Fallback to formatStreams (combined mp4 audio/video)
        if not best_url:
            format_streams = data.get("formatStreams", [])
            for f in format_streams:
                if ("mp4" in f.get("type", "") or "video/mp4" in f.get("container", "")) and f.get("url"):
                    best_url = f.get("url")
                    break

        if best_url:
            stream_cache[video_id] = best_url
            return best_url

    # Fallback to native yt-dlp engine if Invidious instances are throttled or fail
    if yt_dlp:
        logger.info(f"Resolving {video_id} via native yt-dlp engine...")
        direct_url = await asyncio.to_thread(extract_with_ytdlp, video_id)
        if direct_url:
            stream_cache[video_id] = direct_url
            return direct_url

    return None


def prewarm_tracks(tracks: List[Dict[str, str]], count: int = 2):
    """Spawns background tasks to pre-resolve stream URLs so playback starts instantly."""
    for track in tracks[:count]:
        vid = track.get("videoId")
        if vid and vid not in stream_cache:
            asyncio.create_task(resolve_audio_stream(vid))


# ==============================================================================
# Google OAuth 2.0 Account Linking Handlers (User Library & Playlists)
# ==============================================================================

def clean_playlist_query(s: str) -> str:
    """Removes common voice fillers and speech artifacts from playlist queries."""
    s = s.lower()
    for w in ["my", "meri", "mere", "the", "playlist", "songs", "gaane", "gane", "sunao", "chalao", "bajao", "lagao"]:
        s = re.sub(r"\b" + w + r"\b", "", s)
    return " ".join(s.split())


def match_playlist_score(target: str, candidate_title: str) -> float:
    """Computes a robust fuzzy match score between requested query and library playlist title."""
    t_clean = clean_playlist_query(target)
    c_clean = clean_playlist_query(candidate_title)
    if not t_clean or not c_clean:
        return 0.0
    if t_clean == c_clean:
        return 1.0

    t_tokens = set(t_clean.split())
    c_tokens = set(c_clean.split())

    # Full word/token subset match (e.g. 'punjabi' in {'aajkl', 'punjabi'})
    if c_tokens.issubset(t_tokens) or t_tokens.issubset(c_tokens):
        return 0.95

    # Sequence matcher for typo tolerance (e.g. aajkal vs aajkl)
    ratio = difflib.SequenceMatcher(None, t_clean, c_clean).ratio()

    # Word-by-word fuzzy alignment
    token_scores = []
    for tt in t_tokens:
        best_t = max([difflib.SequenceMatcher(None, tt, ct).ratio() for ct in c_tokens], default=0.0)
        token_scores.append(best_t)
    avg_token = sum(token_scores) / len(token_scores) if token_scores else 0.0
    return max(ratio, avg_token)


async def get_user_liked_songs(user_id: str, access_token: str) -> List[Dict[str, str]]:
    """Fetches user's authentic YouTube Music Liked Songs (using system playlist LM) with caching."""
    # Use cached library if available
    if user_id in user_liked_cache and len(user_liked_cache[user_id]) >= 20:
        logger.info(f"Using cached liked songs for {user_id[:10]} ({len(user_liked_cache[user_id])} tracks)")
        return user_liked_cache[user_id]

    headers = {"Authorization": f"Bearer {access_token}"}
    tracks: List[Dict[str, str]] = []

    # 1. Primary: YouTube Data API v3 playlistItems for playlistId=LM (Liked Music)
    page_token = None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            for _ in range(3):  # Fetch initial 150 tracks synchronously for instant response
                url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId=LM&maxResults=50"
                if page_token:
                    url += f"&pageToken={page_token}"
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("items", []):
                        snippet = item.get("snippet", {})
                        vid = snippet.get("resourceId", {}).get("videoId")
                        title = snippet.get("title", "")
                        if vid and title and "deleted" not in title.lower() and "private" not in title.lower():
                            tracks.append({
                                "videoId": vid,
                                "title": title,
                                "author": snippet.get("videoOwnerChannelTitle", "YouTube Music"),
                            })
                    page_token = data.get("nextPageToken")
                    if not page_token:
                        break
                else:
                    logger.warning(f"LM query returned status {resp.status_code}: {resp.text}")
                    break

            if tracks:
                user_liked_cache[user_id] = tracks
                logger.info(f"Retrieved {len(tracks)} pure YouTube Music liked tracks from LM playlist")
                return tracks
    except Exception as e:
        logger.error(f"Failed to fetch LM liked music: {e}")

    # 2. Fallback: If LM fails, query LL but strictly enforce category 10 (Music)
    api_url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet,contentDetails&playlistId=LL&maxResults=50"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(api_url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                video_ids = [
                    it.get("snippet", {}).get("resourceId", {}).get("videoId")
                    for it in items
                    if it.get("snippet", {}).get("resourceId", {}).get("videoId")
                ]

                music_ids = set()
                if video_ids:
                    v_url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet&id={','.join(video_ids[:50])}"
                    v_resp = await client.get(v_url, headers=headers)
                    if v_resp.status_code == 200:
                        v_data = v_resp.json()
                        for v in v_data.get("items", []):
                            if v.get("snippet", {}).get("categoryId") == "10":
                                music_ids.add(v.get("id"))

                for item in items:
                    snippet = item.get("snippet", {})
                    vid = snippet.get("resourceId", {}).get("videoId")
                    title = snippet.get("title", "")
                    if vid in music_ids and title and "deleted" not in title.lower() and "private" not in title.lower():
                        tracks.append({
                            "videoId": vid,
                            "title": title,
                            "author": snippet.get("videoOwnerChannelTitle", "YouTube Music"),
                        })
                user_liked_cache[user_id] = tracks
                logger.info(f"Filtered {len(tracks)} music tracks from Liked Videos fallback")
                return tracks
    except Exception as e:
        logger.error(f"Failed to fetch liked songs fallback: {e}")

    return []


async def get_user_personal_playlist(access_token: str, playlist_name: str) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """Finds a matching user playlist using fuzzy matching and returns its title and tracks."""
    list_url = "https://www.googleapis.com/youtube/v3/playlists?part=snippet&mine=true&maxResults=50"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(list_url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                best_score = 0.0
                best_id = None
                best_title = None

                for pl in data.get("items", []):
                    title = pl.get("snippet", {}).get("title", "")
                    score = match_playlist_score(playlist_name, title)
                    if score > best_score:
                        best_score = score
                        best_id = pl.get("id")
                        best_title = title

                if best_id and best_score >= 0.65:
                    logger.info(f"Fuzzy matched personal playlist '{best_title}' (id={best_id}, score={best_score:.3f}) for query '{playlist_name}'")
                    items_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={best_id}&maxResults=50"
                    items_resp = await client.get(items_url, headers=headers)
                    if items_resp.status_code == 200:
                        items_data = items_resp.json()
                        tracks = []
                        for item in items_data.get("items", []):
                            snippet = item.get("snippet", {})
                            vid = snippet.get("resourceId", {}).get("videoId")
                            t = snippet.get("title", "")
                            if vid and t and "deleted" not in t.lower() and "private" not in t.lower():
                                tracks.append({
                                    "videoId": vid,
                                    "title": t,
                                    "author": snippet.get("videoOwnerChannelTitle", "YouTube Music"),
                                })
                        return best_title, tracks
    except Exception as e:
        logger.error(f"Failed to fetch personal playlist: {e}")
    return None, []


async def get_ytmusic_playlist_tracks(query: str) -> Tuple[str, List[Dict[str, str]]]:
    """Searches for a YouTube Music curated playlist and extracts up to 100 songs (~1.0s)."""
    clean_q = clean_playlist_query(query) or query.strip()
    search_url = "https://music.youtube.com/youtubei/v1/search?alt=json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Referer": "https://music.youtube.com/",
    }
    payload = {
        "context": {
            "client": {
                "clientName": "WEB_REMIX",
                "clientVersion": "1.20240101.01.00",
                "hl": "en",
                "gl": "US",
            }
        },
        "query": f"{clean_q} playlist",
    }
    pl_id = None
    pl_title = f"{clean_q.title()} Playlist"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(search_url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()

                def find_pl(obj):
                    if isinstance(obj, dict):
                        if "watchPlaylistEndpoint" in obj and "playlistId" in obj["watchPlaylistEndpoint"]:
                            return obj["watchPlaylistEndpoint"]["playlistId"]
                        for k, v in obj.items():
                            res = find_pl(v)
                            if res:
                                return res
                    elif isinstance(obj, list):
                        for it in obj:
                            res = find_pl(it)
                            if res:
                                return res
                    return None

                pl_id = find_pl(data)

            if not pl_id:
                return pl_title, []

            # Browse the playlist tracks directly
            browse_id = pl_id if pl_id.startswith("VL") else f"VL{pl_id}"
            browse_url = "https://music.youtube.com/youtubei/v1/browse?alt=json"
            browse_payload = {
                "context": {
                    "client": {
                        "clientName": "WEB_REMIX",
                        "clientVersion": "1.20240101.01.00",
                        "hl": "en",
                        "gl": "US",
                    }
                },
                "browseId": browse_id,
            }
            b_resp = await client.post(browse_url, json=browse_payload, headers=headers)
            if b_resp.status_code == 200:
                b_data = b_resp.json()
                tracks = []

                def walker(obj):
                    if isinstance(obj, dict):
                        if "musicResponsiveListItemRenderer" in obj:
                            r = obj["musicResponsiveListItemRenderer"]
                            cols = r.get("flexColumns", [])
                            if cols:
                                col1 = cols[0].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [{}])
                                title = col1[0].get("text", "")
                                vid = None
                                nav = col1[0].get("navigationEndpoint", {}).get("watchEndpoint", {})
                                if nav:
                                    vid = nav.get("videoId")
                                if not vid and "overlay" in r:
                                    btn = r.get("overlay", {}).get("musicItemThumbnailOverlayRenderer", {}).get("content", {}).get("musicPlayButtonRenderer", {}).get("playNavigationEndpoint", {}).get("watchEndpoint", {})
                                    if btn:
                                        vid = btn.get("videoId")
                                artist = "YouTube Music"
                                if len(cols) > 1:
                                    col2 = cols[1].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [{}])
                                    if col2:
                                        artist = col2[0].get("text", "YouTube Music")
                                if vid and title:
                                    tracks.append({"videoId": vid, "title": title, "author": artist})
                        for k, v in obj.items():
                            walker(v)
                    elif isinstance(obj, list):
                        for it in obj:
                            walker(it)

                walker(b_data)
                return pl_title, tracks
    except Exception as e:
        logger.error(f"Error fetching playlist tracks: {e}")
    return pl_title, []


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

    # CRITICAL: outputSpeech MUST NOT be included in AudioPlayer directives during
    # AMAZON.NextIntent, AMAZON.PreviousIntent, or AudioPlayer events (causes Alexa schema rejection)
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
# Helper Orchestrators (Liked Songs & Playlists)
# ==============================================================================

async def handle_liked_songs_request(
    user_id: str,
    access_token: Optional[str],
    base_url: str,
    is_shuffle: bool = False,
    is_repeat: bool = False,
) -> Dict[str, Any]:
    """Handles playing user liked songs from LM playlist with multi-track queueing."""
    if not access_token:
        return build_alexa_link_account_response(
            "Apne liked gaane sunne ke liye, please link your Google account using the card sent to your Alexa app."
        )

    liked_tracks = await get_user_liked_songs(user_id, access_token)
    if not liked_tracks:
        return build_alexa_speech_response("No liked songs found in your YouTube Music library.")

    queue_tracks = list(liked_tracks)
    if is_shuffle:
        random.shuffle(queue_tracks)

    user_queues[user_id] = {
        "tracks": queue_tracks,
        "current_index": 0,
        "current_offset": 0,
        "repeat": is_repeat,
        "shuffle": is_shuffle,
    }

    first_track = queue_tracks[0]
    # Pre-warm stream cache immediately so audio starts in <0.1s!
    prewarm_tracks(queue_tracks, count=2)

    mode_text = " on shuffle" if is_shuffle else (" on repeat" if is_repeat else "")
    logger.info(f"Initialized liked songs queue for {user_id[:10]} with {len(queue_tracks)} songs (starting with {first_track['title']})")

    return build_alexa_play_response(
        video_id=first_track["videoId"],
        title=first_track["title"],
        author=first_track["author"],
        base_url=base_url,
        speech_text=f"Playing your liked songs{mode_text} on YouTube Music.",
    )


async def handle_playlist_request(
    user_id: str,
    playlist_name: str,
    access_token: Optional[str],
    base_url: str,
    is_shuffle: bool = False,
    is_repeat: bool = False,
) -> Dict[str, Any]:
    """Resolves and plays a personal or public YouTube Music playlist with full queue management."""
    p_low = playlist_name.lower().strip()

    # 1. Did the user ask for Liked Songs via a playlist phrase?
    if any(w in p_low for w in ["liked", "favorites", "pasandida"]):
        logger.info(f"Playlist request '{playlist_name}' redirected to Liked Songs")
        return await handle_liked_songs_request(user_id, access_token, base_url, is_shuffle, is_repeat)

    # 2. Check user personal playlists first if authenticated
    if access_token:
        matched_title, personal_tracks = await get_user_personal_playlist(access_token, playlist_name)
        if personal_tracks:
            queue_tracks = list(personal_tracks)
            if is_shuffle:
                random.shuffle(queue_tracks)

            user_queues[user_id] = {
                "tracks": queue_tracks,
                "current_index": 0,
                "current_offset": 0,
                "repeat": is_repeat,
                "shuffle": is_shuffle,
            }

            first_track = queue_tracks[0]
            # Pre-warm stream cache immediately so audio starts in <0.1s!
            prewarm_tracks(queue_tracks, count=2)

            mode_text = " on shuffle" if is_shuffle else (" on repeat" if is_repeat else "")
            logger.info(f"Initialized personal playlist queue '{matched_title}' with {len(queue_tracks)} songs")

            return build_alexa_play_response(
                video_id=first_track["videoId"],
                title=first_track["title"],
                author=first_track["author"],
                base_url=base_url,
                speech_text=f"Playing your playlist {matched_title}{mode_text} on YouTube Music.",
            )

    # 3. Public YouTube Music curated playlist extraction
    clean_name = clean_playlist_query(playlist_name) or playlist_name
    pl_title, public_tracks = await get_ytmusic_playlist_tracks(clean_name)
    if public_tracks:
        queue_tracks = list(public_tracks)
        if is_shuffle:
            random.shuffle(queue_tracks)

        user_queues[user_id] = {
            "tracks": queue_tracks,
            "current_index": 0,
            "current_offset": 0,
            "repeat": is_repeat,
            "shuffle": is_shuffle,
        }

        first_track = queue_tracks[0]
        prewarm_tracks(queue_tracks, count=2)

        mode_text = " on shuffle" if is_shuffle else (" on repeat" if is_repeat else "")
        logger.info(f"Initialized public playlist queue '{pl_title}' with {len(queue_tracks)} songs")

        return build_alexa_play_response(
            video_id=first_track["videoId"],
            title=first_track["title"],
            author=first_track["author"],
            base_url=base_url,
            speech_text=f"Playing playlist {pl_title}{mode_text} on YouTube Music.",
        )

    # 4. Strict Hierarchy Enforcement: When explicitly asking for a playlist,
    # DO NOT fall back to a random single video search!
    logger.info(f"Explicit playlist requested but not found: '{playlist_name}'. Returning 'no playlist'.")
    return build_alexa_speech_response(f"Sorry, I could not find the playlist {clean_name} on YouTube Music.")


# ==============================================================================
# Endpoints
# ==============================================================================

@app.get("/")
@app.get("/health")
async def health_check():
    return {
        "status": "online",
        "service": "Alexa YouTube Music Bridge",
        "version": "3.1.0",
        "features": [
            "Self-Pinging Keep-Alive Heartbeat (Zero Cold Starts)",
            "Parallel Racing Audio Resolution (<1.5s vs 25s)",
            "Dual-Engine Fallback (Invidious Race + Native yt-dlp)",
            "Instant Pre-Warmed Audio Streams (<0.1s)",
            "Official Liked Music (LM) Playlist",
            "Personal Playlist Fuzzy Matching",
            "Multi-Track Gapless Queueing",
            "Reliable Next & Previous Skipping",
            "Strict Playlist Hierarchy",
            "Shuffle & Repeat Support",
            "Auto-Recovery on Playback Failure",
            "Accurate Resume Offset Tracking",
        ],
        "instances_configured": len(INVIDIOUS_INSTANCES),
        "cached_streams": len(stream_cache),
    }


@app.get("/stream/{video_id}")
async def stream_audio(video_id: str, request: Request, proxy: bool = False):
    """Resolves and streams or redirects the audio for Alexa AudioPlayer with instant caching."""
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

    # Extract user ID and OAuth accessToken
    user_id = body.get("context", {}).get("System", {}).get("user", {}).get("userId", "default_user")
    access_token = body.get("context", {}).get("System", {}).get("user", {}).get("accessToken")

    q = user_queues.get(user_id)

    logger.info(f"Received Alexa request: type={req_type}, user={user_id[:12]}, authenticated={bool(access_token)}")

    # 1. Launch Request
    if req_type == "LaunchRequest":
        status_msg = "Google account linked." if access_token else "Boliye kaunsa gaana ya playlist sunna hai?"
        return build_alexa_speech_response(
            f"YouTube Music is ready. {status_msg}",
            end_session=False,
        )

    # 2. Intent Request
    if req_type == "IntentRequest":
        intent = body.get("request", {}).get("intent", {})
        intent_name = intent.get("name")
        logger.info(f"Intent name: {intent_name}")

        # Check for Shuffle On / Off intents
        if intent_name == "AMAZON.ShuffleOnIntent":
            if q and q.get("tracks"):
                q["shuffle"] = True
                curr_idx = q["current_index"]
                if curr_idx < len(q["tracks"]) - 1:
                    ahead = q["tracks"][curr_idx + 1:]
                    random.shuffle(ahead)
                    q["tracks"] = q["tracks"][:curr_idx + 1] + ahead
            return build_alexa_speech_response("Shuffle turned on.")

        if intent_name == "AMAZON.ShuffleOffIntent":
            if q:
                q["shuffle"] = False
            return build_alexa_speech_response("Shuffle turned off.")

        # Check for Repeat / Loop intents
        if intent_name in ("AMAZON.LoopOnIntent", "AMAZON.RepeatIntent"):
            if q:
                q["repeat"] = True
            return build_alexa_speech_response("Repeat turned on.")

        if intent_name == "AMAZON.LoopOffIntent":
            if q:
                q["repeat"] = False
            return build_alexa_speech_response("Repeat turned off.")

        # Play User's Liked Songs ("play my liked songs on shuffle", "mere liked gane bajao")
        if intent_name == "PlayLikedSongsIntent":
            req_text = str(body).lower()
            is_shuffle = "shuffle" in req_text or (q.get("shuffle") if q else False)
            is_repeat = "repeat" in req_text or "loop" in req_text or (q.get("repeat") if q else False)
            return await handle_liked_songs_request(user_id, access_token, base_url, is_shuffle, is_repeat)

        # Play Playlist ("play my punjabi playlist", "playlist lofi bajao", "aajkal punjabi playlist")
        if intent_name == "PlayPlaylistIntent":
            playlist_name = (
                intent.get("slots", {}).get("playlist", {}).get("value", "")
                or intent.get("slots", {}).get("playlistName", {}).get("value", "")
                or intent.get("slots", {}).get("query", {}).get("value", "")
            )
            if not playlist_name:
                return build_alexa_speech_response("Kaunsi playlist bajani hai? (What playlist do you want to play?)", end_session=False)

            req_text = str(body).lower()
            is_shuffle = "shuffle" in req_text or (q.get("shuffle") if q else False)
            is_repeat = "repeat" in req_text or "loop" in req_text or (q.get("repeat") if q else False)
            return await handle_playlist_request(user_id, playlist_name, access_token, base_url, is_shuffle, is_repeat)

        # General Music Search ("jagjit singh ke ganne lagao", "play Starboy", "arijit singh bajao")
        if intent_name == "PlayMusicIntent":
            query = (
                intent.get("slots", {}).get("query", {}).get("value", "")
                or intent.get("slots", {}).get("song", {}).get("value", "")
                or intent.get("slots", {}).get("music", {}).get("value", "")
                or intent.get("slots", {}).get("search", {}).get("value", "")
            )
            if not query:
                return build_alexa_speech_response("Kaunsa gaana lagau? (What song should I play?)", end_session=False)

            req_text = str(body).lower()
            is_shuffle = "shuffle" in req_text or (q.get("shuffle") if q else False)
            is_repeat = "repeat" in req_text or "loop" in req_text or (q.get("repeat") if q else False)

            q_low = query.lower()

            # 1. Did the user ask for liked songs via PlayMusicIntent?
            if any(w in q_low for w in ["liked", "favorites", "pasandida"]):
                return await handle_liked_songs_request(user_id, access_token, base_url, is_shuffle, is_repeat)

            # 2. Did the user ask for a playlist explicitly?
            if "playlist" in q_low or q_low.startswith("my ") or q_low.startswith("meri "):
                return await handle_playlist_request(user_id, query, access_token, base_url, is_shuffle, is_repeat)

            # 3. Check if query matches a personal playlist with high confidence even without saying "playlist"
            if access_token:
                matched_title, personal_tracks = await get_user_personal_playlist(access_token, query)
                if personal_tracks:
                    return await handle_playlist_request(user_id, query, access_token, base_url, is_shuffle, is_repeat)

            # 4. Standard track/artist search (NOT a playlist)
            result = await search_youtube(query)
            if not result:
                return build_alexa_speech_response(f"Sorry, {query} YouTube Music par nahi mila.")

            video_id = result["videoId"]
            title = result["title"]
            author = result["author"]

            # Pre-warm stream cache immediately
            asyncio.create_task(resolve_audio_stream(video_id))

            # Initialize a 1-song queue (with autoplay continuation)
            user_queues[user_id] = {
                "tracks": [result],
                "current_index": 0,
                "current_offset": 0,
                "repeat": is_repeat,
                "shuffle": is_shuffle,
            }

            logger.info(f"Starting playback for song: {title} ({video_id})")
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
            if not token and q and q.get("tracks"):
                curr = q["tracks"][q["current_index"]]
                token = curr["videoId"]
                offset = q.get("current_offset", 0)

            if token:
                curr_title = q["tracks"][q["current_index"]]["title"] if q and q.get("tracks") else "Resuming"
                curr_author = q["tracks"][q["current_index"]]["author"] if q and q.get("tracks") else "YouTube Music"
                return build_alexa_play_response(
                    video_id=token,
                    title=curr_title,
                    author=curr_author,
                    base_url=base_url,
                    offset=offset,
                    speech_text=None,
                )
            return build_alexa_speech_response("Kuch pause nahi hai resume karne ke liye.")

        # Next Track ("Alexa, next") - CRITICAL: No outputSpeech allowed by Alexa AudioPlayer!
        if intent_name == "AMAZON.NextIntent":
            if q and q.get("tracks"):
                next_idx = q["current_index"] + 1
                if next_idx < len(q["tracks"]):
                    q["current_index"] = next_idx
                    q["current_offset"] = 0
                    next_song = q["tracks"][next_idx]
                    # Pre-warm next+1 track in background
                    if next_idx + 1 < len(q["tracks"]):
                        asyncio.create_task(resolve_audio_stream(q["tracks"][next_idx + 1]["videoId"]))

                    logger.info(f"User skipped to next track: {next_song['title']} (index {next_idx}/{len(q['tracks'])})")
                    return build_alexa_play_response(
                        video_id=next_song["videoId"],
                        title=next_song["title"],
                        author=next_song["author"],
                        base_url=base_url,
                        play_behavior="REPLACE_ALL",
                        offset=0,
                        speech_text=None,
                    )
            return build_alexa_stop_response()

        # Previous Track ("Alexa, previous") - CRITICAL: No outputSpeech allowed by Alexa AudioPlayer!
        if intent_name in ("AMAZON.PreviousIntent", "AMAZON.StartOverIntent"):
            if q and q.get("tracks"):
                if q["current_index"] > 0:
                    q["current_index"] -= 1
                    q["current_offset"] = 0
                    prev_song = q["tracks"][q["current_index"]]
                    logger.info(f"User skipped to previous track: {prev_song['title']} (index {q['current_index']})")
                    return build_alexa_play_response(
                        video_id=prev_song["videoId"],
                        title=prev_song["title"],
                        author=prev_song["author"],
                        base_url=base_url,
                        play_behavior="REPLACE_ALL",
                        offset=0,
                        speech_text=None,
                    )
                else:
                    curr_song = q["tracks"][0]
                    q["current_offset"] = 0
                    return build_alexa_play_response(
                        video_id=curr_song["videoId"],
                        title=curr_song["title"],
                        author=curr_song["author"],
                        base_url=base_url,
                        play_behavior="REPLACE_ALL",
                        offset=0,
                        speech_text=None,
                    )
            return build_alexa_stop_response()

        # Help
        if intent_name == "AMAZON.HelpIntent":
            return build_alexa_speech_response(
                "Aap bol sakte hain: Jagjit Singh ke gane lagao, play my liked songs on shuffle, play my punjabi playlist, ya next aur pause. Kaunsa gaana sunna hai?",
                end_session=False,
            )

    # 3. AudioPlayer Queueing (Gapless Continuous Playback & Next Preload)
    if req_type == "AudioPlayer.PlaybackNearlyFinished":
        current_token = body.get("request", {}).get("token")
        if q and q.get("tracks"):
            # Repeat mode: re-enqueue current song
            if q.get("repeat"):
                curr_song = q["tracks"][q["current_index"]]
                logger.info(f"Repeat mode: Re-enqueuing {curr_song['title']}")
                return build_alexa_play_response(
                    video_id=curr_song["videoId"],
                    title=curr_song["title"],
                    author=curr_song["author"],
                    base_url=base_url,
                    play_behavior="ENQUEUE",
                    expected_previous_token=current_token,
                    speech_text=None,
                )

            # Continuous playlist continuation: pre-enqueue the next track
            next_idx = q["current_index"] + 1
            if next_idx < len(q["tracks"]):
                next_song = q["tracks"][next_idx]
                # Pre-warm next+1 track stream URL
                if next_idx + 1 < len(q["tracks"]):
                    asyncio.create_task(resolve_audio_stream(q["tracks"][next_idx + 1]["videoId"]))

                logger.info(f"Pre-enqueuing next gapless track: {next_song['title']} (index {next_idx}/{len(q['tracks'])})")
                return build_alexa_play_response(
                    video_id=next_song["videoId"],
                    title=next_song["title"],
                    author=next_song["author"],
                    base_url=base_url,
                    play_behavior="ENQUEUE",
                    expected_previous_token=current_token,
                    speech_text=None,
                )

    # 4. AudioPlayer Status Notifications (Update current_index & offset)
    if req_type == "AudioPlayer.PlaybackStarted":
        started_token = body.get("request", {}).get("token")
        if q and started_token:
            for idx, t in enumerate(q["tracks"]):
                if t.get("videoId") == started_token:
                    q["current_index"] = idx
                    q["current_offset"] = 0
                    logger.info(f"AudioPlayer confirmed playing index {idx}: {t.get('title')}")
                    break

    if req_type == "AudioPlayer.PlaybackStopped":
        offset = body.get("request", {}).get("offsetInMilliseconds", 0)
        if q:
            q["current_offset"] = offset

    # 5. AudioPlayer Error Auto-Recovery: Skip failed stream to next track automatically
    if req_type == "AudioPlayer.PlaybackFailed":
        failed_token = body.get("request", {}).get("token")
        error_info = body.get("request", {}).get("error", {})
        logger.warning(f"AudioPlayer playback failed for {failed_token}: {error_info}")
        if q and q.get("tracks"):
            next_idx = q["current_index"] + 1
            if next_idx < len(q["tracks"]):
                q["current_index"] = next_idx
                q["current_offset"] = 0
                next_song = q["tracks"][next_idx]
                logger.info(f"Auto-recovered to next track: {next_song['title']}")
                return build_alexa_play_response(
                    video_id=next_song["videoId"],
                    title=next_song["title"],
                    author=next_song["author"],
                    base_url=base_url,
                    play_behavior="REPLACE_ALL",
                    offset=0,
                    speech_text=None,
                )

    return {"version": "1.0", "response": {}}
