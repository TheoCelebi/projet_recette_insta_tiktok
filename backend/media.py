"""Récupération du contenu d'une vidéo TikTok / Instagram / YouTube Shorts.

On rassemble tout ce qui peut contenir la recette :
- la légende / description du post (souvent la recette complète y est écrite),
- les sous-titres (manuels ou automatiques) quand la plateforme en fournit,
- la transcription de l'audio (faster-whisper, local) si disponible,
- quelques images clés de la vidéo (texte incrusté : ingrédients, quantités).
"""

from __future__ import annotations

import base64
import glob
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

import imageio_ffmpeg
import yt_dlp

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
MAX_FRAMES = 8
SUB_LANGS = ["fr", "fr-FR", "en", "en-US", "en-GB"]


@dataclass
class MediaContent:
    url: str
    platform: str
    title: str = ""
    description: str = ""
    author: str = ""
    thumbnail: str = ""
    duration: float | None = None
    subtitles: str = ""
    transcript: str = ""
    frames: list[str] = field(default_factory=list)  # JPEG en base64


def _vtt_to_text(vtt: str) -> str:
    lines, last = [], None
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        if line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line != last:
            lines.append(line)
            last = line
    return "\n".join(lines)


def _fetch_subtitles(ydl: yt_dlp.YoutubeDL, info: dict) -> str:
    """Sous-titres manuels puis automatiques, dans la première langue dispo.

    Best effort : un échec (429, format absent...) ne doit pas bloquer l'import.
    """
    for source in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        for lang in SUB_LANGS:
            for track in source.get(lang, []):
                if track.get("ext") != "vtt":
                    continue
                try:
                    return _vtt_to_text(ydl.urlopen(track["url"]).read().decode("utf-8", "ignore"))
                except Exception:  # noqa: BLE001
                    break
    return ""


def _extract_frames(video: str, duration: float | None, out_dir: str) -> list[str]:
    d = duration or 60
    n = max(3, min(MAX_FRAMES, int(d // 6) + 2))
    frames = []
    for i in range(n):
        t = d * (i + 0.5) / n
        out = os.path.join(out_dir, f"frame_{i:02d}.jpg")
        subprocess.run(
            [FFMPEG, "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video,
             "-frames:v", "1", "-vf", "scale=-2:720", "-q:v", "4", out, "-y"],
            check=False,
        )
        if os.path.exists(out):
            with open(out, "rb") as f:
                frames.append(base64.standard_b64encode(f.read()).decode())
    return frames


def _transcribe(video: str) -> str:
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except ImportError:
        return ""
    # Décodage via notre ffmpeg (évite les incompatibilités de version de PyAV)
    pcm = subprocess.run(
        [FFMPEG, "-loglevel", "error", "-i", video, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=False,
    ).stdout
    if not pcm:
        return ""
    audio = np.frombuffer(pcm, dtype=np.float32)
    try:
        model = _whisper_model(WhisperModel)
        segments, _ = model.transcribe(audio, vad_filter=True)
        return " ".join(s.text.strip() for s in segments)
    except Exception:  # noqa: BLE001 - la transcription est un bonus
        return ""


_WHISPER = None


def _whisper_model(cls):
    global _WHISPER
    if _WHISPER is None:
        _WHISPER = cls(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
    return _WHISPER


def fetch(url: str, transcribe: bool = True) -> MediaContent:
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "outtmpl": os.path.join(tmp, "video.%(ext)s"),
            "format": "b[height<=720]/bv*[height<=720]+ba/bv*+ba/b",
            "merge_output_format": "mp4",
            "ffmpeg_location": FFMPEG,
        }
        if cookies := os.environ.get("YTDLP_COOKIES_FROM_BROWSER"):
            opts["cookiesfrombrowser"] = (cookies,)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            subtitles = _fetch_subtitles(ydl, info)

        content = MediaContent(
            url=info.get("webpage_url") or url,
            platform=info.get("extractor_key", "").lower(),
            title=info.get("title") or "",
            description=info.get("description") or "",
            author=info.get("uploader") or info.get("channel") or "",
            thumbnail=info.get("thumbnail") or "",
            duration=info.get("duration"),
        )

        content.subtitles = subtitles
        videos = glob.glob(os.path.join(tmp, "video.*"))
        if videos:
            video = videos[0]
            content.frames = _extract_frames(video, content.duration, tmp)
            if transcribe and not content.subtitles:
                content.transcript = _transcribe(video)
        return content
