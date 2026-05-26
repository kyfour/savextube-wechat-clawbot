#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lightweight Xiaohongshu fallback downloader.

This is only used when yt-dlp cannot handle a Xiaohongshu note directly.
It keeps logging quiet by default and reports download progress using the same
dict shape as yt-dlp progress hooks.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

logger = logging.getLogger("savextube.xiaohongshu")

ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]
MAX_FILENAME_BYTES = 150

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.xiaohongshu.com/",
}


class XiaohongshuDownloader:
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def _expand_short_url(self, url: str) -> Optional[str]:
        cleaned = url.strip().split()[0]
        if "xhslink.com" not in cleaned:
            return cleaned

        try:
            response = self.session.get(cleaned, allow_redirects=True, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("xiaohongshu short URL expansion failed: %s", exc)
            return None

        expanded = response.url or cleaned
        if expanded.rstrip("/") in {"https://www.xiaohongshu.com", "https://www.xiaohongshu.com/explore"}:
            logger.warning("xiaohongshu short URL resolved to a generic page: %s", expanded)
            return None
        return expanded

    def extract_note_id(self, url: str) -> Optional[str]:
        expanded_url = self._expand_short_url(url)
        if not expanded_url:
            return None

        patterns = [
            r"/explore/([^/?#]+)",
            r"/discovery/item/([^/?#]+)",
            r"/item/([^/?#]+)",
            r"(?:[?&])noteId=([^&#]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, expanded_url)
            if match:
                return match.group(1)
        logger.warning("could not extract Xiaohongshu note id from URL: %s", expanded_url)
        return None

    def get_page_data(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            response = self.session.get(url, timeout=20)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("xiaohongshu page request failed: %s", exc)
            return None

        patterns = [
            r"window\.__INITIAL_STATE__\s*=\s*({.+?})</script>",
            r"__INITIAL_STATE__\s*=\s*({.+?})</script>",
        ]
        for pattern in patterns:
            match = re.search(pattern, response.text, re.DOTALL)
            if not match:
                continue
            json_str = match.group(1).strip().replace("undefined", "null")
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as exc:
                logger.debug("xiaohongshu initial state parse failed: %s", exc)
        logger.warning("xiaohongshu page did not expose parseable initial state")
        return None

    def extract_note_info(self, data: Dict[str, Any], note_id: str) -> Optional[Dict[str, Any]]:
        note_root = data.get("note") if isinstance(data.get("note"), dict) else {}
        detail_map = note_root.get("noteDetailMap") if isinstance(note_root.get("noteDetailMap"), dict) else {}

        direct = detail_map.get(note_id)
        if isinstance(direct, dict):
            return direct.get("note") if isinstance(direct.get("note"), dict) else direct

        for value in detail_map.values():
            if not isinstance(value, dict):
                continue
            note = value.get("note") if isinstance(value.get("note"), dict) else value
            if str(note.get("id") or note.get("noteId") or note.get("note_id")) == str(note_id):
                return note

        feed_root = data.get("feed") if isinstance(data.get("feed"), dict) else {}
        feeds = feed_root.get("feeds") if isinstance(feed_root.get("feeds"), list) else []
        for item in feeds:
            if not isinstance(item, dict):
                continue
            note = item.get("noteCard") if isinstance(item.get("noteCard"), dict) else item
            item_id = item.get("id") or item.get("noteId") or note.get("id") or note.get("noteId")
            if str(item_id) == str(note_id):
                return note

        logger.warning("xiaohongshu note id not found in page data: %s", note_id)
        return None

    def generate_image_urls(self, note: Dict[str, Any]) -> List[str]:
        urls: List[str] = []
        for item in note.get("imageList") or []:
            if not isinstance(item, dict):
                continue
            url_default = item.get("urlDefault") or ""
            match = re.search(r"http://sns-webpic-qc\.xhscdn\.com/\d+/[0-9a-z]+/(\S+)!", url_default)
            if match:
                urls.append(f"https://ci.xiaohongshu.com/{match.group(1)}?imageView2/format/png")
                continue
            for key in ("urlDefault", "url", "picUrl"):
                if item.get(key):
                    urls.append(str(item[key]))
                    break
        return urls

    def generate_video_url(self, note: Dict[str, Any]) -> List[str]:
        video = note.get("video") if isinstance(note.get("video"), dict) else {}
        consumer = video.get("consumer") if isinstance(video.get("consumer"), dict) else {}
        for key in ("originVideoKey", "videoKey", "masterUrl"):
            value = consumer.get(key) or video.get(key)
            if value:
                value = str(value)
                if value.startswith("http"):
                    return [value]
                return [f"https://sns-video-bd.xhscdn.com/{value}"]
        return []

    def download_file(self, url: str, filepath: str, retries: int = 3, progress_callback: ProgressCallback = None) -> bool:
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        for attempt in range(retries):
            try:
                with self.session.get(url, timeout=30, stream=True) as response:
                    response.raise_for_status()
                    total_size = int(response.headers.get("content-length") or 0)
                    downloaded_size = 0
                    start_time = time.time()
                    last_emit = 0.0

                    with path.open("wb") as f:
                        for chunk in response.iter_content(chunk_size=1024 * 256):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            now = time.time()
                            if progress_callback and (now - last_emit >= 1.0 or downloaded_size == total_size):
                                elapsed = max(now - start_time, 0.001)
                                self._emit_progress(progress_callback, path.name, downloaded_size, total_size, downloaded_size / elapsed)
                                last_emit = now

                if progress_callback:
                    size = path.stat().st_size
                    self._emit_progress(progress_callback, path.name, size, size, 0, status="finished")
                logger.info("xiaohongshu file downloaded: %s", path)
                return True
            except requests.RequestException as exc:
                logger.warning("xiaohongshu file download failed (%s/%s): %s", attempt + 1, retries, exc)
                if attempt < retries - 1:
                    time.sleep(2)
            except OSError as exc:
                logger.warning("xiaohongshu file write failed: %s", exc)
                return False
        return False

    def clean_filename(self, filename: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", filename or "").strip()
        encoded = cleaned.encode("utf-8")
        if len(encoded) > MAX_FILENAME_BYTES:
            cleaned = encoded[:MAX_FILENAME_BYTES].decode("utf-8", errors="ignore").strip()
        return cleaned or "untitled"

    def download_note(self, url: str, download_dir: str = "./downloads", progress_callback: ProgressCallback = None) -> Dict[str, Any]:
        try:
            expanded_url = self._expand_short_url(url)
            if not expanded_url:
                return {"success": False, "error": "无法展开小红书链接"}

            note_id = self.extract_note_id(expanded_url)
            if not note_id:
                return {"success": False, "error": "无法提取笔记ID"}

            data = self.get_page_data(expanded_url)
            if not data:
                return {"success": False, "error": "获取页面数据失败"}

            note = self.extract_note_info(data, note_id)
            if not note:
                return {"success": False, "error": "提取笔记信息失败"}

            title = self.clean_filename(str(note.get("displayTitle") or note.get("title") or note.get("desc") or "untitled"))
            author = str((note.get("user") or {}).get("nickname") or "未知作者")
            note_type = str(note.get("type") or "normal")
            media_type = "video" if note_type == "video" else "image"
            urls = self.generate_video_url(note) if media_type == "video" else self.generate_image_urls(note)
            if not urls:
                return {"success": False, "error": "未找到可下载媒体"}

            base_dir = Path(download_dir) / f"{note_id}_{title}"
            files = []
            total_size = 0
            for idx, media_url in enumerate(urls, start=1):
                ext = ".mp4" if media_type == "video" else ".png"
                filename = f"{title}{ext}" if len(urls) == 1 else f"{title}_{idx}{ext}"
                file_path = base_dir / filename
                if self.download_file(media_url, str(file_path), progress_callback=progress_callback):
                    file_size = file_path.stat().st_size
                    total_size += file_size
                    files.append({"path": str(file_path), "size": file_size, "type": media_type})

            return {
                "success": bool(files),
                "title": title,
                "author": author,
                "note_id": note_id,
                "media_type": media_type,
                "files": files,
                "total_size": total_size,
                "save_dir": str(base_dir),
                "error": "" if files else "下载媒体失败",
            }
        except Exception as exc:
            logger.exception("xiaohongshu note download failed")
            return {"success": False, "error": str(exc)}

    def _emit_progress(
        self,
        progress_callback: Callable[[Dict[str, Any]], None],
        filename: str,
        downloaded_size: int,
        total_size: int,
        speed: float,
        status: str = "downloading",
    ) -> None:
        try:
            progress_callback(
                {
                    "status": status,
                    "filename": filename,
                    "downloaded_bytes": downloaded_size,
                    "total_bytes": total_size,
                    "speed": speed,
                }
            )
        except Exception as exc:
            logger.debug("xiaohongshu progress callback failed: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Xiaohongshu fallback downloader")
    parser.add_argument("urls", nargs="*", help="Xiaohongshu note URLs")
    parser.add_argument("-d", "--dir", default="./downloads", help="Download directory")
    parser.add_argument("-c", "--cookie", help="Raw Xiaohongshu Cookie header")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")
    downloader = XiaohongshuDownloader()
    if args.cookie:
        downloader.session.headers["Cookie"] = args.cookie

    if not args.urls:
        parser.error("at least one URL is required")

    success_count = 0
    for url in args.urls:
        result = downloader.download_note(url, args.dir)
        if result.get("success"):
            success_count += 1
            logger.info("downloaded: %s", result.get("title") or url)
        else:
            logger.error("failed: %s", result.get("error") or url)
    logger.info("done: %s/%s succeeded", success_count, len(args.urls))


if __name__ == "__main__":
    main()
