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

app = FastAPI(title="Alexa YouTube Music Bridge", version="2.7.0")

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
# User playback modes: user_id -> {"shuffle": bool, "repeat": bool, "current_token": str}
user_playback_modes: Dict[str, Dict[str, Any]] = {}


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
    """Searches YouTube Music directly for lightning-fast response, with Invidious failover."""
    cleaned_query = query.strip()

    # 1. Primary: Direct YouTube Music WEB_REMIX API (Sub-second response)
    fast_result = await search_youtube_music_fast(cleaned_query)
    if fast_result:
        return fast_result

    # 2. Secondary Fallback: Decentralized Invidious search
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


async def get_user_liked_songs(access_token: str) -> List[Dict[str, str]]:
    """Fetches user's authentic YouTube Music Liked Songs (using system playlist LM)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    tracks: List[Dict[str, str]] = []

    # 1. Primary: YouTube Data API v3 playlistItems for playlistId=LM (Liked Music)
    # LM is the dedicated playlist ID for YouTube Music likes (excludes regular YouTube videos)
    page_token = None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            for _ in range(2):  # Fetch up to 100 liked music tracks
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
    access_token: Optional[str],
    user_state: Dict[str, Any],
    base_url: str,
    is_shuffle: bool = False,
    is_repeat: bool = False,
) -> Dict[str, Any]:
    """Handles playing user liked songs from LM playlist with shuffle and repeat support."""
    if not access_token:
        return build_alexa_link_account_response(
            "Apne liked gaane sunne ke liye, please link your Google account using the card sent to your Alexa app."
        )

    liked_tracks = await get_user_liked_songs(access_token)
    if not liked_tracks:
        return build_alexa_speech_response("No liked songs found in your YouTube Music library.")

    if is_repeat:
        user_state["repeat"] = True

    if is_shuffle:
        user_state["shuffle"] = True
        random.shuffle(liked_tracks)

    first_track = liked_tracks[0]
    user_state["current_token"] = first_track["videoId"]
    related_cache[first_track["videoId"]] = liked_tracks[1:]

    mode_text = " on shuffle" if is_shuffle else (" on repeat" if is_repeat else "")
    return build_alexa_play_response(
        video_id=first_track["videoId"],
        title=first_track["title"],
        author=first_track["author"],
        base_url=base_url,
        speech_text=f"Playing your liked songs{mode_text} on YouTube Music.",
    )


async def handle_playlist_request(
    playlist_name: str,
    access_token: Optional[str],
    user_state: Dict[str, Any],
    base_url: str,
    is_shuffle: bool = False,
    is_repeat: bool = False,
) -> Dict[str, Any]:
    """Resolves and plays a personal or public YouTube Music playlist, or strictly reports not found."""
    if is_repeat:
        user_state["repeat"] = True

    p_low = playlist_name.lower().strip()

    # 1. Did the user ask for Liked Songs via a playlist phrase?
    # e.g. "play my liked songs playlist", "play liked playlist", "mere liked gane bajao"
    if any(w in p_low for w in ["liked", "favorites", "pasandida"]):
        logger.info(f"Playlist request '{playlist_name}' redirected to Liked Songs")
        return await handle_liked_songs_request(access_token, user_state, base_url, is_shuffle, is_repeat)

    # 2. Check user personal playlists first if authenticated
    if access_token:
        matched_title, personal_tracks = await get_user_personal_playlist(access_token, playlist_name)
        if personal_tracks:
            if is_shuffle:
                user_state["shuffle"] = True
                random.shuffle(personal_tracks)

            first_track = personal_tracks[0]
            user_state["current_token"] = first_track["videoId"]
            related_cache[first_track["videoId"]] = personal_tracks[1:]

            mode_text = " on shuffle" if is_shuffle else (" on repeat" if is_repeat else "")
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
        if is_shuffle:
            user_state["shuffle"] = True
            random.shuffle(public_tracks)

        first_track = public_tracks[0]
        user_state["current_token"] = first_track["videoId"]
        related_cache[first_track["videoId"]] = public_tracks[1:]

        mode_text = " on shuffle" if is_shuffle else (" on repeat" if is_repeat else "")
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
        "version": "2.7.0",
        "features": [
            "Official Liked Music (LM) Playlist",
            "Personal Playlist Fuzzy Matching",
            "Curated Public Playlist Browser",
            "Strict Playlist vs Search Hierarchy",
            "Multi-Track Background Queueing",
            "Shuffle & Repeat Support",
        ],
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

    # Extract user ID and OAuth accessToken
    user_id = body.get("context", {}).get("System", {}).get("user", {}).get("userId", "default_user")
    access_token = body.get("context", {}).get("System", {}).get("user", {}).get("accessToken")

    # Initialize user state if needed
    if user_id not in user_playback_modes:
        user_playback_modes[user_id] = {"shuffle": False, "repeat": False, "current_token": None}

    user_state = user_playback_modes[user_id]

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
            user_state["shuffle"] = True
            current_token = body.get("context", {}).get("AudioPlayer", {}).get("token")
            if current_token and current_token in related_cache:
                random.shuffle(related_cache[current_token])
            return build_alexa_speech_response("Shuffle turned on.")

        if intent_name == "AMAZON.ShuffleOffIntent":
            user_state["shuffle"] = False
            return build_alexa_speech_response("Shuffle turned off.")

        # Check for Repeat / Loop intents
        if intent_name in ("AMAZON.LoopOnIntent", "AMAZON.RepeatIntent"):
            user_state["repeat"] = True
            return build_alexa_speech_response("Repeat turned on.")

        if intent_name == "AMAZON.LoopOffIntent":
            user_state["repeat"] = False
            return build_alexa_speech_response("Repeat turned off.")

        # Play User's Liked Songs ("play my liked songs on shuffle", "mere liked gane bajao")
        if intent_name == "PlayLikedSongsIntent":
            req_text = str(body).lower()
            is_shuffle = "shuffle" in req_text or user_state.get("shuffle", False)
            is_repeat = "repeat" in req_text or "loop" in req_text or user_state.get("repeat", False)
            return await handle_liked_songs_request(access_token, user_state, base_url, is_shuffle, is_repeat)

        # Play Playlist ("play my punjabi playlist", "playlist lofi bajao", "aajkal punjabi playlist")
        if intent_name == "PlayPlaylistIntent":
            playlist_name = intent.get("slots", {}).get("playlist", {}).get("value", "")
            if not playlist_name:
                return build_alexa_speech_response("Kaunsi playlist bajani hai? (What playlist do you want to play?)", end_session=False)

            req_text = str(body).lower()
            is_shuffle = "shuffle" in req_text or user_state.get("shuffle", False)
            is_repeat = "repeat" in req_text or "loop" in req_text or user_state.get("repeat", False)
            return await handle_playlist_request(playlist_name, access_token, user_state, base_url, is_shuffle, is_repeat)

        # General Music Search ("jagjit singh ke ganne lagao", "play Starboy", "arijit singh bajao")
        if intent_name == "PlayMusicIntent":
            query = intent.get("slots", {}).get("query", {}).get("value", "")
            if not query:
                return build_alexa_speech_response("Kaunsa gaana lagau? (What song should I play?)", end_session=False)

            req_text = str(body).lower()
            is_shuffle = "shuffle" in req_text or user_state.get("shuffle", False)
            is_repeat = "repeat" in req_text or "loop" in req_text or user_state.get("repeat", False)

            q_low = query.lower()

            # 1. Did the user ask for liked songs via PlayMusicIntent?
            if any(w in q_low for w in ["liked", "favorites", "pasandida"]):
                return await handle_liked_songs_request(access_token, user_state, base_url, is_shuffle, is_repeat)

            # 2. Did the user ask for a playlist explicitly?
            if "playlist" in q_low or q_low.startswith("my ") or q_low.startswith("meri "):
                return await handle_playlist_request(query, access_token, user_state, base_url, is_shuffle, is_repeat)

            # 3. Check if query matches a personal playlist with high confidence even without saying "playlist"
            if access_token:
                matched_title, personal_tracks = await get_user_personal_playlist(access_token, query)
                if personal_tracks:
                    return await handle_playlist_request(query, access_token, user_state, base_url, is_shuffle, is_repeat)

            # 4. Standard track/artist search (NOT a playlist)
            if is_repeat:
                user_state["repeat"] = True

            result = await search_youtube(query)
            if not result:
                return build_alexa_speech_response(f"Sorry, {query} YouTube Music par nahi mila.")

            video_id = result["videoId"]
            title = result["title"]
            author = result["author"]
            user_state["current_token"] = video_id

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
            if token:
                return build_alexa_play_response(
                    video_id=token,
                    title="Resuming",
                    author="YouTube Music",
                    base_url=base_url,
                    offset=offset,
                )
            return build_alexa_speech_response("Kuch pause nahi hai resume karne ke liye.")

        # Next Track (Autoplay skip)
        if intent_name == "AMAZON.NextIntent":
            current_token = body.get("context", {}).get("AudioPlayer", {}).get("token")
            related = related_cache.get(current_token, [])
            if related:
                if user_state["shuffle"]:
                    next_song = related.pop(random.randint(0, len(related) - 1))
                else:
                    next_song = related.pop(0)

                related_cache[next_song["videoId"]] = related
                user_state["current_token"] = next_song["videoId"]

                return build_alexa_play_response(
                    video_id=next_song["videoId"],
                    title=next_song["title"],
                    author=next_song["author"],
                    base_url=base_url,
                    speech_text=f"Next track, {next_song['title']}.",
                )
            return build_alexa_speech_response("No more tracks in the current queue.")

        # Previous / Start Over
        if intent_name in ("AMAZON.PreviousIntent", "AMAZON.StartOverIntent"):
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
                "Aap bol sakte hain: Jagjit Singh ke gane lagao, play my liked songs on shuffle, play my punjabi playlist, ya next aur pause. Kaunsa gaana sunna hai?",
                end_session=False,
            )

    # 3. AudioPlayer Queueing (Gapless Continuous Playback & Repeat support)
    if req_type == "AudioPlayer.PlaybackNearlyFinished":
        current_token = body.get("request", {}).get("token")

        # If Repeat is active, re-enqueue the same track!
        if user_state.get("repeat") and current_token:
            logger.info(f"Repeat is active: Re-enqueuing current song ({current_token})")
            return build_alexa_play_response(
                video_id=current_token,
                title="Repeating Track",
                author="YouTube Music",
                base_url=base_url,
                play_behavior="ENQUEUE",
                expected_previous_token=current_token,
            )

        # Autoplay / Queue continuation
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
