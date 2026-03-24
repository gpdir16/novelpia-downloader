import html
import json
import re

from plugins.base import Episode, EpisodeContent, RequestSpec

BASE_URL = "https://novelpia.com"


def extract_work_id(url_or_id):
    match = re.search(r"/novel/(\d+)", url_or_id)
    if match:
        return match.group(1)
    if url_or_id.isdigit():
        return url_or_id
    raise ValueError("노벨피아 URL 또는 novel_no를 넣어주세요.")


def make_episode_list_request(work_id, page=0):
    return RequestSpec(
        method="POST",
        url=f"{BASE_URL}/proc/episode_list",
        data={"novel_no": work_id, "page": page},
        referer=f"{BASE_URL}/novel/{work_id}",
        accept="text/html, */*; q=0.01",
    )


def parse_episode_list(text):
    episodes = []
    rows = re.finditer(
        r'<tr[^>]*data-episode-no="(\d+)"[^>]*>(.*?)</tr>',
        text,
        re.S | re.I,
    )

    for fallback_order, match in enumerate(rows, start=1):
        episode_id = int(match.group(1))
        row = match.group(2)
        if "b_free" not in row:
            continue

        order_match = re.search(r">EP\.(\d+)<", row)
        title_match = re.search(r"<b>(.*?)</b>", row, re.S | re.I)

        order = int(order_match.group(1)) if order_match else fallback_order
        title = clean_title(title_match.group(1) if title_match else "")
        episodes.append(Episode(order=order, episode_id=episode_id, title=title))

    return sorted(episodes, key=lambda item: item.order)


def make_episode_requests(episode):
    viewer_url = f"{BASE_URL}/viewer/{episode.episode_id}"
    return [
        RequestSpec(method="GET", url=viewer_url),
        RequestSpec(
            method="POST",
            url=f"{BASE_URL}/proc/viewer_data/{episode.episode_id}",
            data={"size": "14", "viewer_paging": "0"},
            referer=viewer_url,
            accept="application/json, text/javascript, */*; q=0.01",
        ),
    ]


def parse_episode_responses(responses):
    viewer_html, raw_json = responses
    textarea = re.search(
        r'<textarea id="footer_plus"[^>]*>(.*?)</textarea>',
        viewer_html,
        re.S | re.I,
    )
    note = ""
    if textarea:
        note_match = re.search(
            r"id=['\"]writer_comments_box['\"][^>]*>(.*?)</div>",
            textarea.group(1),
            re.S | re.I,
        )
        if note_match:
            note = clean_note(note_match.group(1))

    payload = json.loads(raw_json)
    lines = payload.get("s")
    if not isinstance(lines, list):
        raise RuntimeError("본문 형식이 예상과 다릅니다.")

    fragments = []
    for line in lines:
        if isinstance(line, dict):
            fragments.append(line.get("text", ""))
        else:
            fragments.append(str(line))

    parts = [text for fragment in fragments if (text := clean_text(fragment))]

    body = clean_body("\n\n".join(parts))
    if note:
        body = f"{body}\n\n## 작가 후기\n\n{note}".strip()

    return EpisodeContent(body=body)


def clean_title(text):
    text = clean_text(text, keep_breaks=False)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(무료|FREE)\s*", "", text, flags=re.I)
    text = re.sub(r"^\d+\)\s*", "", text)
    return text.strip()


def clean_note(text):
    text = clean_text(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = re.sub(r"^작가의 한마디\s*\(작가후기\)\s*", "", text).strip()
    return text


def clean_text(text, keep_breaks=True):
    text = html.unescape(text or "")
    text = re.sub(r"(?i)<img[^>]*>", "", text)
    if keep_breaks:
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</?(?:p|div|li|tr|td|table|section|article|blockquote|ul|ol)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    if keep_breaks:
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def clean_body(text):
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
