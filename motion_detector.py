#!/usr/bin/env python3
"""Low-resource motion detector for an RTSP stream."""

from __future__ import annotations

import argparse
import json
import os
import signal
import ssl
import sys
import time
import urllib.request

# Keep FFmpeg from building a large queue of old RTSP frames.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

try:
    import cv2
except ModuleNotFoundError:
    cv2 = None  # type: ignore[assignment]


running = True
WEBHOOK_COOLDOWN = 60.0
OPTIONS_PATH = "/data/options.json"
MAX_CONSECUTIVE_READ_ERRORS = 10


def stop_handler(_signum: int, _frame: object) -> None:
    global running
    running = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", help="RTSP URL; omitted when running as a Home Assistant add-on")
    parser.add_argument("--config", default=OPTIONS_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--webhook-url", default="", help=argparse.SUPPRESS)
    parser.set_defaults(webhook_verify_ssl=True)
    parser.add_argument("--width", type=int, default=640, help="Processing width; 0 keeps source size")
    parser.add_argument("--process-fps", type=float, default=5.0, help="Maximum detection rate")
    parser.add_argument("--min-area", type=float, default=900.0, help="Minimum moving contour area in pixels")
    parser.add_argument("--threshold", type=int, default=25, help="Foreground threshold, 0..255")
    parser.add_argument("--history", type=int, default=300, help="MOG2 background history")
    parser.add_argument("--var-threshold", type=float, default=16.0, help="MOG2 sensitivity")
    parser.add_argument("--warmup", type=float, default=3.0, help="Seconds before reporting events")
    parser.add_argument("--cooldown", type=float, default=2.0, help="Seconds between event messages")
    parser.add_argument("--reconnect-delay", type=float, default=3.0, help="Seconds between reconnect attempts")
    parser.add_argument("--stream-timeout", type=float, default=5.0, help="RTSP read timeout in seconds")
    parser.add_argument("--display", action="store_true", help="Show a preview; increases resource usage")
    return parser.parse_args()


def load_addon_options(args: argparse.Namespace) -> argparse.Namespace:
    if args.source:
        return args

    try:
        with open(args.config, encoding="utf-8") as options_file:
            options = json.load(options_file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read Home Assistant add-on options: {error}") from error

    args.source = str(options.get("rtsp_url", "")).strip()
    args.webhook_url = str(options.get("webhook_url", "")).strip()
    args.webhook_verify_ssl = bool(options.get("webhook_verify_ssl", True))
    for name in ("width", "process_fps", "min_area", "threshold", "history", "var_threshold", "warmup", "cooldown", "reconnect_delay", "stream_timeout"):
        if name in options:
            setattr(args, name, options[name])
    return args


def open_capture(source: str) -> cv2.VideoCapture:
    # CAP_PROP_BUFFERSIZE is backend-dependent, so set it and tolerate cameras
    # that ignore it. CAP_FFMPEG gives consistent RTSP handling on Linux builds.
    capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return capture


def send_webhook(url: str, area: float, verify_ssl: bool) -> None:
    payload = json.dumps({"event": "motion", "timestamp": time.time(), "area": round(area)}).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        ssl_context = None if verify_ssl else ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=5, context=ssl_context) as response:
            if response.status >= 300:
                print(f"Webhook returned HTTP {response.status}", file=sys.stderr)
    except Exception as error:
        print(f"Webhook request failed: {error}", file=sys.stderr)


def detect(source: str, args: argparse.Namespace) -> None:
    global running
    interval = 1.0 / args.process_fps
    last_processed = 0.0
    last_event = -float("inf")
    last_webhook = -float("inf")
    webhook_url = args.webhook_url
    if webhook_url and not args.webhook_verify_ssl:
        print("Webhook SSL certificate verification is disabled", file=sys.stderr)

    while running:
        capture = open_capture(source)
        if not capture.isOpened():
            capture.release()
            print("Unable to open RTSP stream; retrying...", file=sys.stderr)
            time.sleep(args.reconnect_delay)
            continue

        subtractor = cv2.createBackgroundSubtractorMOG2(
            history=args.history,
            varThreshold=args.var_threshold,
            detectShadows=False,
        )
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        connected_at = time.monotonic()
        print("RTSP stream connected", file=sys.stderr)

        try:
            consecutive_read_errors = 0
            while running:
                # grab() avoids copying a full BGR frame when this iteration is
                # outside the processing interval and helps keep latency low.
                if not capture.grab():
                    print("RTSP read failed; reconnecting...", file=sys.stderr)
                    break

                now = time.monotonic()
                if now - last_processed < interval:
                    continue
                last_processed = now

                ok, frame = capture.retrieve()
                if not ok or frame is None:
                    consecutive_read_errors += 1
                    if consecutive_read_errors >= MAX_CONSECUTIVE_READ_ERRORS:
                        print("Too many H.264 decode errors; reconnecting...", file=sys.stderr)
                        break
                    continue
                consecutive_read_errors = 0

                if args.width > 0 and frame.shape[1] > args.width:
                    scale = args.width / frame.shape[1]
                    frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                foreground = subtractor.apply(gray, learningRate=-1)
                _, foreground = cv2.threshold(foreground, args.threshold, 255, cv2.THRESH_BINARY)

                # A small opening removes compression noise without expensive
                # multi-scale image processing.
                foreground = cv2.morphologyEx(foreground, cv2.MORPH_OPEN, kernel)
                contours, _ = cv2.findContours(foreground, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                moving_area = sum(cv2.contourArea(contour) for contour in contours)
                moving = moving_area >= args.min_area

                if moving and now - connected_at >= args.warmup and now - last_event >= args.cooldown:
                    last_event = now
                    timestamp = time.time()
                    print(
                        f"MOTION timestamp={timestamp:.3f} area={moving_area:.0f}",
                        flush=True,
                    )
                    if webhook_url and now - last_webhook >= WEBHOOK_COOLDOWN:
                        last_webhook = now
                        send_webhook(webhook_url, moving_area, args.webhook_verify_ssl)

                if args.display:
                    preview = frame.copy()
                    if moving:
                        cv2.putText(preview, "MOTION", (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                    cv2.imshow("motion-detector", preview)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        running = False
                        break
        finally:
            capture.release()
            if args.display:
                cv2.destroyAllWindows()

        if running:
            time.sleep(args.reconnect_delay)


def main() -> int:
    args = parse_args()
    if cv2 is None:
        print("OpenCV is not installed. Run: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    try:
        args = load_addon_options(args)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    if not args.source:
        print("RTSP URL is required", file=sys.stderr)
        return 2
    if args.process_fps <= 0 or args.width < 0 or not 0 <= args.threshold <= 255 or args.stream_timeout <= 0:
        print("Invalid processing or stream timeout value", file=sys.stderr)
        return 2

    timeout_us = int(args.stream_timeout * 1_000_000)
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
        f"rtsp_transport;tcp|timeout;{timeout_us}|rw_timeout;{timeout_us}|fflags;discardcorrupt"
    )
    cv2.setNumThreads(1)
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    try:
        detect(args.source, args)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
