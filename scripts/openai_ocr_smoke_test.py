#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import cv2


DEFAULT_IMAGE = Path("state/snapshots/video_snapshot.jpg")
DEFAULT_CREDENTIAL_FILE = Path("openai")
DEFAULT_ROI = (0.0, 0.0, 0.55, 0.28)
FALLBACK_MODEL = "gpt-4o"
CLOCK_RE = re.compile(r"(?<!\d)([0-9]{1,3})[:：.]([0-9]{2})(?!\d)")


PROMPT = (
    "Read the match timer in this image. Return ONLY the timer text exactly as shown. "
    "If added time appears, include it in the same string, for example 90:00+02:30 or 4:32+8. "
    "Examples: 90:00, 45:00+02:30, 90:00 4:32 mins.+8. No explanation."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenAI-compatible OCR smoke test against a local screenshot.")
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIAL_FILE, help="File containing endpoint URL and API key.")
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help="Local image to send to the model.")
    parser.add_argument("--roi", default=",".join(str(v) for v in DEFAULT_ROI), help="Crop ROI as x,y,w,h in 0..1.")
    parser.add_argument("--model", default="", help="OpenAI-compatible model name. If omitted, the script tries available models.")
    return parser.parse_args()


def load_credentials(path: Path) -> tuple[str, str, str]:
    if not path.exists():
        raise FileNotFoundError(f"credential file not found: {path}")
    parts = path.read_text(encoding="utf-8", errors="replace").strip().split()
    if len(parts) < 2:
        raise ValueError("credential file must contain endpoint URL and API key separated by whitespace")
    endpoint = parts[0].strip().rstrip("/")
    api_key = parts[1].strip()
    if not endpoint.startswith(("http://", "https://")):
        raise ValueError("endpoint URL must start with http:// or https://")
    if not api_key:
        raise ValueError("API key is empty")
    model = parts[2].strip() if len(parts) >= 3 else ""
    return endpoint, api_key, model


def parse_roi(value: str) -> tuple[float, float, float, float]:
    parts = [item.strip() for item in value.split(",")]
    if len(parts) != 4:
        raise ValueError("--roi must be x,y,w,h")
    roi = tuple(float(item) for item in parts)
    x, y, w, h = roi
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1 or y + h > 1:
        raise ValueError("ROI must stay within 0..1 and have positive size")
    return roi


def crop_image(image_path: Path, roi: tuple[float, float, float, float]) -> tuple[bytes, tuple[int, int]]:
    img = cv2.imread(str(image_path))
    if img is None:
        raise ValueError(f"unable to read image: {image_path}")
    height, width = img.shape[:2]
    x, y, rw, rh = roi
    crop = img[int(round(y * height)) : int(round((y + rh) * height)), int(round(x * width)) : int(round((x + rw) * width))]
    if crop.size == 0:
        raise ValueError("crop is empty")
    ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise RuntimeError("failed to encode JPEG")
    return buf.tobytes(), crop.shape[:2]


def request_ocr(endpoint: str, api_key: str, model: str, image_bytes: bytes) -> dict:
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": 50,
    }
    req = urllib.request.Request(
        f"{endpoint}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "live-sync-openai-ocr-smoke/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def list_models(endpoint: str, api_key: str) -> list[str]:
    req = urllib.request.Request(
        f"{endpoint}/models",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "live-sync-openai-ocr-smoke/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8", "replace"))
    models = []
    for item in result.get("data") or []:
        if isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
    return models


def candidate_models(endpoint: str, api_key: str, requested_model: str, credential_model: str) -> list[str]:
    if requested_model:
        return [requested_model]
    if credential_model:
        return [credential_model]
    try:
        models = list_models(endpoint, api_key)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        models = []
    if not models:
        return [FALLBACK_MODEL]

    preferred_words = ("gpt", "vision", "vl", "qwen", "gemini", "claude", "llava", "auto")
    preferred = [model for model in models if any(word in model.lower() for word in preferred_words)]
    ordered = preferred + models
    seen = set()
    result = []
    for model in ordered:
        if model in seen:
            continue
        seen.add(model)
        result.append(model)
    return result


def extract_reply(result: dict) -> str:
    choices = result.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts).strip()
    return str(content).strip()


def main() -> int:
    args = parse_args()
    endpoint, api_key, credential_model = load_credentials(args.credentials)
    roi = parse_roi(args.roi)
    image_bytes, crop_size = crop_image(args.image, roi)

    print("=== summary ===")
    print(f"input: {args.image} roi={roi} crop={crop_size}")
    for model in candidate_models(endpoint, api_key, args.model.strip(), credential_model):
        try:
            result = request_ocr(endpoint, api_key, model, image_bytes)
        except urllib.error.HTTPError as exc:
            print(f"model={model} status=http_{exc.code}")
            continue
        except urllib.error.URLError:
            print(f"model={model} status=network_error")
            continue
        except json.JSONDecodeError:
            print(f"model={model} status=bad_json")
            continue

        reply = extract_reply(result)
        match = CLOCK_RE.search(reply)
        print(f"model={model} status=ok reply={reply!r} parsed_clock={match.group(0) if match else ''}")
        if match:
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
