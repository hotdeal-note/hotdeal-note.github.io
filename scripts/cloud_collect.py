#!/usr/bin/env python3
"""클라우드(GitHub Actions) 핫딜 후보 수집기.

PC 없이도 30분마다 커뮤니티 핫딜 게시판을 읽어 candidates.json 에 새 후보를 올린다.
표준 라이브러리만 사용한다(러너에 별도 패키지 설치 불필요).

검증된 소스(2026-11-22 이 환경에서 HTTP 200 + 파싱 성공):
  * 뽐뿌 게시판 RSS   https://www.ppomppu.co.kr/rss.php?id=ppomppu
  * 클리앙 알뜰구매    https://www.clien.net/service/board/jirum
검증 실패(수집 대상에서 제외, 사유는 README 참고):
  알구몬·루리웹·퀘이사존·에펨코리아·오늘의유머(403/430/timeout), SLR클럽(게시판 폐쇄),
  네이버+ 랭킹(JS 렌더링 전용, 오픈API 키 필요), 다나와(응답 20초 이상).

사용:
  python3 scripts/cloud_collect.py            # 실제 수집·병합
  python3 scripts/cloud_collect.py --dry-run  # 파일을 쓰지 않고 결과만 확인
  python3 scripts/cloud_collect.py --selftest # 네트워크 없이 파서 단위 검증
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

KST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_PATH = REPO_ROOT / "candidates.json"
DEALS_PATH = REPO_ROOT / "deals.json"
HISTORY_PATH = REPO_ROOT / "data" / "price-history.json"

SOURCE_STATUS: Dict[str, str] = {}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

# 화장품·숙박·중고 등 '시세 페이지에 올리지 않는' 품목 (PC 키트와 동일 기준)
EXCLUDE_KEYWORDS = [
    # 화장품 / 스킨케어 / 뷰티
    "화장품", "스킨", "로션", "크림", "에센스", "세럼", "앰플", "토너", "마스크팩", "팩트",
    "쿠션", "파운데이션", "립스틱", "립밤", "아이섀도", "섀도", "블러셔", "하이라이터",
    "컨실러", "메이크업", "네일", "매니큐어", "향수", "미스트", "선크림", "선스틱",
    "클렌저", "클렌징", "폼클렌징", "샴푸", "컨디셔너", "트리트먼트", "헤어오일",
    "바디워시", "바디로션", "핸드크림", "아이크림", "미백", "주름개선", "탈모",
    "여성청결제", "생리대", "기저귀", "유아크림", "베이비로션",
    # 여행 / 숙박 / 이용권
    "호텔", "리조트", "숙박", "펜션", "패키지", "워터파크", "테마파크", "이용권",
    "입장권", "티켓", "골프", "항공", "렌터카",
    # 중고 / 리퍼 / 판매글 성격
    "중고", "리퍼", "전시상품", "반품", "삽니다", "팝니다",
]
# 해외 구매대행·직구: 국내 최저가 비교가 무의미하므로 제외
EXCLUDE_OVERSEAS = [
    "아마존", "amazon", "알리", "aliexpress", "테무", "temu", "쉬인", "shein",
    "직구", "해외구매", "해외배송", "병행수입", "iherb", "아이허브",
]
# 상품이 아닌 정보·질문 글
EXCLUDE_NON_PRODUCT = [
    "질문", "문의", "알려주세요", "추천좀", "추천 부탁", "나눔", "[정보]", "공지",
    "이벤트 공유", "당첨", "설문",
]

# 품질 기준(후보가 너무 많이 쌓여 피클 검수 사용량이 커지는 것을 막는다)
MIN_PRICE = 1000
MAX_PRICE = 3_000_000
DEFAULT_MIN_RECS = {"ppomppu": 3, "clien": 5}
DEFAULT_MAX_NEW = 6
DUP_WINDOW_DAYS = 3
DUP_RATIO = 0.85


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
def now_kst() -> datetime:
    return datetime.now(KST)


def iso_now() -> str:
    return now_kst().isoformat(timespec="seconds")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str, timeout: float = 15.0, max_bytes: int = 1_500_000) -> str:
    request = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        raw = response.read(max_bytes)
    if not raw:
        raise RuntimeError("빈 응답")
    return raw.decode("utf-8", "replace")


def strip_tags(text: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html_mod.unescape(text)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def excluded_reason(title: str) -> Optional[str]:
    lowered = title.lower()
    for keyword in EXCLUDE_KEYWORDS + EXCLUDE_OVERSEAS + EXCLUDE_NON_PRODUCT:
        if keyword and keyword.lower() in lowered:
            return keyword
    return None


# ---------------------------------------------------------------------------
# 제목 파싱: "[몰] 상품명 (17,900원/무료)" 형태
# ---------------------------------------------------------------------------
PRICE_PATTERN = re.compile(r"([0-9][0-9,\.]{2,12})\s*원")
# "정가 34,000원 → 19,800원", "2개 19,800원 (개당 9,900원)" 에서 판매가가 아닌 숫자
PRICE_NOISE_PREFIX = ("정가", "소비자가", "원가", "개당", "1개당", "팩당", "장당", "ml당", "g당", "최대")
MALL_PATTERN = re.compile(r"^\s*[\[\(【]([^\]\)】]{1,20})[\]\)】]\s*")
CURRENCY_FOREIGN = re.compile(r"(?:US)?\$\s?[0-9]|[0-9]\s?(?:USD|엔|위안)")


def parse_price(title: str) -> Tuple[Optional[int], str, str]:
    """(가격, 가격 표기 방식, 배송 메모)를 돌려준다.

    정가·개당 가격처럼 판매가가 아닌 숫자는 건너뛰고, 남은 마지막 금액을 판매가로 본다.
    """
    prices = []
    for match in PRICE_PATTERN.finditer(title):
        value = as_int(match.group(1).replace(".", ""))
        if not value:
            continue
        prefix = title[max(0, match.start() - 6):match.start()]
        if any(noise in prefix for noise in PRICE_NOISE_PREFIX):
            continue
        prices.append(value)
    if not prices:
        prices = [as_int(m.group(1).replace(".", "")) for m in PRICE_PATTERN.finditer(title)]
        prices = [value for value in prices if value]
    price = prices[-1] if prices else None
    shipping = ""
    lowered = title.lower()
    if "무료배송" in title or "무배" in title or "/무료" in title or "무료" in title:
        shipping = "무료배송 표기"
    elif "배송비" in lowered:
        shipping = "배송비 별도 표기"
    note = "커뮤니티 제목 표기 가격"
    if CURRENCY_FOREIGN.search(title):
        note = "해외 통화 표기 포함 · 원화 가격 확인 필요"
    return price, note, shipping


TRAILING_PRICE_BLOCK = re.compile(r"\s*[\(\[][^\(\)\[\]]*[0-9][0-9,\.]*\s*원[^\(\)\[\]]*[\)\]]\s*$")
EFFECTIVE_LABEL = re.compile(r"^[\[\(]?\s*(?:최종할인가|할인가|특가|최저가)\s*[\)\]]?\s*")


def clean_title(title: str) -> str:
    """몰 이름과 끝의 가격/배송 표기를 떼어 상품명만 남긴다."""
    title = normalize_space(html_mod.unescape(title))
    title = MALL_PATTERN.sub("", title).strip()
    for _ in range(3):
        stripped = TRAILING_PRICE_BLOCK.sub("", title).strip()
        if stripped == title:
            break
        title = stripped
    title = EFFECTIVE_LABEL.sub("", title)
    return title.strip()


def split_mall(title: str) -> Tuple[str, str]:
    match = MALL_PATTERN.match(normalize_space(title))
    mall = normalize_space(match.group(1)) if match else ""
    return mall, clean_title(title)


def product_key(title: str) -> str:
    """중복 판정용 상품명 키(몰 이름·가격·기호 제거)."""
    text = clean_title(title)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[\[\]{}<>()/,·\-+~!@#$%^&*_=|\"'`]", " ", text)
    text = re.sub(r"[0-9]+\s*(?:원|개|팩|매입|입|ml|l|g|kg|mm|cm|개입)", " ", text, flags=re.I)
    return normalize_space(text).lower()


# ---------------------------------------------------------------------------
# 소스 1: 뽐뿌 게시판 RSS
# ---------------------------------------------------------------------------
PPOMPPU_RSS = "https://www.ppomppu.co.kr/rss.php?id=ppomppu"
ITEM_PATTERN = re.compile(r"<item>(.*?)</item>", re.S | re.I)
FIELD_PATTERN = r"<{tag}>(.*?)</{tag}>"


def _field(block: str, tag: str) -> str:
    match = re.search(FIELD_PATTERN.format(tag=tag), block, re.S | re.I)
    if not match:
        return ""
    value = match.group(1)
    value = re.sub(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", r"\1", value, flags=re.S)
    return html_mod.unescape(value).strip()


def parse_ppomppu(xml_text: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for block in ITEM_PATTERN.findall(xml_text):
        title = _field(block, "title")
        link = _field(block, "link")
        if not title or not link:
            continue
        post_id = ""
        match = re.search(r"[?&]no=(\d+)", link)
        if match:
            post_id = match.group(1)
        if not post_id:
            continue
        hits = re.findall(r"\d+", _field(block, "hits"))
        recommendations = as_int(hits[0]) if hits else None
        views = as_int(hits[1]) if len(hits) > 1 else None
        comments = as_int(hits[2]) if len(hits) > 2 else None
        items.append({
            "id": f"ppomppu-{post_id}",
            "source": "ppomppu",
            "source_name": "뽐뿌 게시판",
            "raw_title": title,
            "product_url": link.replace("&amp;", "&"),
            "description": normalize_space(strip_tags(_field(block, "description")))[:200],
            "author": _field(block, "author")[:2] + "**" if _field(block, "author") else "",
            "published": _field(block, "pubDate"),
            "recommendations": recommendations,
            "comments": comments,
            "views": views,
        })
    return items


def collect_ppomppu(timeout: float) -> List[Dict[str, Any]]:
    text = fetch(PPOMPPU_RSS, timeout=timeout)
    if "<rss" not in text.lower():
        raise RuntimeError("RSS 형식이 아님")
    return parse_ppomppu(text)


# ---------------------------------------------------------------------------
# 소스 2: 클리앙 알뜰구매
# ---------------------------------------------------------------------------
CLIEN_LIST = "https://www.clien.net/service/board/jirum"
CLIEN_ROW = re.compile(
    r'<div class="list_item symph_row[^"]*"\s+data-role="list-row"(.*?)</div>\s*</div>\s*</div>',
    re.S,
)


def parse_clien(page_html: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    chunks = re.split(r'<div class="list_item symph_row', page_html)
    for chunk in chunks[1:]:
        sn = re.search(r'data-board-sn=["\']?(\d+)', chunk)
        if not sn:
            continue
        post_id = sn.group(1)
        subject = re.search(r'class="list_subject"[^>]*title="([^"]*)"', chunk)
        if not subject:
            subject = re.search(r'data-role="list-title-text"[^>]*>(.*?)</a>', chunk, re.S)
        if not subject:
            continue
        title = normalize_space(html_mod.unescape(strip_tags(subject.group(1))))
        rec_match = re.search(r'data-role="list-like-count"[^>]*>\s*(?:<em[^>]*>)?([\d,]*)(?:</em>)?', chunk)
        recommendations = as_int(rec_match.group(1)) if rec_match and rec_match.group(1) else 0
        comment_match = re.search(r'data-comment-count=["\']?(\d+)', chunk)
        comments = as_int(comment_match.group(1)) if comment_match else 0
        time_match = re.search(r'<span class="timestamp">([^<]+)</span>', chunk)
        image_match = re.search(r'<img src="(https://[^"]+)"', chunk)
        if not title:
            continue
        items.append({
            "id": f"clien-{post_id}",
            "source": "clien",
            "source_name": "클리앙 알뜰구매",
            "raw_title": title,
            "product_url": f"https://www.clien.net/service/board/jirum/{post_id}",
            "description": "",
            "author": "",
            "published": time_match.group(1).strip() if time_match else "",
            "recommendations": recommendations,
            "comments": comments,
            "views": None,
            "image_url": image_match.group(1) if image_match else "",
        })
    return items


def collect_clien(timeout: float) -> List[Dict[str, Any]]:
    text = fetch(CLIEN_LIST, timeout=timeout)
    if "클리앙" not in text:
        raise RuntimeError("클리앙 페이지가 아님(차단/변경 의심)")
    items = parse_clien(text)
    if not items:
        raise RuntimeError("목록 파싱 결과 0건(구조 변경 의심)")
    return items


# ---------------------------------------------------------------------------
# 후보 생성·병합
# ---------------------------------------------------------------------------
def similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


def is_duplicate_title(key: str, existing_keys: List[Tuple[str, str]]) -> Optional[str]:
    for other_key, other_id in existing_keys:
        if not other_key:
            continue
        if key == other_key or similarity(key, other_key) >= DUP_RATIO:
            return other_id
    return None


def load_existing() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    data = load_json(CANDIDATES_PATH, {})
    if not isinstance(data, dict):
        data = {}
    items = data.get("candidates")
    if not isinstance(items, list):
        items = []
    return data, items


def published_keys() -> List[Tuple[str, str]]:
    data = load_json(DEALS_PATH, {})
    deals = data.get("deals") if isinstance(data, dict) else []
    result = []
    for deal in deals or []:
        if isinstance(deal, dict):
            title = deal.get("title") or ""
            if title:
                result.append((product_key(title), str(deal.get("id") or title)))
    return result


def recent_candidate_keys(items: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    cutoff = now_kst() - timedelta(days=DUP_WINDOW_DAYS)
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        if not title:
            continue
        collected = str(item.get("collected_at") or "")
        try:
            moment = datetime.fromisoformat(collected)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=KST)
            if moment < cutoff:
                continue
        except ValueError:
            pass
        result.append((product_key(title), str(item.get("id") or title)))
    return result


def build_candidate(raw: Dict[str, Any], history: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    # history 는 읽기만 하고, 실제 반영은 선택된 후보만 update_history() 에서 한다.
    title = raw["raw_title"]
    reason = excluded_reason(title)
    if reason:
        return None, f"제외 키워드({reason})"
    price, price_note, shipping = parse_price(title)
    if price is None:
        return None, "가격 표기 없음"
    if price < MIN_PRICE or price > MAX_PRICE:
        return None, f"가격 범위 밖({price:,}원)"
    mall, product = split_mall(title)
    if not product:
        return None, "상품명 없음"
    if len(product_key(product)) < 4:
        return None, "상품명이 너무 짧음"

    min_recs = DEFAULT_MIN_RECS.get(raw["source"], 3)
    min_recs = env_int(f"MIN_RECS_{raw['source'].upper()}", min_recs)
    recs = raw.get("recommendations") or 0
    comments = raw.get("comments") or 0
    if recs < min_recs and comments < min_recs * 3:
        return None, f"추천 {recs}·댓글 {comments} (기준 추천 {min_recs})"

    deal_id = raw["id"]
    previous = history.get(deal_id, {})
    previous_lowest = previous.get("lowest_price")
    lowest = previous_lowest if previous_lowest and previous_lowest < price else price
    is_new_low = bool(previous_lowest and price < previous_lowest)

    reasons = []
    if mall:
        reasons.append(f"{mall} 판매 · 커뮤니티 추천 {recs}건" + (f" · 댓글 {comments}건" if comments else ""))
    else:
        reasons.append(f"{raw['source_name']} 추천 {recs}건" + (f" · 댓글 {comments}건" if comments else ""))
    if shipping:
        reasons.append(f"{shipping} · {price:,}원")
    if raw.get("description"):
        reasons.append(raw["description"][:120])
    if is_new_low and previous_lowest:
        reasons.append(f"이전 최저 {previous_lowest:,}원 → {price:,}원")

    candidate = {
        "id": deal_id,
        "source": raw["source"],
        "source_name": raw["source_name"],
        "category": "커뮤니티",
        "title": product,
        "product_url": raw["product_url"],
        "image_url": raw.get("image_url", ""),
        "price": price,
        "effective_price": price,
        "list_price": None,
        "price_note": price_note + (f" ({shipping})" if shipping else ""),
        "market_low": None,
        "market_low_mall": "",
        "market_low_title": "",
        "market_low_link": "",
        "market_source": "",
        "market_ref": "",
        "history_lowest": lowest,
        "is_new_low": is_new_low,
        "score": None,
        "reasons": reasons[:4],
        "deadline": "",
        "sold_rate": None,
        "status": "pending",
        "needs_pickle_review": True,
        "collected_at": iso_now(),
        "source_checked_at": now_kst().strftime("%Y-%m-%d %H:%M"),
        "community": {
            "recommendations": recs,
            "comments": comments,
            "views": raw.get("views"),
            "posted": raw.get("published", ""),
        },
        "review_note": (
            "클라우드(GitHub Actions) 수집: 커뮤니티 추천·가격만 확인했고 다나와 대조는 하지 않았습니다. "
            "피클이 최종 검수해 주세요."
        ),
    }
    return candidate, "통과"


def update_history(history: Dict[str, Any], candidate: Dict[str, Any]) -> None:
    deal_id = candidate["id"]
    price = candidate.get("effective_price") or candidate.get("price")
    previous = history.get(deal_id, {})
    lowest = previous.get("lowest_price")
    if not lowest or (price and price < lowest):
        lowest = price
    history[deal_id] = {
        "title": candidate.get("title", ""),
        "source": candidate.get("source", ""),
        "first_seen": previous.get("first_seen") or now_kst().strftime("%Y-%m-%d"),
        "last_seen": now_kst().strftime("%Y-%m-%d"),
        "lowest_price": lowest,
        "last_price": price,
        "observations": int(previous.get("observations") or 0) + 1,
    }


def collect_all(timeout: float) -> List[Dict[str, Any]]:
    collected: List[Dict[str, Any]] = []
    for name, func in (("ppomppu", collect_ppomppu), ("clien", collect_clien)):
        started = time.time()
        try:
            items = func(timeout)
            SOURCE_STATUS[name] = f"OK {len(items)}건 ({time.time() - started:.1f}s)"
            collected.extend(items)
        except (urllib.error.URLError, OSError, RuntimeError, ValueError) as error:
            SOURCE_STATUS[name] = f"실패: {error}"
            log(f"[warn] {name} 수집 실패: {error}")
    return collected


def run(dry_run: bool = False) -> int:
    data, existing = load_existing()
    history = load_json(HISTORY_PATH, {})
    if not isinstance(history, dict):
        history = {}

    collected = collect_all(timeout=float(env_int("HTTP_TIMEOUT", 20)))
    ok_sources = [name for name, status in SOURCE_STATUS.items() if status.startswith("OK")]
    for name, status in SOURCE_STATUS.items():
        log(f"[source] {name}: {status}")
    if not ok_sources:
        # 소스 전체 차단은 흔한 일이 아니지만, 30분짜리 워크플로가 실패 메일을 반복 발송하지 않도록
        # 실행은 성공으로 끝내고 경고만 남긴다(Actions 화면의 Annotation으로 보인다).
        log("::warning::모든 소스 수집 실패 — candidates.json을 변경하지 않았습니다. 소스 차단/변경 여부를 확인하세요.")
        print("ADDED=0", flush=True)
        return 0

    known_ids = {str(item.get("id")) for item in existing if isinstance(item, dict)}
    dup_keys = recent_candidate_keys(existing) + published_keys()

    max_new = env_int("MAX_NEW", DEFAULT_MAX_NEW)
    accepted: List[Dict[str, Any]] = []
    rejected: Dict[str, int] = {}
    for raw in collected:
        if raw["id"] in known_ids:
            continue
        candidate, reason = build_candidate(raw, history)
        if candidate is None:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        key = product_key(candidate["title"])
        duplicate_of = is_duplicate_title(key, dup_keys)
        if duplicate_of:
            rejected["이미 있는 상품"] = rejected.get("이미 있는 상품", 0) + 1
            continue
        candidate["_key"] = key
        accepted.append(candidate)
        known_ids.add(candidate["id"])
        dup_keys.append((key, candidate["id"]))

    accepted.sort(
        key=lambda c: (
            int(c["community"].get("recommendations") or 0),
            int(c["community"].get("comments") or 0),
        ),
        reverse=True,
    )
    chosen = accepted[:max_new]
    dropped = len(accepted) - len(chosen)

    log(f"[filter] 수집 {len(collected)}건 · 신규 통과 {len(accepted)}건 · 이번 반영 {len(chosen)}건"
        + (f" · 다음 실행으로 미룸 {dropped}건" if dropped else ""))
    if rejected:
        summary = ", ".join(f"{reason} {count}건" for reason, count in sorted(rejected.items()))
        log(f"[filter] 제외 {summary}")

    for candidate in chosen:
        candidate.pop("_key", None)
        log(f"  + [{candidate['source']}] {candidate['title'][:60]} · {candidate['price']:,}원")

    if not chosen:
        log("새 후보 없음 — 커밋하지 않습니다.")
        return 0

    if dry_run:
        log("[dry-run] 파일을 쓰지 않았습니다.")
        return 0

    for candidate in chosen:
        update_history(history, candidate)

    merged = existing + chosen
    data["schema_version"] = data.get("schema_version") or 1
    data["updated_at"] = iso_now()
    data["candidates"] = merged
    save_json(CANDIDATES_PATH, data)
    save_json(HISTORY_PATH, history)
    log(f"candidates.json 반영 완료: {len(existing)} → {len(merged)}건")
    print(f"ADDED={len(chosen)}", flush=True)
    return 0


# ---------------------------------------------------------------------------
# 자체 검증(네트워크 불필요)
# ---------------------------------------------------------------------------
PPOMPPU_SAMPLE = """<rss version="2.0"><channel>
<item><title>[G마켓] 국산 면양말 10켤레(6,540원/무료)</title>
<link>http://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&amp;no=750706</link>
<description>쿠폰 받으면 저렴합니다</description><author>김남일12</author>
<pubDate>Sat, 21 Nov 2026 15:18:39 GMT</pubDate><hits> [6|46|9|0]</hits></item>
<item><title>[쿠팡] 아마존 파이어 TV 스틱 (39,900원/무료)</title>
<link>http://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&amp;no=750705</link>
<description>해외직구</description><author>test</author>
<pubDate>Sat, 21 Nov 2026 15:13:22 GMT</pubDate><hits> [9|185|3|0]</hits></item>
<item><title>설문 이벤트 참여하세요</title>
<link>http://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&amp;no=750704</link>
<description>이벤트</description><author>test</author>
<pubDate>Sat, 21 Nov 2026 15:07:57 GMT</pubDate><hits> [9|268|3|0]</hits></item>
</channel></rss>"""

CLIEN_SAMPLE = """<div class="list_item symph_row jirum  " data-role="list-row" data-author-id=abc data-board-sn=19296664 data-comment-count=7>
<div class="list_symph view_symph" data-role="list-like-count"><em class="none">11</em></div>
<div class="list_title" data-role="list-title"><span class="list_subject" data-role="cut-string" title="[하이마트] 한일 전기요 2구 (26,900원/무배)">
<a href="/service/board/jirum/19296664">한일 전기요</a></span></div>
<div class="list_img" data-role="list-img"><img src="https://edgio.clien.net/F01/123.jpg"></div>
<div class="list_time"><span class="time popover">00:10<span class="timestamp">2026-11-22 00:10:20</span></span></div>
</div></div></div>"""


def selftest() -> int:
    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        log(f"{'PASS' if condition else 'FAIL'} {name}" + (f" — {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    ppomppu = parse_ppomppu(PPOMPPU_SAMPLE)
    check("뽐뿌 3건 파싱", len(ppomppu) == 3, str(len(ppomppu)))
    if ppomppu:
        first = ppomppu[0]
        check("뽐뿌 id 추출", first["id"] == "ppomppu-750706", first["id"])
        check("뽐뿌 추천/댓글", first["recommendations"] == 6 and first["comments"] == 9, str(first))
        price, _, shipping = parse_price(first["raw_title"])
        check("뽐뿌 가격 파싱", price == 6540 and shipping == "무료배송 표기", f"{price} {shipping}")
        mall, product = split_mall(first["raw_title"])
        check("몰/상품명 분리", mall == "G마켓" and product.startswith("국산 면양말"), f"{mall} | {product}")

    clien = parse_clien(CLIEN_SAMPLE)
    check("클리앙 1건 파싱", len(clien) == 1, str(len(clien)))
    if clien:
        row = clien[0]
        check("클리앙 추천/댓글", row["recommendations"] == 11 and row["comments"] == 7, str(row))
        check("클리앙 URL", row["product_url"].endswith("/19296664"), row["product_url"])
        price, _, shipping = parse_price(row["raw_title"])
        check("클리앙 가격", price == 26900 and shipping == "무료배송 표기", f"{price} {shipping}")

    history: Dict[str, Any] = {}
    good, reason = build_candidate(ppomppu[0], history)
    check("정상 후보 통과", good is not None and reason == "통과", reason)
    if good:
        check("후보 필수 필드", good["needs_pickle_review"] is True and good["price"] == 6540, str(good.get("price")))
    overseas, reason = build_candidate(ppomppu[1], dict())
    check("해외직구 제외", overseas is None and "제외 키워드" in reason, reason)
    event, reason = build_candidate(ppomppu[2], dict())
    check("이벤트 글 제외", event is None, reason)

    os.environ["MIN_RECS_PPOMPPU"] = "50"
    low, reason = build_candidate(ppomppu[0], dict())
    check("추천 기준 미달 제외", low is None and "추천" in reason, reason)
    os.environ.pop("MIN_RECS_PPOMPPU")

    check("상품명 중복 판정", similarity(product_key("국산 면양말 10켤레"), product_key("[G마켓] 국산 면양말 10켤레")) > 0.95)
    check("다른 상품 비중복", similarity(product_key("국산 면양말 10켤레"), product_key("스테인리스 텀블러 600ml")) < 0.7)
    check("정가 숫자 무시", parse_price("[몰] 무선청소기 정가 349,000원 → 199,000원 (무료)")[0] == 199000,
          str(parse_price("[몰] 무선청소기 정가 349,000원 → 199,000원 (무료)")))
    check("개당 가격 무시", parse_price("[몰] 계란 2판 19,800원 (개당 9,900원)")[0] == 19800,
          str(parse_price("[몰] 계란 2판 19,800원 (개당 9,900원)")))

    log("RESULT " + ("OK" if not failures else f"FAIL {failures}"))
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="클라우드 핫딜 후보 수집기")
    parser.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 결과만 확인")
    parser.add_argument("--selftest", action="store_true", help="네트워크 없이 파서 검증")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
