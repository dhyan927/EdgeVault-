"""
EdgeVault FAKE hub (Siddharth's dev server)

Pretends to be the 4 cameras so the dashboard can be built and demoed
before the real camera code exists. Later, the real cameras will POST
their events to /event and this file keeps working as the real hub.

Run:  python server.py          (listens on http://localhost:9000)
"""
import base64
import asyncio
import random
import sys
import time
import hashlib
import secrets

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

CAPACITY_MB = 100
MAX_VIDEO_BYTES = 100 * 1024 * 1024
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}
CAMS = ["cam1", "cam2", "cam3", "cam4"]
CAM_NAMES = {"cam1": "Front Door", "cam2": "Hallway", "cam3": "Server Room", "cam4": "Parking"}
VIDEO_DIR = ROOT_DIR / "videos"
STORAGE_DIR = ROOT_DIR / "storage"   # each camera's disk: storage/<cam>/
KEY_DIR = ROOT_DIR / ".keys"         # per-camera AES keys (add .keys/ to .gitignore)
CAM_VIDEOS = {
    "cam1": "hallway.mp4",
    "cam2": "normal.mp4",
    "cam3": "suspicious.mp4",
    "cam4": "seemingly_suspicious.mp4",
}

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/videos", StaticFiles(directory=VIDEO_DIR), name="videos")

# ---------- shared state ----------
state = {
    "cams": {
        c: {"name": CAM_NAMES[c], "online": True, "used_mb": random.randint(15, 40),
            "capacity_mb": CAPACITY_MB, "last_score": None, "status": "calm",
            "threat_level": "low", "video_name": CAM_VIDEOS[c],
            "video_url": f"http://localhost:9000/videos/{CAM_VIDEOS[c]}",
            "video_size_mb": round((VIDEO_DIR / CAM_VIDEOS[c]).stat().st_size / 1e6, 2)}
        for c in CAMS
    },
    "ledger": {c: 100.0 for c in CAMS},       # fake credits
    "stored_for_others": {c: 0 for c in CAMS},  # MB of other cameras' clips held here
}
log = []            # last events
clients = set()     # connected websockets
clip_counter = 0
auctioned_cameras = set()


class VideoAnalysis(BaseModel):
    cam: str
    frames: list[str] = Field(min_length=3, max_length=3)


async def broadcast(kind, data):
    dead = []
    for ws in clients:
        try:
            await ws.send_json({"kind": kind, "data": data})
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def emit(cam, type_, msg, score=None, **extra):
    """type_: perceive | reason | bid | transfer | evict | compress | audit | alert | offline"""
    event = {"ts": time.strftime("%H:%M:%S"), "cam": cam, "type": type_, "msg": msg,
             "score": score, **extra}
    log.append(event)
    del log[:-200]
    await broadcast("event", event)
    await broadcast("state", state)


def next_clip():
    global clip_counter
    clip_counter += 1
    return f"c{clip_counter}"


# ---------- video analysis and storage auction ----------
def threat_level(score):
    if score >= 0.7:
        return "critical"
    if score >= 0.3:
        return "watch"
    return "low"


def _store_on_peer(owner, peer, clip, video_name):
    """Really store `owner`'s clip on `peer`'s disk, then audit it."""
    from camera.crypto import VaultCrypto   # needs: pip install cryptography

    KEY_DIR.mkdir(exist_ok=True)
    blob = VaultCrypto(str(KEY_DIR / f"{owner}_key.bin")).encrypt((VIDEO_DIR / video_name).read_bytes())

    dest_dir = STORAGE_DIR / peer / f"stored_for_{owner}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{clip}.bin"
    dest.write_bytes(blob)

    nonce = secrets.token_hex(16).encode("utf-8")
    expected = hashlib.sha256(nonce + blob).hexdigest()
    proof = hashlib.sha256(nonce + dest.read_bytes()).hexdigest()
    return {"path": dest, "size_mb": round(len(blob) / 1e6, 2), "audit_ok": proof == expected}


async def auction_video(cam, score=None):
    """score=None means the auction was started manually (not by a Gemini score)."""
    camera = state["cams"][cam]
    clip = next_clip()
    size_mb = camera["video_size_mb"]
    if score is None:
        await emit(cam, "reason", f"{clip}: local storage above 80%, requesting peer bids")
    else:
        await emit(cam, "reason", f"{clip}: suspicious-activity score {score:.2f}; local storage above 80%, requesting peer bids", score)

    bids = {}
    for peer in CAMS:
        if peer == cam or not state["cams"][peer]["online"]:
            continue
        peer_state = state["cams"][peer]
        free_mb = max(0, peer_state["capacity_mb"] - peer_state["used_mb"])
        if free_mb < size_mb:
            continue
        ask_price = round(0.5 + 2.0 * peer_state["used_mb"] / peer_state["capacity_mb"], 2)
        bids[peer] = ask_price
        await emit(peer, "bid", f"Bid {ask_price:.2f} credits for {clip} ({free_mb:.2f} MB free)", score, cam_from=cam)

    # cheapest bidder first; if it can't really store the clip, try the next one
    stored = None
    for winner in sorted(bids, key=bids.get):
        try:
            result = await run_in_threadpool(_store_on_peer, cam, winner, clip, camera["video_name"])
        except Exception as error:
            await emit(winner, "alert", f"{clip}: could not store the clip for {cam}: {error}")
            continue
        if not result["audit_ok"]:
            await emit(winner, "audit", f"{clip}: storage proof FAILED, trying the next bidder")
            continue
        stored = (winner, bids[winner], result)
        break

    if stored is None:
        camera["used_mb"] = round(max(0, camera["used_mb"] - size_mb / 2), 2)
        camera["status"] = "full" if camera["used_mb"] >= camera["capacity_mb"] * 0.9 else "alert"
        await emit(cam, "compress", f"{clip}: no peer could store it; retained at half size", score)
        return

    winner, price, result = stored
    stored_mb = result["size_mb"]
    camera["used_mb"] = round(max(0, camera["used_mb"] - stored_mb), 2)
    camera["status"] = "full" if camera["used_mb"] >= camera["capacity_mb"] * 0.9 else "alert"
    state["cams"][winner]["used_mb"] = round(state["cams"][winner]["used_mb"] + stored_mb, 2)
    state["stored_for_others"][winner] = round(state["stored_for_others"][winner] + stored_mb, 2)
    state["ledger"][cam] = round(state["ledger"][cam] - price, 2)
    state["ledger"][winner] = round(state["ledger"][winner] + price, 2)
    where = result["path"].relative_to(STORAGE_DIR.parent).as_posix()
    await emit(cam, "transfer", f"{clip}: {stored_mb:.2f} MB encrypted and stored on {winner} ({where}) for {price:.2f} credits", score, to=winner)
    await emit(winner, "audit", f"Audit scheduled for {clip}")
    await emit(winner, "audit", f"{clip}: storage proof passed")

async def disk_full(cam="cam1"):
    cameras = CAMS if cam == "all" else [cam]
    for camera_id in cameras:
        if camera_id not in state["cams"]:
            raise HTTPException(status_code=404, detail=f"Unknown camera: {camera_id}")
        state["cams"][camera_id]["used_mb"] = 95
        state["cams"][camera_id]["status"] = "full"
        await emit(camera_id, "alert", "Storage at 95%")


async def toggle_cam(cam):
    cameras = CAMS if cam == "all" else [cam]
    for camera_id in cameras:
        if camera_id not in state["cams"]:
            raise HTTPException(status_code=404, detail=f"Unknown camera: {camera_id}")
        c = state["cams"][camera_id]
        c["online"] = not c["online"]
        c["status"] = (
            "offline" if not c["online"] else
            "full" if c["used_mb"] >= c["capacity_mb"] * 0.9 else
            "alert" if c.get("threat_level") == "critical" else
            "watch" if c.get("threat_level") == "watch" else "calm"
        )
        await emit(camera_id, "offline" if not c["online"] else "alert",
                   "Camera went OFFLINE" if not c["online"] else "Camera back ONLINE")


async def reset():
    for c in CAMS:
        state["cams"][c].update(online=True, used_mb=random.randint(15, 40), last_score=None, status="calm", threat_level="low")
        state["ledger"][c] = 100.0
        state["stored_for_others"][c] = 0
    auctioned_cameras.clear()
    log.clear()
    await broadcast("state", state)
    await emit("hub", "alert", "Demo reset")


# ---------- HTTP endpoints ----------
@app.get("/state")
def get_state():
    return {"state": state, "log": log}


@app.get("/video_sources")
def get_video_sources():
    return [
        {"name": path.name, "size_mb": round(path.stat().st_size / 1e6, 2)}
        for path in sorted(VIDEO_DIR.iterdir())
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]


@app.post("/video_sources")
async def upload_video(request: Request, filename: str):
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Use a plain video filename")
    if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported video format")
    contents = await request.body()
    if not contents:
        raise HTTPException(status_code=400, detail="The uploaded video is empty")
    if len(contents) > MAX_VIDEO_BYTES:
        raise HTTPException(status_code=413, detail="Videos must be 100 MB or smaller")
    destination = VIDEO_DIR / filename
    if destination.exists():
        raise HTTPException(status_code=409, detail="A video with that filename already exists")
    destination.write_bytes(contents)
    return {"name": filename, "size_mb": round(len(contents) / 1e6, 2)}


@app.post("/camera_video")
async def select_camera_video(body: dict):
    cam = body.get("cam")
    filename = body.get("video")
    if cam != "all" and cam not in state["cams"]:
        raise HTTPException(status_code=404, detail=f"Unknown camera: {cam}")
    if not isinstance(filename, str) or Path(filename).name != filename or Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Choose a valid video source")
    video_path = VIDEO_DIR / filename
    if not video_path.is_file():
        raise HTTPException(status_code=404, detail="Video source not found")

    targets = CAMS if cam == "all" else [cam]
    video_url = f"http://localhost:9000/videos/{filename}"
    for camera_id in targets:
        camera = state["cams"][camera_id]
        camera.update(video_name=filename, video_url=video_url, video_size_mb=round(video_path.stat().st_size / 1e6, 2))
        camera["last_score"] = None
        camera["threat_level"] = "low"
        camera.pop("analysis_reason", None)
        camera["status"] = "full" if camera["online"] and camera["used_mb"] >= camera["capacity_mb"] * 0.9 else ("calm" if camera["online"] else "offline")
        auctioned_cameras.discard(camera_id)
        await emit(camera_id, "reason", f"Video source changed to {filename}; analyzing live frames")
    return {"ok": True, "cameras": targets}


@app.post("/trigger/disk_full")
async def trigger_disk_full(cam: str = "cam1"):
    await disk_full(cam)
    return {"ok": True}


@app.post("/trigger/toggle/{cam}")
async def trigger_toggle(cam: str):
    await toggle_cam(cam)
    return {"ok": True}


@app.post("/camera_analysis")
async def analyze_camera_video(analysis: VideoAnalysis):
    if analysis.cam not in state["cams"]:
        raise HTTPException(status_code=404, detail=f"Unknown camera: {analysis.cam}")
    camera = state["cams"][analysis.cam]
    if not camera["online"]:
        return {"ok": False, "error": "Camera is offline"}

    for frame in analysis.frames:
        if len(frame) > 700_000:
            raise HTTPException(status_code=413, detail="Each analyzed frame must be smaller than 512 KB")
        try:
            decoded = base64.b64decode(frame, validate=True)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Frames must be base64-encoded JPEG images") from error
        if not decoded or len(decoded) > 512 * 1024:
            raise HTTPException(status_code=413, detail="Each analyzed frame must be smaller than 512 KB")

    context = {
        "camera_name": camera["name"],
        "is_restricted": analysis.cam == "cam3",
        "disk_percent": round(100 * camera["used_mb"] / camera["capacity_mb"], 1),
    }
    try:
        from scorer import score_video_frames
    except ImportError as error:
        raise HTTPException(
            status_code=503,
            detail="Gemini scoring is unavailable. Install google-genai and python-dotenv.",
        ) from error
    try:
        result = await run_in_threadpool(score_video_frames, analysis.frames, context)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Gemini video analysis failed: {error}") from error

    score = round(result["score"], 2)
    reason = result["reason"]
    level = threat_level(score)
    previous_score = camera["last_score"]
    previous_level = camera.get("threat_level", "low")
    camera["last_score"] = score
    camera["analysis_reason"] = reason
    camera["threat_level"] = level
    camera["status"] = "full" if camera["used_mb"] >= 90 else ("alert" if level == "critical" else ("watch" if level == "watch" else "calm"))
    await broadcast("state", state)

    if level != previous_level or previous_score is None or abs(score - previous_score) >= 0.1:
        clip = next_clip()
        await emit(analysis.cam, "reason", f"{clip}: threat score {score:.2f} ({level}). {reason}", score)

    if level != "critical":
        auctioned_cameras.discard(analysis.cam)
    elif camera["used_mb"] / camera["capacity_mb"] >= 0.8 and analysis.cam not in auctioned_cameras:
        auctioned_cameras.add(analysis.cam)
        await auction_video(analysis.cam, score)
    return {"ok": True, "score": score, "level": level}

@app.post("/trigger/auction")
async def trigger_auction(cam: str = "cam1"):
    """Demo button: fill the disk if needed, then run the storage auction now."""
    cameras = CAMS if cam == "all" else [cam]
    for camera_id in cameras:
        if camera_id not in state["cams"]:
            raise HTTPException(status_code=404, detail=f"Unknown camera: {camera_id}")
    for camera_id in cameras:
        camera = state["cams"][camera_id]
        if not camera["online"]:
            continue
        if camera["used_mb"] < camera["capacity_mb"] * 0.8:
            await disk_full(camera_id)
        await auction_video(camera_id)
    return {"ok": True}

@app.post("/trigger/reset")
async def trigger_reset():
    await reset()
    return {"ok": True}


@app.post("/event")
async def receive_event(body: dict):
    """Real cameras will POST here later: {"cam","type","msg","score"}"""
    extra = {k: body[k] for k in ("to", "cam_from") if k in body}   # e.g. {"to": "cam4"} on a transfer
    await emit(body.get("cam", "?"), body.get("type", "perceive"), body.get("msg", ""), body.get("score"), **extra)
    return {"ok": True}


@app.post("/camera_state")
async def camera_state(body: dict):
    """Real cameras send their numbers here: used_mb, last_score, status, online, credits, hosted_mb, thumb_url"""
    cam = body.get("cam")
    if cam not in state["cams"]:
        return {"ok": False, "error": f"unknown camera {cam}"}
    c = state["cams"][cam]
    for k in ("used_mb", "online", "last_score", "status", "thumb_url", "video_url"):
        if k in body:
            c[k] = body[k]
    if "credits" in body:
        state["ledger"][cam] = body["credits"]
    if "hosted_mb" in body:
        state["stored_for_others"][cam] = body["hosted_mb"]
    await broadcast("state", state)
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_json({"kind": "state", "data": state})
    await ws.send_json({"kind": "history", "data": log})
    try:
        while True:
            await ws.receive_text()   # keep the connection open
    except WebSocketDisconnect:
        clients.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
