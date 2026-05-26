#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WeChat Channels (视频号) share-link downloader.

The NAS-friendly route is:
  https://weixin.qq.com/sph/... -> Tencent Yuanbao parse API -> Tencent
  Channels preview API -> finder.video.qq.com MP4 URL.

No third-party resolver is used by default. A custom resolver URL can be
configured explicitly for a self-hosted service, but the default path only
talks to Tencent endpoints and requires the user's own Yuanbao Web cookie.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger("savextube.wechat_channels")

ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]

YUANBAO_PARSE_URL = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
CHANNELS_FEED_INFO_URL = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
SPH_URL_PATTERN = re.compile(r"https?://weixin\.qq\.com/sph/[A-Za-z0-9_-]+[^\s<>'\"，。；、)）\]]*", re.IGNORECASE)
MAX_STEM_BYTES = 150


def is_wechat_channels_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return (host == "weixin.qq.com" and "/sph/" in urlparse(url).path.lower()) or host == "finder.video.qq.com"


def extract_wechat_channels_urls(text: str) -> List[str]:
    normalized = text.replace("tp://", "http://").replace("ttp://", "http://")
    urls = [match.group(0).rstrip(".,;，。；") for match in SPH_URL_PATTERN.finditer(normalized)]
    if urls:
        return urls
    bare = re.search(r"(weixin\.qq\.com/sph/[A-Za-z0-9_-]+[^\s<>'\"，。；、)）\]]*)", normalized, re.IGNORECASE)
    return [f"https://{bare.group(1).rstrip('.,;，。；')}"] if bare else []


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").strip()


def _safe_stem(value: str, fallback: str = "wechat_channels", max_bytes: int = MAX_STEM_BYTES) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return _truncate_utf8(cleaned, max_bytes) or fallback


def _suffix_from_response(url: str, content_type: str) -> str:
    suffix = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if suffix in {".mp4", ".m4v", ".mov"}:
        return ".mp4"
    path_suffix = Path(urlparse(url).path).suffix.lower()
    if path_suffix in {".mp4", ".m4v", ".mov"}:
        return ".mp4"
    return ".mp4"


class WeChatChannelsDownloader:
    def __init__(self, resolver_url: str = "", proxy: str = "", yuanbao_cookie: str = ""):
        self.resolver_url = (resolver_url or os.getenv("WECHAT_CHANNELS_RESOLVER_URL") or "").strip()
        self.proxy = proxy.strip()
        self.yuanbao_cookie = (yuanbao_cookie or os.getenv("WECHAT_CHANNELS_YUANBAO_COOKIE") or "").strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )

    def download(
        self,
        url: str,
        output_dir: str,
        progress_callback: ProgressCallback = None,
    ) -> Dict[str, Any]:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)

        try:
            profile = self.resolve(url)
            video_url = profile["video_url"]
            title = _safe_stem(profile.get("title") or "wechat_channels")
            author = _safe_stem(profile.get("author") or "", "")
            stem = _safe_stem(f"{author} - {title}" if author else title)
            file_path = self._download_url(video_url, output_path, stem, progress_callback)
        except Exception as exc:
            logger.exception("wechat channels download failed")
            return {"success": False, "error": str(exc), "platform": "wechat_channels", "url": url}

        file_info = {
            "path": str(file_path),
            "full_path": str(file_path),
            "filename": file_path.name,
            "download_path": str(file_path.parent),
        }
        return {
            "success": True,
            "title": profile.get("title") or file_path.stem,
            "platform": "wechat_channels",
            "url": url,
            "download_path": str(file_path.parent),
            "full_path": str(file_path),
            "files": [file_info],
        }

    def resolve(self, url: str) -> Dict[str, str]:
        host = urlparse(url).netloc.lower()
        if host == "finder.video.qq.com":
            return {"video_url": url, "title": "wechat_channels", "author": ""}

        if "weixin.qq.com" not in host or "/sph/" not in urlparse(url).path.lower():
            raise ValueError("不是微信视频号 sph 分享链接")
        if self.resolver_url:
            return self._resolve_with_custom_resolver(url)

        return self._resolve_with_tencent(url)

    def _resolve_with_custom_resolver(self, url: str) -> Dict[str, str]:
        response = self.session.post(
            self.resolver_url,
            json={"url": url},
            timeout=30,
            proxies=self._proxies(),
        )
        if response.status_code >= 400:
            raise ValueError(f"视频号解析接口返回 HTTP {response.status_code}: {response.text[:300]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"视频号解析接口返回非 JSON: {response.text[:300]}") from exc

        feed_payload = self._find_feed_payload(payload)
        if not feed_payload:
            raise ValueError("视频号解析接口未返回 feedInfo")

        feed_info = feed_payload.get("feedInfo") or {}
        author_info = feed_payload.get("authorInfo") or {}
        video_url = self._video_url_from_feed(feed_info)
        if not video_url:
            raise ValueError("视频号解析结果里没有可下载视频 URL")

        return {
            "video_url": video_url,
            "title": str(feed_info.get("description") or ""),
            "author": str(author_info.get("nickname") or ""),
        }

    def _resolve_with_tencent(self, url: str) -> Dict[str, str]:
        cookie = self.yuanbao_cookie
        if not cookie:
            raise ValueError(
                "微信视频号本地解析需要配置自己的元宝 Web cookie："
                "把 cookies 放到 /app/cookies/wechat_channels_yuanbao_cookies.txt，"
                "或设置 WECHAT_CHANNELS_YUANBAO_COOKIE。"
            )

        parse_payload = {"type": "video_channel_url", "url": url, "scene": 1}
        parse_response = self.session.post(
            YUANBAO_PARSE_URL,
            data=json.dumps(parse_payload, ensure_ascii=False),
            headers={**self._yuanbao_headers(), "Cookie": cookie},
            timeout=30,
            proxies=self._proxies(),
        )
        if parse_response.status_code >= 400:
            raise ValueError(f"腾讯元宝解析返回 HTTP {parse_response.status_code}: {parse_response.text[:300]}")
        try:
            parse_data = parse_response.json()
        except ValueError as exc:
            raise ValueError(f"腾讯元宝解析返回非 JSON: {parse_response.text[:300]}") from exc

        parse_result = parse_data.get("data") if isinstance(parse_data, dict) else {}
        if not isinstance(parse_result, dict):
            parse_result = {}
        playable_url = str(parse_result.get("playable_url") or "")
        parsed_playable = urlparse(playable_url)
        token = self._query_value(parsed_playable.query, "token")
        export_id = self._query_value(parsed_playable.query, "eid") or str(parse_result.get("wx_export_id") or "")
        if not token or not export_id:
            raise ValueError("腾讯元宝解析未返回有效 token/eid，可能是 cookie 失效或链接不可解析")

        feed_payload = {"baseReq": {"generalToken": token}, "exportId": export_id}
        rid = self._rid()
        feed_url = f"{CHANNELS_FEED_INFO_URL}?_rid={rid}&_pageUrl=https:%2F%2Fchannels.weixin.qq.com%2Ffinder-preview%2Fpages%2Ffeed"
        referer = (
            "https://channels.weixin.qq.com/finder-preview/pages/feed"
            f"?entry_card_type=48&comment_scene=39&appid=0&token={token}&entry_scene=0&eid={export_id}"
        )
        feed_response = self.session.post(
            feed_url,
            data=json.dumps(feed_payload, ensure_ascii=False),
            headers={**self._channels_headers(), "Referer": referer},
            timeout=30,
            proxies=self._proxies(),
        )
        if feed_response.status_code >= 400:
            raise ValueError(f"腾讯视频号详情返回 HTTP {feed_response.status_code}: {feed_response.text[:300]}")
        try:
            feed_data = feed_response.json()
        except ValueError as exc:
            raise ValueError(f"腾讯视频号详情返回非 JSON: {feed_response.text[:300]}") from exc

        feed_payload = self._find_feed_payload(feed_data)
        if not feed_payload:
            raise ValueError("腾讯视频号详情未返回 feedInfo")
        feed_info = feed_payload.get("feedInfo") or {}
        author_info = feed_payload.get("authorInfo") or {}
        video_url = self._video_url_from_feed(feed_info)
        if not video_url:
            raise ValueError("腾讯视频号详情里没有可下载视频 URL")
        return {
            "video_url": video_url,
            "title": str(feed_info.get("description") or ""),
            "author": str(author_info.get("nickname") or ""),
        }

    @staticmethod
    def _find_feed_payload(value: Any) -> Optional[Dict[str, Any]]:
        if isinstance(value, dict):
            if isinstance(value.get("feedInfo"), dict):
                return value
            for child in value.values():
                found = WeChatChannelsDownloader._find_feed_payload(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = WeChatChannelsDownloader._find_feed_payload(child)
                if found:
                    return found
        return None

    @staticmethod
    def _video_url_from_feed(feed_info: Dict[str, Any]) -> str:
        candidates = [
            ((feed_info.get("h264VideoInfo") or {}).get("videoUrl") if isinstance(feed_info.get("h264VideoInfo"), dict) else ""),
            str(feed_info.get("originVideoUrl") or ""),
            str(feed_info.get("videoUrl") or ""),
            ((feed_info.get("h265VideoInfo") or {}).get("videoUrl") if isinstance(feed_info.get("h265VideoInfo"), dict) else ""),
        ]
        return next((candidate.strip() for candidate in candidates if isinstance(candidate, str) and candidate.strip()), "")

    def _download_url(
        self,
        url: str,
        output_dir: Path,
        stem: str,
        progress_callback: ProgressCallback,
    ) -> Path:
        headers = {
            "Referer": "https://channels.weixin.qq.com/",
            "Accept": "*/*",
        }
        with self.session.get(url, headers=headers, stream=True, timeout=60, proxies=self._proxies()) as response:
            if response.status_code >= 400:
                raise ValueError(f"视频文件下载返回 HTTP {response.status_code}: {response.text[:300]}")

            total = int(response.headers.get("content-length") or 0)
            suffix = _suffix_from_response(url, response.headers.get("content-type", ""))
            final_path = self._unique_path(output_dir / f"{stem}{suffix}")
            tmp_path = final_path.with_suffix(final_path.suffix + ".part")
            tmp_path.unlink(missing_ok=True)

            downloaded = 0
            started = time.time()
            last_emit = 0.0
            with tmp_path.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if progress_callback and (now - last_emit >= 1.0 or (total and downloaded >= total)):
                        elapsed = max(0.001, now - started)
                        progress_callback(
                            {
                                "status": "downloading",
                                "filename": final_path.name,
                                "downloaded_bytes": downloaded,
                                "total_bytes": total,
                                "speed": downloaded / elapsed,
                            }
                        )
                        last_emit = now

        if downloaded <= 0:
            tmp_path.unlink(missing_ok=True)
            raise ValueError("视频号文件下载为空")

        tmp_path.replace(final_path)
        return final_path

    @staticmethod
    def _unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        for index in range(1, 1000):
            candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
            if not candidate.exists():
                return candidate
        return path.with_name(f"{path.stem}_{int(time.time())}{path.suffix}")

    def _proxies(self) -> Optional[Dict[str, str]]:
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None

    @staticmethod
    def _query_value(query: str, key: str) -> str:
        from urllib.parse import parse_qs

        values = parse_qs(query).get(key) or []
        return values[0] if values else ""

    @staticmethod
    def _rid() -> str:
        import random

        return f"{int(time.time()):x}-" + "".join(random.choice("0123456789abcdef") for _ in range(8))

    @staticmethod
    def _yuanbao_headers() -> Dict[str, str]:
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://yuanbao.tencent.com",
            "referer": "https://yuanbao.tencent.com/chat/naQivTmsDa/cf4d0079-ed1b-4c55-a3f3-2ca1379727d1",
            "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "t-userid": "b9575f6b0a8c4a55a08096904a5ef20a",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "x-agentid": "naQivTmsDa/cf4d0079-ed1b-4c55-a3f3-2ca1379727d1",
            "x-commit-tag": "72282a0d",
            "x-device-id": "1921b001708100d7fa31002b9646bd0cc15a3e2e1f",
            "x-hy106": "",
            "x-hy92": "e963067ffa31002b9646bd0c03000008b1951a",
            "x-hy93": "1921b001708100d7fa31002b9646bd0cc15a3e2e1f",
            "x-id": "b9575f6b0a8c4a55a08096904a5ef20a",
            "x-instance-id": "5",
            "x-language": "zh-CN",
            "x-os_version": "Mac OS(10.15.7)-Blink",
            "x-platform": "mac",
            "x-requested-with": "XMLHttpRequest",
            "x-source": "web",
            "x-web-third-source": "main",
            "x-webdriver": "0",
            "x-webversion": "2.69.0",
            "x-ybuitest": "0",
        }

    @staticmethod
    def _channels_headers() -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": "https://channels.weixin.qq.com",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
