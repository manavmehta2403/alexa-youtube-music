# 🎵 Alexa YouTube Bridge (Hindi, English, Shuffle & Repeat)

An open-source, multi-cloud bridge that enables **Amazon Echo & Alexa** devices to search, stream, and play music directly from **YouTube & YouTube Music**.

Supports **English & Hindi/Hinglish commands**, **Shuffle & Repeat modes**, **public search**, and **Google Account Linking (OAuth 2.0)** so each user can access their personal **Liked Songs**, **Punjabi playlists**, and **personal library**.

---

## 🌟 Key Features
- **Hindi, Punjabi & English Support:** Full support for Hinglish natural queries (*"Jagjit Singh ke ganne lagao"*, *"Punjabi playlist bajao"*).
- **Shuffle & Repeat:** Say *"play my liked songs on shuffle"* or *"play on repeat"*.
- **Multi-User Google Login:** Each user links their own Google account via the Alexa app to stream their personal library.
- **Anti-Bot Failover Pool:** Resolves streams through decentralized Invidious nodes with 0 datacenter IP blocks.
- **Continuous Autoplay:** Enqueues recommended tracks seamlessly via Alexa's `AudioPlayer.PlaybackNearlyFinished` protocol.
- **Hardware & Voice Controls:** Play, Pause, Resume, Next, Previous, Repeat, and Volume controls.

---

## 🗣️ Voice Commands (English & Hindi)

### 🔀 Shuffle & Repeat Commands
* *"Alexa, ask YouTube Music to play my liked songs on shuffle"*
* *"Alexa, ask YouTube Music to play my liked songs on repeat"*
* *"Alexa, ask YouTube Music to shuffle my Punjabi playlist"*
* *"Alexa, shuffle"* / *"Alexa, repeat"*

### 🇮🇳 Hindi / Hinglish Commands
* *"Alexa, ask YouTube Music Jagjit Singh ke ganne lagao"*
* *"Alexa, ask YouTube Music Arijit Singh ke gaane sunao"*
* *"Alexa, ask YouTube Music Punjabi playlist bajao"*
* *"Alexa, ask YouTube Music mere liked gane bajao"*
* *"Alexa, ask YouTube Music mere pasandida gane shuffle par lagao"*
* *"Alexa, ask YouTube Music shuru se lagao"* / *"Alexa, band karo"*

### 🇬🇧 English Commands
* *"Alexa, ask YouTube Music to play Starboy"*
* *"Alexa, ask YouTube Music to play Believer by Imagine Dragons"*
* *"Alexa, ask YouTube Music to play playlist Lofi Chill"*
* *"Alexa, ask YouTube Music to play my liked songs"*
* *"Alexa, next"* / *"Alexa, previous"* / *"Alexa, pause"* / *"Alexa, resume"*

*(Note: If you configured your invocation name as `youtube music`, say "ask YouTube Music to...". If set to `youtube`, say "ask YouTube Music to...")*

---

## 🚀 Quick Deploy to Any Cloud

Choose your preferred hosting provider. All options support Docker and Python out of the box:

### Option A: Render.com (Recommended - 100% Free Forever)
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)
1. Fork or push this repository to your GitHub account.
2. In [Render Dashboard](https://dashboard.render.com), click **New +** → **Web Service**.
3. Select this repository:
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Plan:** `Free`
4. Click **Create Web Service**. Your live endpoint will be:
   `https://<your-service-name>.onrender.com/alexa`

---

### Option B: Railway.app
1. Go to [Railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
2. Select this repo. Railway automatically detects the `Procfile` and `Dockerfile`.
3. Go to **Settings** → **Generate Domain**. Your endpoint is:
   `https://<your-domain>.up.railway.app/alexa`

---

### Option C: Koyeb (Free Tier)
1. In [Koyeb Dashboard](https://app.koyeb.com), click **Create App** → **GitHub**.
2. Select this repo.
3. Set Port to `8000`. Your endpoint is:
   `https://<your-app>.koyeb.app/alexa`

---

### Option D: Docker / Local Run
```bash
git clone https://github.com/manavmehta2403/alexa-youtube-music.git
cd alexa-youtube-music

# Run with Python:
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000

# Or run with Docker:
docker build -t alexa-ytmusic .
docker run -p 8000:8000 alexa-ytmusic
```

---

## 🔑 How to Setup Google Account Login (Personal Library & Liked Songs)

Google Account Linking allows Alexa to fetch your personal YouTube Music library. Because this uses standard OAuth 2.0, **multiple users can each link their own separate Google account!**

### Step 1: Create OAuth Credentials in Google Cloud Console
1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project (e.g. `My Alexa YouTube Music`).
2. Go to **APIs & Services** → **Library** → Search for **YouTube Data API v3** → Click **Enable**.
3. Go to **APIs & Services** → **OAuth consent screen**:
   - User Type: **External**
   - App Name: `YouTube Music for Alexa`
   - User Support Email: Your email
   - Under **Scopes**, click **Add or Remove Scopes** and add:
     - `https://www.googleapis.com/auth/youtube.readonly`
     - `openid`, `profile`, `email`
   - Under **Test Users**, add your personal Google email (and any family/friends' emails).
4. Go to **APIs & Services** → **Credentials** → **Create Credentials** → **OAuth Client ID**:
   - Application Type: **Web Application**
   - Name: `Alexa Skill Client`
   - Click **Create**. Copy your **Client ID** and **Client Secret**.

---

### Step 2: Configure Account Linking in Alexa Developer Console
1. Open your skill in the [Alexa Developer Console](https://developer.amazon.com/alexa/console/ask).
2. In the left sidebar, click **Account Linking** (under *Tools*).
3. Toggle **Do you allow users to create an account or link to an existing account?** to **YES**.
4. Configure these fields:
   - **Security Provider Information:** Choose `Auth Code Grant`.
   - **Authorization URI:** `https://accounts.google.com/o/oauth2/v2/auth?access_type=offline&prompt=consent`
   - **Access Token URI:** `https://oauth2.googleapis.com/token`
   - **Client ID:** *(Paste your Google Client ID from Step 1)*
   - **Client Secret:** *(Paste your Google Client Secret from Step 1)*
   - **Client Authentication Scheme:** `Credentials in request body`
   - **Scope:** Add:
     - `https://www.googleapis.com/auth/youtube.readonly`
     - `openid`, `profile`, `email`
5. At the bottom of the Alexa Account Linking page, copy the **Alexa Redirect URLs** (e.g. `https://pitangui.amazon.com/api/run/skills/...`).
6. Return to Google Cloud Console (from Step 1) → Edit your OAuth 2.0 Web Client → Paste the Alexa Redirect URLs under **Authorized redirect URIs** → Click **Save**.
7. In the Alexa Developer Console, click **Save** (top right).

---

### Step 3: Link Account in Alexa App
1. Open the **Amazon Alexa App** on your phone.
2. Go to **More** → **Skills & Games** → **Your Skills** → **Dev**.
3. Tap **YouTube Music** → Tap **Settings** → Tap **Link Account**.
4. Sign in with your Google account and grant read access to YouTube.
5. Your Google account is now linked!
