"""Sequential per-movie pipeline. Only authorized direct media URLs are accepted."""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

from showfars.telegram import TelegramPublisher

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("showfars")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def load_config():
    return read_json(ROOT / "config.json", {})


def valid_https(url: str, allowed_hosts: list[str]) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and bool(host) and any(
            host == allowed or host.endswith("." + allowed)
            for allowed in allowed_hosts
        )
    except (ValueError, AttributeError):
        return False


def download_source(movie: dict, config: dict, target: Path) -> None:
    if movie.get("rights_confirmed") is not True:
        raise ValueError("rights_confirmed must be true before processing")
    url = movie.get("source_url", "")
    if not valid_https(url, config.get("allowed_download_hosts", [])):
        raise ValueError("source URL is not HTTPS on an explicitly allowlisted host")
    max_bytes = int(config.get("max_source_size_mb", 4096)) * 1024 * 1024
    with requests.get(url, stream=True, timeout=int(config.get("download_timeout_seconds", 60)),
                      headers={"User-Agent": "ShowFars/1.0"}) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type.startswith(("text/html", "application/json")):
            raise ValueError("source returned a webpage/API response, not a direct media file")
        length = response.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise ValueError("source exceeds configured maximum size")
        written = 0
        with target.open("wb") as out:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("download exceeded configured maximum size")
                out.write(chunk)
    if target.stat().st_size < 1024:
        raise ValueError("downloaded file is empty or implausibly small")


def run_cmd(command: list[str]) -> None:
    result = subprocess.run(command, text=True, capture_output=True, timeout=5 * 60 * 60)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {result.stderr[-2500:]}")


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_type,width,height", "-of", "json", str(path)],
        text=True, capture_output=True, timeout=60,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe rejected file: {result.stderr[-1000:]}")
    data = json.loads(result.stdout)
    if not any(s.get("codec_type") == "video" for s in data.get("streams", [])):
        raise ValueError("input/output contains no video stream")
    return data


def encode_three(source: Path, work: Path, config: dict) -> dict[str, Path]:
    probe(source)
    outputs = {}
    for quality, width in (("1080p", 1920), ("720p", 1280), ("480p", 854)):
        dest = work / f"{quality}.mp4"
        vf = f"scale='min({width},iw)':-2:force_original_aspect_ratio=decrease"
        run_cmd([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
            "-vf", vf, "-c:v", "libx265", "-preset", str(config.get("ffmpeg_preset", "medium")),
            "-crf", str(config.get("video_crf", 27)), "-tag:v", "hvc1",
            "-c:a", "aac", "-b:a", str(config.get("audio_bitrate", "128k")),
            "-movflags", "+faststart", str(dest),
        ])
        info = probe(dest)
        if dest.stat().st_size < 1024:
            raise ValueError(f"{quality} output is empty")
        limit = int(config.get("max_output_size_mb", 49)) * 1024 * 1024
        if dest.stat().st_size > limit:
            raise ValueError(f"{quality} output exceeds {config.get('max_output_size_mb')} MB upload limit")
        outputs[quality] = dest
        LOG.info("Validated %s: %s bytes, duration=%s", quality, dest.stat().st_size,
                 info.get("format", {}).get("duration"))
    return outputs


def poster_bytes(movie: dict, config: dict) -> bytes | None:
    url = movie.get("poster_url")
    if not url or not valid_https(url, config.get("allowed_poster_hosts", [])):
        return None
    try:
        response = requests.get(url, timeout=30, headers={"User-Agent": "ShowFars/1.0"})
        response.raise_for_status()
        if not response.headers.get("Content-Type", "").lower().startswith("image/"):
            return None
        if len(response.content) > 10 * 1024 * 1024:
            return None
        return response.content
    except requests.RequestException as exc:
        LOG.warning("Poster unavailable: %s", exc)
        return None


def build_caption(movie: dict) -> str:
    title = html.escape(str(movie.get("title", "بدون عنوان")))
    year = html.escape(str(movie.get("year", "—")))
    description = html.escape(str(movie.get("description", "—")))
    return f"<b>{title}</b>\nسال: {year}\n\n{description}\n\n#ShowFars"


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    movies = read_json(ROOT / "data/movies.json", [])
    state_path = ROOT / "data/state.json"
    state = read_json(state_path, {"published": {}, "failed": {}})
    if not isinstance(movies, list) or not isinstance(state, dict):
        LOG.error("Invalid movies/state JSON structure")
        return 2

    dry_run = os.getenv("SHOWFARS_DRY_RUN", str(config.get("dry_run_default", True))).lower() == "true"
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not dry_run and (not token or not chat_id):
        LOG.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets are required for live publishing")
        return 2

    publisher = TelegramPublisher(token, chat_id) if not dry_run else None
    max_movies = max(1, int(config.get("max_movies_per_run", 1)))
    processed = failures = 0
    for movie in movies:
        if processed >= max_movies:
            break
        if not isinstance(movie, dict) or not movie.get("id") or not movie.get("title"):
            LOG.error("Skipping malformed movie entry")
            failures += 1
            continue
        identity = str(movie["id"])
        prior = state.get("published", {}).get(identity, {})
        prior_ids = prior.get("message_ids", {}) if isinstance(prior, dict) else {}
        if all(key in prior_ids for key in ("lead", "1080p", "720p", "480p")):
            LOG.info("Already published; skip %s", identity)
            continue
        processed += 1
        work = ROOT / "work" / hashlib.sha256(identity.encode()).hexdigest()[:16]
        work.mkdir(parents=True, exist_ok=True)
        try:
            source = work / "source.bin"
            download_source(movie, config, source)
            outputs = encode_three(source, work, config)
            caption = build_caption(movie)
            poster = poster_bytes(movie, config)
            if dry_run:
                LOG.info("DRY RUN: %s validated; three files ready; no Telegram messages sent", identity)
            else:
                record = state.setdefault("published", {}).setdefault(identity, {
                    "title": movie["title"], "message_ids": {}, "sha256": {},
                })
                record["title"] = movie["title"]
                record.setdefault("message_ids", {}).update(prior_ids)

                def checkpoint(message_ids):
                    record["message_ids"] = dict(message_ids)
                    record["sha256"] = {q: hashlib.sha256(p.read_bytes()).hexdigest() for q, p in outputs.items()}
                    state.setdefault("failed", {}).pop(identity, None)
                    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

                message_ids = publisher.publish_movie(
                    movie, caption, poster, outputs,
                    existing_ids=record["message_ids"], checkpoint=checkpoint,
                )
                checkpoint(message_ids)
                LOG.info("Published %s with message IDs %s", identity, message_ids)
        except Exception as exc:
            failures += 1
            state.setdefault("failed", {})[identity] = {"error": str(exc)[:1000]}
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            LOG.exception("Movie failed: %s", identity)
        finally:
            for path in work.glob("*"):
                try:
                    path.unlink()
                except OSError:
                    pass
            try:
                work.rmdir()
            except OSError:
                pass
    LOG.info("Summary: catalog=%d processed=%d failures=%d published=%d dry_run=%s",
             len(movies), processed, failures, len(state.get("published", {})), dry_run)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
