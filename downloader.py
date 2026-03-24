#!/usr/bin/env python3
import argparse
import http.client
import http.cookiejar
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from plugins import load as load_plugin


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
DELAY_SECONDS = 1.0
MAX_RETRIES = 3


def patch_http_connection():
    original_connect = http.client.HTTPConnection.connect

    def patched_connect(self):
        self.sock = self._create_connection((self.host, self.port), self.timeout, self.source_address)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            if exc.errno not in {9, 22}:
                raise
        if getattr(self, "_tunnel_host", None):
            self._tunnel()

    http.client.HTTPConnection.connect = patched_connect
    return original_connect


def build_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
    )


def request(opener, spec, cookie):
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(DELAY_SECONDS)
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": spec.accept,
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            if spec.referer:
                headers["Referer"] = spec.referer
            if cookie:
                headers["Cookie"] = cookie

            data = None
            if spec.data is not None:
                data = urllib.parse.urlencode(spec.data).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                headers["X-Requested-With"] = "XMLHttpRequest"

            req = urllib.request.Request(spec.url, data=data, headers=headers, method=spec.method)
            with opener.open(req, timeout=30) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, "replace")
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if attempt >= MAX_RETRIES or not retryable:
                raise
        except urllib.error.URLError:
            if attempt >= MAX_RETRIES:
                raise
        time.sleep(attempt)

def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name or "download"


def save_episode(out_dir, episode, body):
    save_dir = Path(out_dir) / time.strftime("%Y-%m-%d")
    save_dir.mkdir(parents=True, exist_ok=True)
    title = episode.title.strip() or f"{episode.order}화"
    path = save_dir / sanitize_filename(f"{episode.order}화 - {title}.md")
    path.write_text(f"# {episode.order}화 - {title}\n\n{body}\n", encoding="utf-8")
    return path


def iter_episode_pages(plugin, opener, work_id, cookie):
    page = 0
    while True:
        items = plugin.parse_episode_list(
            request(opener, plugin.make_episode_list_request(work_id, page), cookie)
        )
        if not items:
            return
        yield items
        page += 1


def parse_args():
    parser = argparse.ArgumentParser(description="플러그인 기반 다운로더")
    parser.add_argument("site", help="사이트 이름. 예: novelpia")
    parser.add_argument("url", help="작품 URL 또는 사이트별 ID")
    parser.add_argument("--start", type=int, help="시작 화수")
    parser.add_argument("--end", type=int, help="끝 화수")
    parser.add_argument("--list-only", action="store_true", help="회차 목록만 출력")
    parser.add_argument("--cookie", help="브라우저 쿠키 문자열")
    parser.add_argument("--out-dir", default="downloads", help="저장 폴더")
    return parser.parse_args()


def main():
    args = parse_args()
    plugin = load_plugin(args.site)
    patch_http_connection()
    opener = build_opener()

    work_id = plugin.extract_work_id(args.url)
    print(f"[정보] site={args.site}")
    print(f"[정보] work_id={work_id}")
    print(f"[정보] 요청 딜레이={DELAY_SECONDS:.1f}초")

    if args.list_only:
        episodes = []
        for page_items in iter_episode_pages(plugin, opener, work_id, args.cookie):
            episodes.extend(page_items)
        episodes = sorted({episode.episode_id: episode for episode in episodes}.values(), key=lambda ep: ep.order)
        if not episodes:
            raise RuntimeError("다운로드 가능한 회차를 찾지 못했습니다.")
        for ep in episodes:
            print(f"{ep.order:4d} | {ep.episode_id:8d} | {ep.title}")
        return

    start = args.start if args.start is not None else 1
    end = args.end
    matched_count = 0
    last_path = None
    for page_items in iter_episode_pages(plugin, opener, work_id, args.cookie):
        for episode in page_items:
            if episode.order < start:
                continue
            if end is not None and episode.order > end:
                continue
            matched_count += 1
            print(f"[다운로드] {matched_count} | {episode.order}화 | {episode.title}")
            responses = [request(opener, spec, args.cookie).strip() for spec in plugin.make_episode_requests(episode)]
            content = plugin.parse_episode_responses(responses)
            last_path = save_episode(args.out_dir, episode, content.body)
        if end is not None and page_items[-1].order >= end:
            break

    if not matched_count:
        raise RuntimeError("선택한 범위에 해당하는 회차가 없습니다.")

    if last_path:
        print(f"[완료] 마지막 저장 파일: {last_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[중단] 사용자 취소", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[오류] {exc}", file=sys.stderr)
        sys.exit(1)
