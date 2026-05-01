#!/usr/bin/env python3
import argparse
import hashlib
import html
import http.cookiejar
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BASE_URL = "https://novelpia.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
MAX_RETRIES = 3
DELAY = 0


@dataclass
class Episode:
    order: int
    episode_id: int
    title: str


# ── HTTP ──────────────────────────────────────────────────────────────


def _build_opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
    )


def _request(opener, method, url, data=None, referer=None, accept="*/*"):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": accept,
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            if referer:
                headers["Referer"] = referer

            body = None
            if data is not None:
                body = urllib.parse.urlencode(data).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                headers["X-Requested-With"] = "XMLHttpRequest"

            req = urllib.request.Request(url, data=body, headers=headers, method=method)
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
        time.sleep(DELAY)


def _request_raw(opener, url, referer=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            headers = {"User-Agent": USER_AGENT}
            if referer:
                headers["Referer"] = referer
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError):
            if attempt >= MAX_RETRIES:
                raise
        time.sleep(DELAY)


# ── 본문 정리 ─────────────────────────────────────────────────────────


def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name or "download"


def _clean_text(text, keep_breaks=True):
    text = html.unescape(text or "")
    if keep_breaks:
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(
            r"(?i)</?(?:p|div|li|tr|td|table|section|article|blockquote|ul|ol)[^>]*>",
            "\n",
            text,
        )
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    if keep_breaks:
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _clean_body(text):
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            lines.append("")
            continue
        if line in {"커버보기", "커버접기"}:
            continue
        if re.fullmatch(r"[A-Za-z0-9+/=]{60,}", line):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_note(text):
    text = _clean_text(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = re.sub(r"^작가의 한마디\s*\(작가후기\)\s*", "", text).strip()
    return text


# ── 이미지 처리 ────────────────────────────────────────────────────────


def _normalize_image_url(src):
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return BASE_URL + src
    return src


def _extract_images_from_text(opener, text, img_dir, referer, file_prefix="", counter=None):
    """<img> 태그를 찾아 이미지를 다운로드하고 (이미지-{id}) 로 치환."""
    if counter is None:
        counter = [1]

    def _replace(m):
        src = _normalize_image_url(m.group(1))
        img_id = hashlib.md5(src.encode()).hexdigest()[:8]
        ext = Path(urllib.parse.urlparse(src).path).suffix or ".jpg"
        if ext.lower() == ".file":
            ext = ".file.jpeg"
        idx = counter[0]
        counter[0] += 1
        fname = f"{idx}_{file_prefix}{img_id}{ext}"
        dest = img_dir / fname
        if not dest.exists():
            try:
                data = _request_raw(opener, src, referer)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            except Exception:
                pass
        return f"(이미지-{img_id})"

    return re.sub(
        r"""(?is)<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["'][^>]*>""",
        _replace,
        text,
    )


# ── 노벨피아 API ──────────────────────────────────────────────────────


def extract_work_id(url_or_id):
    m = re.search(r"/novel/(\d+)", url_or_id)
    if m:
        return m.group(1)
    if url_or_id.isdigit():
        return url_or_id
    raise ValueError("노벨피아 URL 또는 novel_no를 넣어주세요.")


def fetch_episode_list(opener, work_id, page):
    html_text = _request(
        opener,
        "POST",
        f"{BASE_URL}/proc/episode_list",
        data={"novel_no": work_id, "page": page},
        referer=f"{BASE_URL}/novel/{work_id}",
        accept="text/html, */*; q=0.01",
    )
    episodes = []
    rows = re.finditer(
        r'<tr[^>]*data-episode-no="(\d+)"[^>]*>(.*?)</tr>', html_text, re.S | re.I
    )
    for fallback_order, m in enumerate(rows, start=1):
        episode_id = int(m.group(1))
        row = m.group(2)
        if "b_free" not in row:
            continue
        order_m = re.search(r">EP\.(\d+)<", row)
        title_m = re.search(r"<b>(.*?)</b>", row, re.S | re.I)
        order = int(order_m.group(1)) if order_m else fallback_order
        title = _clean_text(title_m.group(1) if title_m else "", keep_breaks=False)
        title = re.sub(r"\s+", " ", title).strip()
        episodes.append(Episode(order=order, episode_id=episode_id, title=title))
    return sorted(episodes, key=lambda e: e.order)


def iter_episode_pages(opener, work_id):
    page = 0
    while True:
        items = fetch_episode_list(opener, work_id, page)
        if not items:
            return
        yield items
        page += 1


def episode_stem(episode, order_prefix):
    title = episode.title.strip() or f"{episode.order}화"
    if order_prefix:
        return sanitize_filename(f"{episode.order}_{title}")
    return sanitize_filename(title)


def download_episode(opener, episode, download_images, out_dir, order_prefix):
    viewer_url = f"{BASE_URL}/viewer/{episode.episode_id}"
    viewer_html = _request(opener, "GET", viewer_url)
    raw_json = _request(
        opener,
        "POST",
        f"{BASE_URL}/proc/viewer_data/{episode.episode_id}",
        data={"size": "14", "viewer_paging": "0"},
        referer=viewer_url,
        accept="application/json, text/javascript, */*; q=0.01",
    )

    # 작가 후기
    note = ""
    ta = re.search(
        r'<textarea id="footer_plus"[^>]*>(.*?)</textarea>', viewer_html, re.S | re.I
    )
    if ta:
        nm = re.search(
            r"id=['\"]writer_comments_box['\"][^>]*>(.*?)</div>",
            ta.group(1),
            re.S | re.I,
        )
        if nm:
            note = _clean_note(nm.group(1))

    # 본문
    payload = json.loads(raw_json)
    lines = payload.get("s")
    if not isinstance(lines, list):
        raise RuntimeError("본문 형식이 예상과 다릅니다.")

    stem = episode_stem(episode, order_prefix)
    img_dir = out_dir / stem
    img_prefix = f"{episode.order}_" if order_prefix else ""
    img_counter = [1]
    fragments = []
    for line in lines:
        if isinstance(line, dict):
            raw = line.get("text") or ""
            if not raw:
                for key in ("file", "path", "src", "url"):
                    val = line.get(key)
                    if val:
                        if not val.startswith("http"):
                            val = f"{BASE_URL}/data/file/{val.lstrip('/')}"
                        raw = f'<img src="{val}">'
                        break
        else:
            raw = str(line)

        if download_images and raw:
            raw = _extract_images_from_text(
                opener, raw, img_dir, viewer_url, img_prefix, img_counter
            )

        t = _clean_text(raw)
        if t:
            fragments.append(t)

    body = _clean_body("\n\n".join(fragments))

    if note:
        body = f"{body}\n\n## 작가 후기\n\n{note}".strip()

    return body


def save_episode(out_dir, episode, body, order_prefix):
    out_dir.mkdir(parents=True, exist_ok=True)
    title = episode.title.strip() or f"{episode.order}화"
    path = out_dir / (episode_stem(episode, order_prefix) + ".md")
    path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    return path


# ── 설정 파싱 ─────────────────────────────────────────────────────────


DEFAULT_SETTINGS = {
    "remove_free": True,
    "download_images": True,
    "order_prefix": True,
}


def parse_settings(lines):
    settings = dict(DEFAULT_SETTINGS)
    for line in lines:
        m = re.match(r"\$\s*(\w+)\s*=\s*(\w+)", line)
        if m:
            key, val = m.group(1), m.group(2).lower()
            if key in settings:
                settings[key] = val == "true"
    return settings


def apply_title_clean(title, settings):
    if settings.get("remove_free"):
        title = re.sub(r"^무료\s*", "", title, flags=re.I)
    return title.strip()


# ── listup ────────────────────────────────────────────────────────────


def cmd_listup(args):
    opener = _build_opener()
    work_id = extract_work_id(args.url)

    episodes = []
    for page in iter_episode_pages(opener, work_id):
        episodes.extend(page)
    episodes = sorted({e.episode_id: e for e in episodes}.values(), key=lambda e: e.order)
    if not episodes:
        raise RuntimeError("회차를 찾지 못했습니다.")

    out_path = Path(f"episodes_{work_id}.txt")
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"$ novel_no={work_id}\n")
        for key in DEFAULT_SETTINGS:
            f.write(f"$ {key}={str(DEFAULT_SETTINGS[key]).lower()}\n")
        f.write("\n")
        for ep in episodes:
            f.write(f"{ep.episode_id} | {ep.order} | {ep.title}\n")

    print(f"[완료] {len(episodes)}개 회차 → {out_path}")
    print("회차, 설정 변경 후 다음 명령어 실행 >> python3 main.py download <파일경로>")


# ── download ──────────────────────────────────────────────────────────


def cmd_download(args):
    path = Path(args.path)
    if not path.exists():
        print(f"[오류] 파일이 없습니다: {path}", file=sys.stderr)
        sys.exit(1)

    raw_lines = path.read_text("utf-8").splitlines()
    settings = parse_settings(raw_lines)

    episodes = []
    for line in raw_lines:
        line = line.strip()
        if not line or line.startswith("$"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        episode_id, order, title = int(parts[0]), int(parts[1]), parts[2]
        title = apply_title_clean(title, settings)
        episodes.append(Episode(order=order, episode_id=episode_id, title=title))

    if not episodes:
        print("[오류] 다운로드할 회차가 없습니다.", file=sys.stderr)
        sys.exit(1)

    opener = _build_opener()
    out_dir = Path(f"output_{int(time.time())}")
    for idx, ep in enumerate(episodes, start=1):
        print(f"[다운로드] {idx}/{len(episodes)} | {ep.order}화 | {ep.title}")
        body = download_episode(
            opener, ep, settings["download_images"], out_dir, settings["order_prefix"]
        )
        save_episode(out_dir, ep, body, settings["order_prefix"])

    print(f"[완료] {len(episodes)}개 저장 → {out_dir}/")


# ── CLI ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="노벨피아 다운로더")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("listup", help="회차 목록을 txt 파일로 저장")
    p_list.add_argument("url", help="작품 URL 또는 novel_no")

    p_dl = sub.add_parser("download", help="txt 파일을 읽어 회차 다운로드")
    p_dl.add_argument("path", help="listup으로 생성한 txt 파일 경로")

    args = parser.parse_args()

    if args.command == "listup":
        cmd_listup(args)
    elif args.command == "download":
        cmd_download(args)


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
