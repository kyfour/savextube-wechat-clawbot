#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Douyin mobile share-page downloader.

Douyin's desktop /note page commonly triggers captcha from server-side
requests. The mobile share page still exposes the SSR router payload for many
public posts, including video streams, images, and audio/live-photo references.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

logger = logging.getLogger("savextube.douyin_note")

ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]

NOTE_ID_RE = re.compile(r"/(?:share/)?note/(\d+)", re.IGNORECASE)
VIDEO_ID_RE = re.compile(r"/(?:share/)?video/(\d+)", re.IGNORECASE)
ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", re.DOTALL)
BAD_CONTENT_TYPES = ("text/html", "application/json")
MAX_STEM_BYTES = 150


class DouyinNoteError(RuntimeError):
    pass


def is_douyin_note_url(url: str) -> bool:
    return bool(NOTE_ID_RE.search(urlparse(url).path))


def is_douyin_aweme_url(url: str) -> bool:
    path = urlparse(url).path
    return bool(NOTE_ID_RE.search(path) or VIDEO_ID_RE.search(path))


def extract_douyin_note_id(url: str) -> Optional[str]:
    match = NOTE_ID_RE.search(urlparse(url).path)
    return match.group(1) if match else None


def extract_douyin_aweme_id(url: str) -> Optional[str]:
    path = urlparse(url).path
    match = NOTE_ID_RE.search(path) or VIDEO_ID_RE.search(path)
    return match.group(1) if match else None


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").strip()


def _safe_stem(value: str, fallback: str = "douyin_note", max_bytes: int = MAX_STEM_BYTES) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return _truncate_utf8(cleaned, max_bytes) or fallback


def _iter_dict_values(data: Any) -> Iterable[Any]:
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _iter_dict_values(value)
    elif isinstance(data, list):
        for value in data:
            yield from _iter_dict_values(value)


def _url_list(addr: Any) -> List[str]:
    urls: List[str] = []
    if not isinstance(addr, dict):
        return urls
    uri = addr.get("uri")
    if isinstance(uri, str) and uri.startswith(("http://", "https://")):
        urls.append(uri)
    for item in addr.get("url_list") or []:
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            urls.append(item)
    return _dedupe_strings(urls)


def _dedupe_strings(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _suffix_from_content_type(content_type: str, fallback: str) -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    known = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/mpeg": ".mp3",
    }
    if mime in known:
        return known[mime]
    guessed = mimetypes.guess_extension(mime) if mime else ""
    return guessed or fallback


def _ffconcat_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


class DouyinNoteDownloader:
    def __init__(self, proxy: str = "", cookie_header: str = ""):
        self.proxy = proxy.strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://m.douyin.com/",
            }
        )
        if cookie_header:
            self.session.headers["Cookie"] = cookie_header

    def download_note(
        self,
        url: str,
        output_dir: str,
        progress_callback: ProgressCallback = None,
    ) -> Dict[str, Any]:
        return self.download_aweme(url, output_dir, progress_callback=progress_callback, prefer_kind="note")

    def download_aweme(
        self,
        url: str,
        output_dir: str,
        progress_callback: ProgressCallback = None,
        prefer_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        aweme_id = extract_douyin_aweme_id(url)
        if not aweme_id:
            return {"success": False, "error": "not a Douyin video/note URL", "platform": "douyin", "url": url}
        kind = prefer_kind or ("note" if is_douyin_note_url(url) else "video")

        try:
            item = self.fetch_aweme_item(aweme_id, kind)
        except Exception as exc:
            logger.exception("douyin mobile share parse failed")
            return {"success": False, "error": str(exc), "platform": "douyin", "url": url}

        title = _safe_stem(str(item.get("desc") or f"抖音 {aweme_id}"), f"douyin_{aweme_id}")
        note_dir = Path(output_dir) / f"{title} [{aweme_id}]"
        note_dir.mkdir(parents=True, exist_ok=True)

        if progress_callback:
            progress_callback({"status": "downloading", "filename": f"{title} - 解析抖音素材"})

        image_files = self._download_images(item, note_dir, progress_callback)
        media_file, has_video, has_audio, sidecar_audio = self._download_best_media(item, note_dir, progress_callback)

        output_files: List[Path] = []
        if media_file and has_video:
            if not has_audio and sidecar_audio:
                video_file = note_dir / f"{title} [{aweme_id}].mp4"
                try:
                    output_files.append(self._merge_video_audio(media_file, sidecar_audio, video_file))
                    self._cleanup_files([media_file, sidecar_audio, *image_files])
                except Exception as exc:
                    logger.warning("failed to merge douyin live photo audio, sending video only: %s", exc)
                    output_files.append(media_file)
                    self._cleanup_files([sidecar_audio, *image_files])
            else:
                output_files.append(self._move_to_final_media(media_file, note_dir, title, aweme_id))
                self._cleanup_files(image_files)
        elif media_file and has_audio and image_files:
            video_file = note_dir / f"{title} [{aweme_id}].mp4"
            try:
                output_files.append(self._images_to_audio_video(image_files, media_file, video_file))
                self._cleanup_files([media_file, *image_files])
            except Exception as exc:
                logger.warning("failed to create douyin note video, falling back to images: %s", exc)
                output_files.extend(image_files)
        elif image_files:
            output_files.extend(image_files)

        files = [
            {
                "path": str(path),
                "full_path": str(path),
                "filename": path.name,
                "download_path": str(path.parent),
            }
            for path in output_files
            if path.exists() and path.stat().st_size > 0
        ]

        if progress_callback:
            progress_callback({"status": "finished", "filename": title})

        return {
            "success": bool(files),
            "title": title,
            "platform": "douyin",
            "url": url,
            "download_path": str(note_dir),
            "files": files,
            "full_path": files[0]["full_path"] if files else "",
            "error": "" if files else "抖音图文解析成功，但没有可下载的图片、音频或 Live Photo 素材",
        }

    def fetch_note_item(self, note_id: str) -> Dict[str, Any]:
        return self.fetch_aweme_item(note_id, "note")

    def fetch_aweme_item(self, aweme_id: str, kind: str = "video") -> Dict[str, Any]:
        errors: List[str] = []
        for page_url in self._share_page_candidates(aweme_id, kind):
            try:
                html = self._get_text(page_url)
                return self.parse_router_data(html, aweme_id)
            except Exception as exc:
                errors.append(f"{page_url}: {exc}")
                continue
        raise DouyinNoteError("无法解析抖音分享页；可能触发验证码/风控，或链接不是公开内容。详情：" + " | ".join(errors))

    def parse_router_data(self, html: str, aweme_id: str) -> Dict[str, Any]:
        match = ROUTER_DATA_RE.search(html)
        if not match:
            if "验证码中间页" in html or "captcha" in html.lower():
                raise DouyinNoteError("抖音返回验证码/风控页面，需要刷新 cookies 或稍后重试")
            raise DouyinNoteError("页面里没有找到抖音图文数据")

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise DouyinNoteError("抖音图文数据 JSON 解析失败") from exc

        for node in _iter_dict_values(data):
            video_info = node.get("videoInfoRes")
            if not isinstance(video_info, dict):
                continue
            for item in video_info.get("item_list") or []:
                if isinstance(item, dict) and str(item.get("aweme_id") or "") == str(aweme_id):
                    return item

        raise DouyinNoteError("页面数据里没有匹配的抖音条目")

    def _share_page_candidates(self, aweme_id: str, kind: str) -> Sequence[str]:
        kind = "note" if kind == "note" else "video"
        return (
            f"https://m.douyin.com/share/{kind}/{aweme_id}",
            f"https://www.iesdouyin.com/share/{kind}/{aweme_id}/",
            f"https://www.douyin.com/{kind}/{aweme_id}",
        )

    def _get_text(self, url: str) -> str:
        response = self.session.get(url, timeout=25, proxies=self._proxies(), allow_redirects=True)
        response.raise_for_status()
        return response.text

    def _download_images(
        self,
        item: Dict[str, Any],
        note_dir: Path,
        progress_callback: ProgressCallback,
    ) -> List[Path]:
        files: List[Path] = []
        images = item.get("images") or []
        for index, image in enumerate(images, start=1):
            candidates = self._image_candidates(image)
            if not candidates:
                continue
            if progress_callback:
                progress_callback({"status": "downloading", "filename": f"图片 {index}/{len(images)}"})
            path = self._download_first_success(candidates, note_dir / f"{index:02d}", ".jpg", "image")
            if path:
                files.append(path)
        return files

    def _image_candidates(self, image: Any) -> List[str]:
        if not isinstance(image, dict):
            return []
        primary_urls = [url for url in image.get("url_list") or [] if isinstance(url, str)]
        # download_url_list is normally watermarked; keep it only as a final fallback.
        fallback_urls = [url for url in image.get("download_url_list") or [] if isinstance(url, str)]

        def prefer_jpg(urls: Sequence[str]) -> List[str]:
            jpg_urls = [url for url in urls if ".jpeg" in url.lower() or ".jpg" in url.lower()]
            other_urls = [url for url in urls if url not in jpg_urls]
            return jpg_urls + other_urls

        return _dedupe_strings(prefer_jpg(primary_urls) + prefer_jpg(fallback_urls))

    def _download_best_media(
        self,
        item: Dict[str, Any],
        note_dir: Path,
        progress_callback: ProgressCallback,
    ) -> Tuple[Optional[Path], bool, bool, Optional[Path]]:
        best_video: Optional[Path] = None
        best_video_has_audio = False
        best_audio: Optional[Path] = None

        for index, url in enumerate(self._media_candidates(item), start=1):
            if progress_callback:
                progress_callback({"status": "downloading", "filename": f"音频/Live Photo 素材 {index}"})
            path = self._download_first_success([url], note_dir / f"live_photo_media_{index:02d}", ".mp4", "media")
            if not path:
                continue
            has_video, has_audio = self._probe_streams(path)
            if has_video:
                if not best_video:
                    best_video = path
                    best_video_has_audio = has_audio
                elif path != best_video:
                    path.unlink(missing_ok=True)
                if has_audio or best_audio:
                    return best_video, True, best_video_has_audio, best_audio
                continue
            if has_audio:
                if not best_audio:
                    best_audio = path
                elif path != best_audio:
                    path.unlink(missing_ok=True)
                if best_video:
                    return best_video, True, best_video_has_audio, best_audio
                continue
            path.unlink(missing_ok=True)
        if best_video:
            return best_video, True, best_video_has_audio, best_audio
        if best_audio:
            return best_audio, False, True, None
        return None, False, False, None

    def _media_candidates(self, item: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []
        video = item.get("video") or {}
        music = item.get("music") or {}

        if isinstance(video, dict):
            for key in ("play_addr", "download_addr", "play_addr_h264"):
                candidates.extend(_url_list(video.get(key)))

        if isinstance(music, dict):
            for key in ("play_url", "play_addr"):
                candidates.extend(_url_list(music.get(key)))

        for image in item.get("images") or []:
            if not isinstance(image, dict):
                continue
            for key in ("video", "live_photo", "dynamic_video", "clip"):
                candidates.extend(_url_list(image.get(key)))

        return _dedupe_strings(self._douyin_video_url_variants(candidates))

    def _douyin_video_url_variants(self, urls: Iterable[str]) -> List[str]:
        variants: List[str] = []
        for url in urls:
            parsed = urlparse(url)
            if "/aweme/v1/playwm/" not in parsed.path:
                variants.append(url)
                continue

            no_watermark = url.replace("/aweme/v1/playwm/", "/aweme/v1/play/")
            variants.extend(
                [
                    self._replace_query_value(no_watermark, "ratio", "1080p"),
                    self._replace_query_value(no_watermark, "ratio", "720p"),
                    no_watermark,
                    url,
                ]
            )
        return variants

    def _replace_query_value(self, url: str, key: str, value: str) -> str:
        parsed = urlparse(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        changed = False
        result = []
        for item_key, item_value in pairs:
            if item_key == key:
                result.append((item_key, value))
                changed = True
            else:
                result.append((item_key, item_value))
        if not changed:
            result.append((key, value))
        return urlunparse(parsed._replace(query=urlencode(result)))

    def _download_first_success(
        self,
        urls: Iterable[str],
        dest_prefix: Path,
        fallback_suffix: str,
        expected_type: str,
    ) -> Optional[Path]:
        for url in urls:
            try:
                path = self._download_url(url, dest_prefix, fallback_suffix, expected_type)
                if path:
                    return path
            except Exception as exc:
                logger.info("download candidate failed: %s -> %s", url, exc)
        return None

    def _download_url(self, url: str, dest_prefix: Path, fallback_suffix: str, expected_type: str) -> Optional[Path]:
        response = self.session.get(url, timeout=60, proxies=self._proxies(), stream=True, allow_redirects=True)
        if response.status_code >= 400:
            return None

        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if any(content_type.startswith(bad) for bad in BAD_CONTENT_TYPES):
            return None
        if expected_type == "image" and content_type and not content_type.startswith("image/"):
            return None

        suffix = _suffix_from_content_type(content_type, self._suffix_from_url(url, fallback_suffix))
        dest = dest_prefix.with_suffix(suffix)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.unlink(missing_ok=True)

        size = 0
        with tmp.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                size += len(chunk)

        if size <= 0:
            tmp.unlink(missing_ok=True)
            return None
        tmp.replace(dest)
        return dest

    def _images_to_audio_video(self, images: Sequence[Path], audio: Path, output: Path) -> Path:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise DouyinNoteError("ffmpeg not found; cannot convert Douyin note to video")

        output.unlink(missing_ok=True)
        audio_duration = self._duration_seconds(audio) or max(3.0, len(images) * 3.0)

        if len(images) == 1:
            cmd = [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-loop",
                "1",
                "-framerate",
                "30",
                "-i",
                str(images[0]),
                "-i",
                str(audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-tune",
                "stillimage",
                "-crf",
                os.getenv("DOUYIN_NOTE_VIDEO_CRF", "20"),
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30,format=yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(output),
            ]
        else:
            concat_file = output.with_suffix(".ffconcat")
            per_image = max(1.0, audio_duration / len(images))
            concat_lines = ["ffconcat version 1.0"]
            for image in images:
                concat_lines.append(f"file {_ffconcat_quote(image)}")
                concat_lines.append(f"duration {per_image:.3f}")
            concat_lines.append(f"file {_ffconcat_quote(images[-1])}")
            concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
            cmd = [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-i",
                str(audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                os.getenv("DOUYIN_NOTE_VIDEO_CRF", "20"),
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30,format=yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(output),
            ]

        start = time.time()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
        if result.returncode != 0 or not output.exists() or output.stat().st_size <= 0:
            output.unlink(missing_ok=True)
            raise DouyinNoteError(f"ffmpeg convert failed: {result.stderr.strip()}")
        logger.info("created douyin note video in %.1fs: %s", time.time() - start, output)
        return output

    def _merge_video_audio(self, video: Path, audio: Path, output: Path) -> Path:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise DouyinNoteError("ffmpeg not found; cannot merge Douyin live photo audio")

        output.unlink(missing_ok=True)
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
        if result.returncode != 0 or not output.exists() or output.stat().st_size <= 0:
            output.unlink(missing_ok=True)
            raise DouyinNoteError(f"ffmpeg merge failed: {result.stderr.strip()}")
        return output

    def _cleanup_files(self, paths: Iterable[Path]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except Exception as exc:
                logger.info("failed to remove douyin note intermediate %s: %s", path, exc)

    def _move_to_final_media(self, path: Path, note_dir: Path, title: str, aweme_id: str) -> Path:
        suffix = path.suffix.lower() if path.suffix else ".mp4"
        dest = note_dir / f"{title} [{aweme_id}]{suffix}"
        if path.resolve() == dest.resolve():
            return path
        dest.unlink(missing_ok=True)
        path.replace(dest)
        return dest

    def _probe_streams(self, path: Path) -> Tuple[bool, bool]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            mime = mimetypes.guess_type(str(path))[0] or ""
            return mime.startswith("video/"), mime.startswith("audio/")
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_streams",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                return False, False
            data = json.loads(result.stdout or "{}")
        except Exception as exc:
            logger.info("ffprobe failed for %s: %s", path, exc)
            return False, False
        streams = data.get("streams") or []
        return (
            any(stream.get("codec_type") == "video" for stream in streams),
            any(stream.get("codec_type") == "audio" for stream in streams),
        )

    def _duration_seconds(self, path: Path) -> float:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return 0.0
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return max(0.0, float((result.stdout or "0").strip() or "0"))
        except Exception:
            return 0.0

    def _suffix_from_url(self, url: str, fallback: str) -> str:
        path = urlparse(url).path
        suffix = Path(path).suffix.lower()
        return suffix if suffix and len(suffix) <= 8 else fallback

    def _proxies(self) -> Optional[Dict[str, str]]:
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None
