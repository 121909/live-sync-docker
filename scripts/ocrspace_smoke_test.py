#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import uuid
import urllib.error
import urllib.request
from pathlib import Path

import cv2


DEFAULT_IMAGE = Path("state/snapshots/video_snapshot.jpg")
DEFAULT_KEY_FILE = Path("OCR.space")
DEFAULT_ROI = (0.0, 0.0, 0.55, 0.28)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OCR.space smoke test against a local screenshot.")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE, help="Path to the OCR.space API key file.")
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help="Local image to send to OCR.space.")
    parser.add_argument("--roi", default=",".join(str(v) for v in DEFAULT_ROI), help="Crop ROI as x,y,w,h in 0..1.")
    parser.add_argument("--language", default="eng", help="OCR language code.")
    parser.add_argument("--engine", default="2", help="OCR.space engine number.")
    return parser.parse_args()


def load_key(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"API key file not found: {path}")
    key = path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    if not key:
        raise ValueError(f"API key file is empty: {path}")
    return key


def parse_roi(value: str) -> tuple[float, float, float, float]:
    parts = [item.strip() for item in value.split(",")]
    if len(parts) != 4:
        raise ValueError("--roi must be x,y,w,h")
    roi = tuple(float(item) for item in parts)
    x, y, w, h = roi
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1 or y + h > 1:
        raise ValueError("ROI must stay within 0..1 and have positive size")
    return roi


def crop_and_prepare(image_path: Path, roi: tuple[float, float, float, float]) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    img = cv2.imread(str(image_path))
    if img is None:
        raise ValueError(f"Unable to read image: {image_path}")
    height, width = img.shape[:2]
    x, y, rw, rh = roi
    crop = img[int(round(y * height)) : int(round((y + rh) * height)), int(round(x * width)) : int(round((x + rw) * width))]
    if crop.size == 0:
        raise ValueError("Crop is empty")

    if crop.shape[1] < 960:
        scale = max(1.0, 960.0 / max(crop.shape[1], 1))
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    norm = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    if min(norm.shape[:2]) < 240:
        norm = cv2.resize(norm, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    proc = cv2.cvtColor(norm, cv2.COLOR_GRAY2BGR)
    ok, buf = cv2.imencode(".jpg", proc, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        raise RuntimeError("Failed to encode JPEG")
    return buf.tobytes(), crop.shape[:2], proc.shape[:2]


def build_multipart(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[str, bytes]:
    boundary = f"----ocrspace{uuid.uuid4().hex}"
    body = bytearray()
    crlf = b"\r\n"

    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(crlf)

    for name, filename, content_type, data in files:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8"))
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(data)
        body.extend(crlf)

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return boundary, bytes(body)


def error_text(value) -> str:
    if isinstance(value, list):
        return " | ".join(str(item) for item in value if item)
    if value is None:
        return ""
    return str(value)


def ocrspace_request(api_key: str, image_bytes: bytes, filename: str, language: str, engine: str) -> dict:
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = "image/jpeg"
    fields = {
        "apikey": api_key,
        "language": language,
        "isOverlayRequired": "true",
        "OCREngine": str(engine),
        "detectOrientation": "false",
        "scale": "false",
    }
    boundary, body = build_multipart(fields, [("file", filename, content_type, image_bytes)])
    req = urllib.request.Request(
        "https://api.ocr.space/parse/image",
        data=body,
        headers={
            "apikey": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "User-Agent": "live-sync-ocrspace-smoke/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw)


def main() -> int:
    args = parse_args()
    api_key = load_key(args.key_file)
    roi = parse_roi(args.roi)
    image_bytes, original_size, prepared_size = crop_and_prepare(args.image, roi)

    try:
        result = ocrspace_request(api_key, image_bytes, args.image.name, args.language, args.engine)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(f"HTTP {exc.code} from OCR.space", file=sys.stderr)
        print(body, file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1

    exit_code_raw = result.get("OCRExitCode")
    try:
        exit_code = int(str(exit_code_raw).strip())
    except Exception:
        exit_code = None

    parsed_results = result.get("ParsedResults") or []
    parsed_text = ""
    if parsed_results:
        parsed_text = str(parsed_results[0].get("ParsedText") or "").strip()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print()
    print("=== summary ===")
    print(f"input: {args.image} roi={roi} original={original_size} prepared={prepared_size}")
    print(f"OCRExitCode: {exit_code_raw}")
    print(f"IsErroredOnProcessing: {result.get('IsErroredOnProcessing')}")
    print(f"ErrorMessage: {error_text(result.get('ErrorMessage'))}")
    print(f"ErrorDetails: {error_text(result.get('ErrorDetails'))}")
    print(f"ParsedText: {parsed_text!r}")

    if exit_code != 1 or result.get("IsErroredOnProcessing"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
