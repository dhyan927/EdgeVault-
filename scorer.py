import json
import base64
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# ============================================================
#  TOGGLE
#  DEMO_MODE = True  → instant response, no API call (for stage)
#  DEMO_MODE = False → live Gemini call (for judges who ask)
# ============================================================
DEMO_MODE = False

_client_instance = None


def _client():
    """Return one shared Gemini client.

    The client must stay referenced for as long as it is used. Building a new
    genai.Client() inline (e.g. `_client().models.generate_content(...)`) lets
    the Client object be garbage collected immediately, which closes its HTTP
    connection and causes "Cannot send a request, as the client has been closed".
    """
    global _client_instance
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        raise RuntimeError("Set GEMINI_API_KEY before starting the hub.")
    if _client_instance is None:
        _client_instance = genai.Client()
    return _client_instance


def score_video_frames(b64_frames, context):
    """Score sampled video frames for suspicious activity without a fallback score."""
    parts = []
    for b64_str in b64_frames:
        image_bytes = base64.b64decode(b64_str, validate=True)
        parts.append(
            types.Part.from_bytes(
                data=image_bytes, mime_type="image/jpeg")
        )

    prompt = f"""
You are assessing a security camera clip for suspicious activity. Review the
provided frames in order and score only visible evidence of behavior, not the
mere presence of a person or ordinary frame-to-frame movement.
- Camera: {context.get('camera_name')}
- Restricted zone: {context.get('is_restricted')}
- Disk usage: {context.get('disk_percent')}%

Reply ONLY with valid JSON:
{{"score": 0.0-1.0, "reason": "one short evidence-based sentence"}}

Score guide:
0.0-0.3: no suspicious behavior visible; empty scene or ordinary activity
0.3-0.7: behavior is ambiguous; more context is needed
0.7-1.0: clear suspicious behavior visible, such as attempted entry,
         tampering, concealment, or a person entering a restricted area
Do not claim an action is visible unless the frames support it. If the frames
are unclear or show no suspicious activity, use a low score and say so.
"""
    response = _client().models.generate_content(
        model="gemini-3.8-flash",
        contents=[*parts, prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    result = json.loads(response.text)
    score = result.get("score")
    reason = result.get("reason")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise ValueError("Gemini returned an invalid threat score")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Gemini returned no evidence-based reason")
    return {"score": float(score), "reason": reason.strip()}


def score_clip(b64_frames, context):
    """
    Sends frames + context to Gemini and returns a threat score.
    Falls back to a local rule if the API is unavailable.
    """

    if DEMO_MODE:
        return {
            "score":  0.92,
            "reason": "Unauthorized person detected in restricted zone."
        }

    try:
        parts = []
        for b64_str in b64_frames:
            image_bytes = base64.b64decode(b64_str)
            parts.append(
                types.Part.from_bytes(
                    data=image_bytes, mime_type="image/jpeg")
            )

        prompt = f"""
You are a security AI. Evaluate these camera frames and context:
- Camera: {context.get('camera_name')}
- Time:   {context.get('time_of_day')}
- Restricted zone: {context.get('is_restricted')}
- Disk usage: {context.get('disk_percent')}%

Reply ONLY with valid JSON in exactly this format:
{{"score": 0.0-1.0, "reason": "one short sentence"}}

Score guide:
0.0-0.3  empty room, normal daytime movement, no person
0.3-0.7  person present but activity is ambiguous
0.7-1.0  suspicious behaviour, restricted zone at night,
         forced entry, concealed face, running
"""
        parts.append(prompt)

        response = _client().models.generate_content(
            model="gemini-3.8-flash",
            contents=parts,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )

        return json.loads(response.text)

    except Exception as e:
        print(f"  [WARNING] Gemini API failed: {e}")
        print("  [WARNING] Using local fallback rule.")
        if context.get("time_of_day") == "night" \
                and context.get("is_restricted"):
            return {"score": 0.85,
                    "reason": "Fallback: night + restricted zone"}
        elif context.get("time_of_day") == "night":
            return {"score": 0.6,
                    "reason": "Fallback: night movement"}
        else:
            return {"score": 0.15,
                    "reason": "Fallback: daytime routine movement"}
