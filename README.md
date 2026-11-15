# Alexa YouTube Music Bridge (100% Free & 24/7)

A lightweight, high-performance bridge that enables **Amazon Echo / Alexa** to search, stream, and continuously play any song, artist, or playlist from **YouTube Music** for free.

---

## Features
- **Zero Cost (100% Free):** Runs on Render's free 24/7 cloud tier (no credit card required) or locally on your PC.
- **No IP Blocks / CAPTCHAs:** Uses a smart decentralized failover engine across Invidious instances to extract AAC audio streams without datacenter IP bans.
- **Continuous Gapless Playback:** Listens for Alexa's `AudioPlayer.PlaybackNearlyFinished` events and automatically queues related songs for continuous music playback.
- **Full Voice & Hardware Controls:** Supports Play, Pause, Resume, Next, Previous, Repeat, and Volume controls directly via voice or your Echo buttons.

---

## Step 1: Deploy Backend to Free Cloud (2 Minutes)

You can host this on **Render.com** (completely free forever):

1. **Push this folder to a free GitHub repository** (e.g. `my-alexa-ytmusic`).
2. Go to [Render.com](https://render.com) and create a free account (sign in with GitHub).
3. Click **New +** → **Web Service**.
4. Connect your `my-alexa-ytmusic` repository.
5. Configure the settings:
   - **Environment:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** `Free`
6. Click **Create Web Service**.
7. In ~60 seconds, Render will provide your live public HTTPS URL:
   ```
   https://alexa-youtube-music-xxxx.onrender.com
   ```

*(Optional: To test locally on your computer right now, simply run `uvicorn app:app --reload`)*

---

## Step 2: Set Up Alexa Skill (3 Minutes)

1. Go to the [Alexa Developer Console](https://developer.amazon.com/alexa/console/ask) and log in with the **same Amazon account** used on your Echo devices.
2. Click **Create Skill**:
   - **Skill Name:** `YouTube Music` (or whatever you prefer)
   - **Primary Locale:** Select your language/country (e.g. English US / UK / IN)
   - **Experience:** `Custom`
   - **Hosting method:** `Provision your own`
   - Click **Next** → Select **Start from Scratch** → Click **Create Skill**.
3. **Import Interaction Model:**
   - In the left sidebar, click **Interaction Model** → **JSON Editor**.
   - Drag and drop or paste the entire contents of [`interaction_model.json`](file:///C:/Users/manav/Downloads/alexa-youtube-music/interaction_model.json).
   - Click **Save Model** at the top.
4. **Enable Audio Player:**
   - In the left sidebar, click **Interfaces**.
   - Turn **ON** the toggle for **Audio Player**.
   - Click **Save Interfaces**.
5. **Set the Endpoint:**
   - In the left sidebar, click **Endpoint**.
   - Select **HTTPS**.
   - In the **Default Region** text box, paste your Render URL with `/alexa`:
     ```
     https://alexa-youtube-music-xxxx.onrender.com/alexa
     ```
   - In the SSL dropdown below it, choose:
     `My development endpoint is a sub-domain of a domain that has a wildcard certificate from a certificate authority`
   - Click **Save Endpoints**.
6. **Build Skill:**
   - In the left sidebar, click **Interaction Model** → Click **Build Model**.
   - Wait ~30 seconds for the build to finish.

---

## Step 3: Speak to your Echo Speaker!

Your skill is automatically live on all your Echo devices connected to your Amazon account. You do **not** need to submit it for certification!

Try these commands:
* *"Alexa, ask YouTube Music to play Starboy"*
* *"Alexa, ask YouTube Music to play Believer by Imagine Dragons"*
* *"Alexa, ask YouTube Music to play playlist Lofi Chill"*
* *"Alexa, pause"* / *"Alexa, resume"*
* *"Alexa, next"* / *"Alexa, previous"*
* *"Alexa, restart"*
