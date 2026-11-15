import asyncio
import sys
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app, clean_playlist_query, match_playlist_score, user_queues
import httpx


class TestAlexaYouTubeMusic(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")
        self.user_id = "test_user_end_to_end"

    async def asyncTearDown(self):
        await self.client.aclose()

    def make_alexa_request(self, req_type="IntentRequest", intent_name=None, slots=None, extra_req=None, access_token=None):
        req = {
            "version": "1.0",
            "session": {"new": True, "sessionId": "s_123"},
            "context": {
                "System": {
                    "application": {"applicationId": "amzn1.ask.skill.test"},
                    "user": {"userId": self.user_id},
                }
            },
            "request": {"type": req_type, "timestamp": "2026-11-15T12:00:00Z"},
        }
        if access_token:
            req["context"]["System"]["user"]["accessToken"] = access_token
        if intent_name:
            req["request"]["intent"] = {"name": intent_name, "slots": slots or {}}
        if extra_req:
            req["request"].update(extra_req)
        return req

    async def test_01_launch_request(self):
        req = self.make_alexa_request(req_type="LaunchRequest")
        resp = await self.client.post("/alexa", json=req)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        speech = data["response"]["outputSpeech"]["text"]
        self.assertIn("YouTube Music is ready", speech)
        self.assertFalse(data["response"]["shouldEndSession"])

    async def test_02_clean_query_and_fuzzy_matching(self):
        # Target from user's actual library: aajkl Punjabi
        score = match_playlist_score("my aajkal punjabi playlist", "aajkl Punjabi")
        self.assertGreaterEqual(score, 0.90)

        # Single letter Z
        score_z = match_playlist_score("play Z playlist", "Z")
        self.assertGreaterEqual(score_z, 0.90)

        # Ensure random words don't match Z
        score_bad = match_playlist_score("random unrelated stuff", "Z")
        self.assertLess(score_bad, 0.50)

    async def test_03_public_curated_playlist(self):
        req = self.make_alexa_request(
            intent_name="PlayPlaylistIntent",
            slots={"playlist": {"value": "lofi"}},
        )
        resp = await self.client.post("/alexa", json=req)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        speech = data["response"]["outputSpeech"]["text"]
        self.assertIn("Lofi Playlist", speech)
        directives = data["response"].get("directives", [])
        self.assertEqual(len(directives), 1)
        self.assertEqual(directives[0]["type"], "AudioPlayer.Play")

    async def test_04_unknown_playlist_strict_stop_no_video(self):
        # Strict hierarchy rule: if playlist not found, say not found and DO NOT play random video!
        req = self.make_alexa_request(
            intent_name="PlayPlaylistIntent",
            slots={"playlist": {"value": "completely_nonexistent_xyz_playlist_123"}},
        )
        resp = await self.client.post("/alexa", json=req)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        speech = data["response"]["outputSpeech"]["text"]
        self.assertIn("could not find the playlist", speech.lower())
        directives = data["response"].get("directives", [])
        self.assertEqual(len(directives), 0)  # NO DIRECTIVES!

    async def test_05_song_search(self):
        req = self.make_alexa_request(
            intent_name="PlayMusicIntent",
            slots={"query": {"value": "Starboy"}},
        )
        resp = await self.client.post("/alexa", json=req)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        speech = data["response"]["outputSpeech"]["text"]
        self.assertIn("Starboy", speech)
        directives = data["response"].get("directives", [])
        self.assertEqual(len(directives), 1)
        self.assertEqual(directives[0]["type"], "AudioPlayer.Play")

    async def test_06_audio_navigation_and_gapless_queue(self):
        # Mock populate user queue with 5 tracks
        user_queues[self.user_id] = {
            "tracks": [
                {"videoId": f"v_{i}", "title": f"Song {i}", "author": "Artist"}
                for i in range(1, 6)
            ],
            "current_index": 0,
            "current_offset": 0,
            "repeat": False,
            "shuffle": False,
        }

        # 1. Test PlaybackNearlyFinished (must pre-enqueue next song, and MUST NOT contain outputSpeech!)
        req_pnf = self.make_alexa_request(
            req_type="AudioPlayer.PlaybackNearlyFinished",
            extra_req={"token": "v_1"},
        )
        resp_pnf = await self.client.post("/alexa", json=req_pnf)
        data_pnf = resp_pnf.json()
        self.assertNotIn("outputSpeech", data_pnf["response"], "AudioPlayer events must never contain outputSpeech!")
        dir_pnf = data_pnf["response"]["directives"][0]
        self.assertEqual(dir_pnf["playBehavior"], "ENQUEUE")
        self.assertEqual(dir_pnf["audioItem"]["stream"]["token"], "v_2")

        # 2. Test AMAZON.NextIntent (must skip to v_2, REPLACE_ALL, and MUST NOT contain outputSpeech!)
        req_next = self.make_alexa_request(intent_name="AMAZON.NextIntent")
        resp_next = await self.client.post("/alexa", json=req_next)
        data_next = resp_next.json()
        self.assertNotIn("outputSpeech", data_next["response"], "NextIntent must not contain outputSpeech in AudioPlayer!")
        dir_next = data_next["response"]["directives"][0]
        self.assertEqual(dir_next["playBehavior"], "REPLACE_ALL")
        self.assertEqual(dir_next["audioItem"]["stream"]["token"], "v_2")
        self.assertEqual(user_queues[self.user_id]["current_index"], 1)

        # 3. Test AMAZON.PreviousIntent (must skip back to v_1)
        req_prev = self.make_alexa_request(intent_name="AMAZON.PreviousIntent")
        resp_prev = await self.client.post("/alexa", json=req_prev)
        data_prev = resp_prev.json()
        self.assertNotIn("outputSpeech", data_prev["response"])
        dir_prev = data_prev["response"]["directives"][0]
        self.assertEqual(dir_prev["audioItem"]["stream"]["token"], "v_1")
        self.assertEqual(user_queues[self.user_id]["current_index"], 0)

        # 4. Test Pause / Stop
        req_pause = self.make_alexa_request(intent_name="AMAZON.PauseIntent")
        resp_pause = await self.client.post("/alexa", json=req_pause)
        data_pause = resp_pause.json()
        self.assertEqual(data_pause["response"]["directives"][0]["type"], "AudioPlayer.Stop")

        # 5. Test Resume
        req_resume = self.make_alexa_request(intent_name="AMAZON.ResumeIntent")
        resp_resume = await self.client.post("/alexa", json=req_resume)
        data_resume = resp_resume.json()
        self.assertEqual(data_resume["response"]["directives"][0]["type"], "AudioPlayer.Play")

        # 6. Test PlaybackFailed Auto-Recovery
        req_fail = self.make_alexa_request(
            req_type="AudioPlayer.PlaybackFailed",
            extra_req={"token": "v_1", "error": {"type": "MEDIA_ERROR_INTERNAL_DEVICE_ERROR"}},
        )
        resp_fail = await self.client.post("/alexa", json=req_fail)
        data_fail = resp_fail.json()
        dir_fail = data_fail["response"]["directives"][0]
        self.assertEqual(dir_fail["playBehavior"], "REPLACE_ALL")
        self.assertEqual(dir_fail["audioItem"]["stream"]["token"], "v_2")
        self.assertNotIn("outputSpeech", data_fail["response"])


if __name__ == "__main__":
    unittest.main()
