#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Downloader core for the WeChat ClawBot runtime.

The public build intentionally keeps only the platforms needed by this bot:
    Douyin, Kuaishou, Weibo, Toutiao, Xiaohongshu, Bilibili, and WeChat Channels.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests
import yt_dlp

from douyin_note_downloader import is_douyin_aweme_url

logger = logging.getLogger("savextube.wechat_downloader")

ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]


SUPPORTED_PLATFORMS = (
    "douyin",
    "kuaishou",
    "weibo",
    "toutiao",
    "xiaohongshu",
    "bilibili",
    "wechat_channels",
)


COOKIE_ENV = {
    "douyin": "DOUYIN_COOKIES",
    "kuaishou": "KUAISHOU_COOKIES",
    "weibo": "WEIBO_COOKIES",
    "toutiao": "TOUTIAO_COOKIES",
    "xiaohongshu": "XIAOHONGSHU_COOKIES",
    "bilibili": "BILIBILI_COOKIES",
    "wechat_channels": "WECHAT_CHANNELS_YUANBAO_COOKIE_FILE",
}


COOKIE_FILES = {
    "douyin": "douyin_cookies.txt",
    "kuaishou": "kuaishou_cookies.txt",
    "weibo": "weibo_cookies.txt",
    "toutiao": "toutiao_cookies.txt",
    "xiaohongshu": "xiaohongshu_cookies.txt",
    "bilibili": "bilibili_cookies.txt",
    "wechat_channels": "wechat_channels_yuanbao_cookies.txt",
}


URL_PATTERN = re.compile(r"https?://[^\s<>'\"，。；、)）\]]+", re.IGNORECASE)
MAX_STEM_BYTES = 150


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").strip()


def _safe_stem(value: str, fallback: str = "video", max_bytes: int = MAX_STEM_BYTES) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return _truncate_utf8(cleaned, max_bytes) or fallback


def _existing_cookiefile(path: Path) -> Optional[str]:
    try:
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            return str(path)
    except OSError:
        return None
    return None


def _cookie_header_from_netscape(path: Path) -> str:
    if not path.exists():
        return ""
    pairs: List[str] = []
    raw_lines: List[str] = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            raw_lines.append(line)
            fields = line.split("\t")
            if len(fields) >= 7:
                name = fields[5].strip()
                value = fields[6].strip()
                if name and value:
                    pairs.append(f"{name}={value}")
    except OSError:
        return ""
    if pairs:
        return "; ".join(pairs)
    raw_cookie = "; ".join(line.rstrip(";") for line in raw_lines if "=" in line)
    return raw_cookie.strip("; ")


class WeChatVideoDownloader:
    def __init__(self, download_path: str, cookies_path: str = "/app/cookies", proxy: str = ""):
        self.download_path = Path(download_path).resolve()
        self.cookies_path = Path(cookies_path).resolve()
        self.proxy = proxy.strip()
        self.download_path.mkdir(parents=True, exist_ok=True)
        self.cookies_path.mkdir(parents=True, exist_ok=True)

        self.platform_dirs = {
            platform: self.download_path / platform
            for platform in SUPPORTED_PLATFORMS
        }
        for directory in self.platform_dirs.values():
            directory.mkdir(parents=True, exist_ok=True)

        self.cookie_files = {
            platform: Path(os.getenv(COOKIE_ENV[platform], str(self.cookies_path / COOKIE_FILES[platform]))).resolve()
            for platform in SUPPORTED_PLATFORMS
        }
        self.b_cookies_path = str(self.cookie_files["bilibili"])

    def extract_urls_from_text(self, text: str) -> List[str]:
        normalized = text.replace("tp://", "http://").replace("ttp://", "http://")
        urls = [match.group(0).rstrip(".,;，。；") for match in URL_PATTERN.finditer(normalized)]
        if urls:
            return urls

        bare_match = re.search(
            r"((?:v\.douyin|www\.douyin|www\.iesdouyin|www\.kuaishou|v\.kuaishou|"
            r"m\.weibo|weibo|www\.toutiao|m\.toutiao|xhslink|www\.xiaohongshu|"
            r"b23|www\.bilibili|m\.bilibili)\.[^\s<>'\"，。；、)）\]]+|"
            r"weixin\.qq\.com/sph/[^\s<>'\"，。；、)）\]]+)",
            normalized,
            re.IGNORECASE,
        )
        return [f"https://{bare_match.group(1).rstrip('.,;，。；')}"] if bare_match else []

    def get_platform_name(self, url: str) -> str:
        host = urlparse(url).netloc.lower()
        if "douyin.com" in host or "iesdouyin.com" in host:
            return "douyin"
        if "kuaishou.com" in host or "chenzhongtech.com" in host:
            return "kuaishou"
        if "weibo.com" in host or "weibo.cn" in host:
            return "weibo"
        if "toutiao.com" in host or "ixigua.com" in host:
            return "toutiao"
        if "xiaohongshu.com" in host or "xhslink.com" in host:
            return "xiaohongshu"
        if "bilibili.com" in host or "b23.tv" in host or "bili2233.cn" in host:
            return "bilibili"
        if host == "finder.video.qq.com" or (host == "weixin.qq.com" and "/sph/" in urlparse(url).path.lower()):
            return "wechat_channels"
        return "unknown"

    async def download_video(
        self,
        url: str,
        progress_callback: ProgressCallback = None,
        auto_playlist: bool = False,
        *_args: Any,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        platform = self.get_platform_name(url)
        if platform not in SUPPORTED_PLATFORMS:
            return {"success": False, "error": f"unsupported platform: {platform}"}

        normalized_url = await asyncio.to_thread(self.normalize_url, url, platform)
        if platform == "douyin" and is_douyin_aweme_url(normalized_url):
            douyin_result = await self._try_douyin_direct(normalized_url, progress_callback)
            if douyin_result.get("success"):
                return douyin_result
            logger.info("douyin mobile share downloader failed, falling back to yt-dlp: %s", douyin_result.get("error"))

        if platform == "xiaohongshu":
            xhs_result = await self._try_xiaohongshu_direct(normalized_url, progress_callback)
            if xhs_result.get("success"):
                return xhs_result
            logger.info("xiaohongshu direct downloader failed, falling back to yt-dlp: %s", xhs_result.get("error"))

        if platform == "wechat_channels":
            return await self._try_wechat_channels_direct(normalized_url, progress_callback)

        return await asyncio.to_thread(
            self._download_with_ytdlp,
            normalized_url,
            platform,
            progress_callback,
            auto_playlist,
        )

    def normalize_url(self, url: str, platform: Optional[str] = None) -> str:
        platform = platform or self.get_platform_name(url)
        url = url.strip()
        if platform == "bilibili":
            url = self._normalize_bilibili_url(url)
        elif platform == "douyin":
            url = self._normalize_douyin_url(url)
        return url

    def _normalize_douyin_url(self, url: str) -> str:
        host = urlparse(url).netloc.lower()
        if host == "v.douyin.com" or host.endswith(".v.douyin.com"):
            return self._expand_redirect(url)
        return url

    def _normalize_bilibili_url(self, url: str) -> str:
        expanded = self._expand_redirect(url) if self._is_short_url(url) else url
        bv_match = re.search(r"(BV[0-9A-Za-z]{10})", expanded)
        if bv_match:
            return f"https://www.bilibili.com/video/{bv_match.group(1)}/"
        av_match = re.search(r"(?:video/)?av(\d+)", expanded, re.IGNORECASE)
        if av_match:
            return f"https://www.bilibili.com/video/av{av_match.group(1)}/"
        return expanded

    def _is_short_url(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return host in {"b23.tv", "bili2233.cn"} or host.endswith(".b23.tv")

    def _expand_redirect(self, url: str) -> str:
        try:
            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
            response = requests.get(
                url,
                allow_redirects=True,
                timeout=15,
                headers={"User-Agent": self._user_agent()},
                proxies=proxies,
            )
            return response.url or url
        except requests.RequestException as exc:
            logger.warning("redirect expansion failed for %s: %s", url, exc)
            return url

    def _platform_cookiefile(self, platform: str) -> Optional[str]:
        return _existing_cookiefile(self.cookie_files[platform])

    def _download_with_ytdlp(
        self,
        url: str,
        platform: str,
        progress_callback: ProgressCallback,
        auto_playlist: bool,
    ) -> Dict[str, Any]:
        output_dir = self.platform_dirs[platform]
        output_dir.mkdir(parents=True, exist_ok=True)
        start_time = time.time()
        downloaded_files: List[Path] = []

        ydl_opts = self._build_ytdlp_options(platform, output_dir, progress_callback, auto_playlist)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                downloaded_files.extend(self._files_from_info(ydl, info))
        except Exception as exc:
            logger.exception("yt-dlp download failed")
            return {"success": False, "error": str(exc), "platform": platform, "url": url}

        if not downloaded_files:
            downloaded_files.extend(self._recent_files(output_dir, start_time))

        title = self._info_title(info) if "info" in locals() else platform
        files = [
            {"path": str(path), "full_path": str(path), "filename": path.name, "download_path": str(path.parent)}
            for path in self._dedupe_existing(downloaded_files)
        ]
        return {
            "success": bool(files),
            "title": title,
            "platform": platform,
            "url": url,
            "download_path": str(output_dir),
            "files": files,
            "full_path": str(files[0]["full_path"]) if files else "",
            "error": "" if files else "download finished but no output file was found",
        }

    def _build_ytdlp_options(
        self,
        platform: str,
        output_dir: Path,
        progress_callback: ProgressCallback,
        auto_playlist: bool,
    ) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "outtmpl": str(output_dir / "%(title).180B [%(id)s].%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": not auto_playlist,
            "restrictfilenames": False,
            "windowsfilenames": True,
            "ignoreerrors": False,
            "quiet": True,
            "no_warnings": True,
            "logger": logger,
            "retries": 5,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "progress_hooks": [progress_callback] if progress_callback else [],
            "http_headers": self._headers_for_platform(platform),
            "format": self._format_selector(platform),
        }
        cookiefile = self._platform_cookiefile(platform)
        if cookiefile:
            opts["cookiefile"] = cookiefile
        if self.proxy:
            opts["proxy"] = self.proxy
        return opts

    def _headers_for_platform(self, platform: str) -> Dict[str, str]:
        headers = {
            "User-Agent": self._user_agent(),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        referers = {
            "douyin": "https://www.douyin.com/",
            "kuaishou": "https://www.kuaishou.com/",
            "weibo": "https://weibo.com/",
            "toutiao": "https://www.toutiao.com/",
            "xiaohongshu": "https://www.xiaohongshu.com/",
            "bilibili": "https://www.bilibili.com/",
            "wechat_channels": "https://channels.weixin.qq.com/",
        }
        headers["Referer"] = referers.get(platform, "")
        return headers

    def _format_selector(self, platform: str) -> str:
        if platform == "bilibili":
            return (
                "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[vcodec^=avc1]+bestaudio/"
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "best[ext=mp4]/best"
            )
        return "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/best[ext=mp4]/best"

    def _files_from_info(self, ydl: yt_dlp.YoutubeDL, info: Any) -> List[Path]:
        files: List[Path] = []
        for item in self._iter_info_items(info):
            for download in item.get("requested_downloads") or []:
                self._append_existing(files, download.get("filepath"))
                self._append_existing(files, download.get("filename"))
            self._append_existing(files, item.get("filepath"))
            self._append_existing(files, item.get("_filename"))
            try:
                self._append_existing(files, ydl.prepare_filename(item))
            except Exception:
                pass
        return files

    def _iter_info_items(self, info: Any) -> Iterable[Dict[str, Any]]:
        if not isinstance(info, dict):
            return []
        entries = info.get("entries")
        if entries:
            return [entry for entry in entries if isinstance(entry, dict)]
        return [info]

    def _append_existing(self, files: List[Path], value: Any) -> None:
        if not value:
            return
        path = Path(str(value))
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            files.append(path)
            return
        mp4_path = path.with_suffix(".mp4")
        if mp4_path.exists() and mp4_path.is_file() and mp4_path.stat().st_size > 0:
            files.append(mp4_path)

    def _recent_files(self, directory: Path, start_time: float) -> List[Path]:
        ignored_suffixes = {".part", ".ytdl", ".aria2", ".tmp"}
        result = []
        for path in directory.rglob("*"):
            try:
                if path.is_file() and path.suffix.lower() not in ignored_suffixes and path.stat().st_mtime >= start_time - 2:
                    result.append(path)
            except OSError:
                continue
        return result

    def _dedupe_existing(self, files: Iterable[Path]) -> List[Path]:
        seen = set()
        result = []
        for path in files:
            try:
                resolved = path.resolve()
                if resolved in seen or not resolved.exists() or resolved.stat().st_size <= 0:
                    continue
                seen.add(resolved)
                result.append(resolved)
            except OSError:
                continue
        result.sort(key=lambda item: item.stat().st_mtime)
        return result

    def _info_title(self, info: Any) -> str:
        if isinstance(info, dict):
            if info.get("title"):
                return _safe_stem(str(info["title"]))
            entries = info.get("entries")
            if entries:
                first = next((entry for entry in entries if isinstance(entry, dict) and entry.get("title")), None)
                if first:
                    return _safe_stem(str(first["title"]))
        return "下载完成"

    async def _try_xiaohongshu_direct(self, url: str, progress_callback: ProgressCallback) -> Dict[str, Any]:
        try:
            from xiaohongshu_downloader import XiaohongshuDownloader
        except ImportError as exc:
            return {"success": False, "error": str(exc)}

        output_dir = self.platform_dirs["xiaohongshu"]

        def run() -> Dict[str, Any]:
            downloader = XiaohongshuDownloader()
            cookiefile = self.cookie_files["xiaohongshu"]
            cookie_header = _cookie_header_from_netscape(cookiefile)
            if cookie_header:
                downloader.session.headers["Cookie"] = cookie_header
            return downloader.download_note(url, str(output_dir), progress_callback=progress_callback)

        result = await asyncio.to_thread(run)
        if result.get("success"):
            files = []
            for item in result.get("files") or []:
                path = item.get("path")
                if path:
                    files.append({
                        "path": path,
                        "full_path": path,
                        "filename": Path(path).name,
                        "download_path": str(Path(path).parent),
                    })
            result["files"] = files
            result["download_path"] = result.get("save_dir") or str(output_dir)
            if files:
                result["full_path"] = files[0]["full_path"]
            if progress_callback:
                progress_callback({"status": "finished", "filename": result.get("title") or "小红书"})
        return result

    async def _try_douyin_direct(self, url: str, progress_callback: ProgressCallback) -> Dict[str, Any]:
        try:
            from douyin_note_downloader import DouyinNoteDownloader
        except ImportError as exc:
            return {"success": False, "error": str(exc)}

        output_dir = self.platform_dirs["douyin"]

        def run() -> Dict[str, Any]:
            cookiefile = self.cookie_files["douyin"]
            downloader = DouyinNoteDownloader(
                proxy=self.proxy,
                cookie_header=_cookie_header_from_netscape(cookiefile),
            )
            return downloader.download_aweme(url, str(output_dir), progress_callback=progress_callback)

        return await asyncio.to_thread(run)

    async def _try_wechat_channels_direct(self, url: str, progress_callback: ProgressCallback) -> Dict[str, Any]:
        try:
            from wechat_channels_downloader import WeChatChannelsDownloader
        except ImportError as exc:
            return {"success": False, "error": str(exc), "platform": "wechat_channels", "url": url}

        output_dir = self.platform_dirs["wechat_channels"]

        def run() -> Dict[str, Any]:
            cookiefile = self.cookie_files["wechat_channels"]
            downloader = WeChatChannelsDownloader(
                proxy=self.proxy,
                yuanbao_cookie=_cookie_header_from_netscape(cookiefile),
            )
            return downloader.download(url, str(output_dir), progress_callback=progress_callback)

        return await asyncio.to_thread(run)

    def _user_agent(self) -> str:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )
