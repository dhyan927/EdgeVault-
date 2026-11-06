# EdgeVault

## Run the dashboard

Create a project virtual environment and install the Python dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

In the terminal used to start the hub, set your Gemini API key without saving it
in the repository, then run the hub. If a key has been shared or committed,
revoke it and create a replacement before using the integration:

```powershell
$env:GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
.\.venv\Scripts\python.exe hub\server.py
```

In a second terminal, start the dashboard:

```powershell
Set-Location dashboard
npm ci
npm run dev
```

Open the Vite URL printed in the dashboard terminal (usually
`http://localhost:5173`). Open **Simulation controls** to choose one camera or
**All cameras**, select a bundled video or add a video, then choose **Use video**.
The dashboard samples three low-resolution frames about every 12 seconds and
sends them to Gemini for a suspicious-activity score. Scores and evidence-based
reasons update the camera cards; a high score with local storage above 80%
starts the peer bidding flow.

Video-frame analysis requires a valid Gemini API key. After configuring the key,
restart the hub and reload the dashboard. Frames are sent to Google's Gemini API;
use only footage you are authorized to process. If the key is missing or analysis
fails, the affected camera shows an analysis error instead of a fabricated score.
The dashboard hub is a demo simulator: bids update in-memory storage and credit
balances and do not transfer the video file to a peer camera.