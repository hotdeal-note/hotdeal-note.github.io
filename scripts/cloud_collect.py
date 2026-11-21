#!/usr/bin/env python3
"""클라우드(GitHub Actions) 핫딜 후보 수집기.

PC 없이도 30분마다 커뮤니티 핫딜 게시판을 읽어 candidates.json 에 새 후보를 올린다.
표준 라이브러리만 사용한다(러너에 별도 패키지 설치 불필요).

검증된 소스(2026-11-22 이 환경에서 HTTP 200 + 파싱 성공):
  * 뽐뿌 게시판 RSS   https://www.ppomppu.co.kr/rss.php?id=ppomppu
  * 뽐뿌 쇼핑뽐뿌 RSS  https://www.ppomppu.co.kr/rss.php?id=pmarket
  * 클리앙 알뜰구매    https://www.clien.net/service/board/jirum
  * 딜바다 국내핫딜    https://www.dealbada.com/bbs/board.php?bo_table=deal_domestic
  * 어미새 통합 RSS    https://eomisae.co.kr/rss
  * 이토랜드 핫딜      https://etoland.co.kr/bbs/board.php?bo_table=hotdeal
검증 실패(수집 대상에서 제외, 사유는 README 참고):
  알구몬·루리웹·퀘이사존·에펨코리아·오늘의유머(403/430/timeout), SLR클럽(게시판 폐쇄),
  쿨엔조이·플레이웨어즈·11번가·다나와(10~20초 타임아웃), SSG·롯데온·쿠팡·지마켓(403),
  위메프(도메인 매물), 티몬·무신사(SPA 껍데기), 오늘의집(SSL 오류), 디시 핫딜갤러리(폐쇄),
  아카라이브(로컬은 200이지만 GitHub Actions 러너 IP는 Cloudflare 403),
  네이버+ 랭킹(JS 렌더링 전용, 오픈API 키 필요).

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
DEFAULT_MAX_NEW = 6
PER_SOURCE_MAX = 2

# 소스별 품질 기준.
#   min_recs / min_comments: 추천이 min_recs 이상이거나 댓글이 min_comments 이상이면 통과
#   min_views: 조회수만 공개되는 게시판의 대체 기준(추천·댓글과 OR)
#   price_only: 추천·댓글이 없는 피드(RSS) — 가격 표기 + 상품명 길이로만 거른다
SOURCE_RULES: Dict[str, Dict[str, Any]] = {
    "ppomppu": {"min_recs": 3, "min_comments": 9},
    "ppomppu_pmarket": {"min_recs": 3, "min_comments": 9},
    "clien": {"min_recs": 5, "min_comments": 15},
    "etoland": {"min_recs": 5, "min_comments": 10, "min_views": 200},
    "dealbada": {"min_views": 400},
    "eomisae": {"price_only": True, "min_name_len": 8},
}
LIST_PRICE_SOURCES = {"etoland", "dealbada"}
SOURCE_NAMES = {
    "ppomppu": "뽐뿌 게시판",
    "ppomppu_pmarket": "뽐뿌 쇼핑뽐뿌",
    "clien": "클리앙 알뜰구매",
    "etoland": "이토랜드 핫딜",
    "dealbada": "딜바다 국내핫딜",
    "eomisae": "어미새",
}
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


def fetch(url: str, timeout: float = 15.0, max_bytes: int = 1_500_000, extra_headers: Optional[Dict[str, str]] = None) -> str:
    headers = dict(HTTP_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, headers=headers)
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
# 딜바다·어미새처럼 가색을 괄호 없이 뒤에 붙인 제목: "... 27,900원 / 무배"
TRAILING_BARE_PRICE = re.compile(r"\s*[0-9][0-9,]{2,}\s*원\s*(?:[~/·,]\s*)?(?:무배|무료배송|무료)?\s*$")
EFFECTIVE_LABEL = re.compile(r"^[\[\(]?\s*(?:최종할인가|할인가|특가|최저가)\s*[\)\]]?\s*")


def clean_title(title: str) -> str:
    """몰 이름과 끝의 가격/배송 표기를 떼어 상품명만 남긴다."""
    title = normalize_space(html_mod.unescape(title))
    title = MALL_PATTERN.sub("", title).strip()
    for _ in range(3):
        stripped = TRAILING_PRICE_BLOCK.sub("", title).strip()
        if stripped != title and len(product_key_core(stripped)) >= 3:
            title = stripped
            continue
        stripped = TRAILING_BARE_PRICE.sub("", title).strip()
        if stripped != title and len(product_key_core(stripped)) >= 3:
            title = stripped
            continue
        break
    title = EFFECTIVE_LABEL.sub("", title)
    # 가격을 떼고 남은 끝 쉼표·구분자 정리 ("... 240G 4개,")
    title = re.sub(r"[\s,·/\-+~]+$", "", title)
    return title.strip()


def product_key_core(text: str) -> str:
    """상품명 글자만 남긴 비교용 문자열(가격·수량·기호 제거, 순환 호출 없이)."""
    text = re.sub(r"\([^)]*\)", " ", text or "")
    text = re.sub(r"[\[\]{}<>()/,·\-+~!@#$%^&*_=|\"'`]", " ", text)
    text = re.sub(r"[0-9]+\s*(?:원|개|팩|매입|입|ml|l|g|kg|mm|cm|개입)", " ", text, flags=re.I)
    return normalize_space(text).lower()


def split_mall(title: str) -> Tuple[str, str]:
    match = MALL_PATTERN.match(normalize_space(title))
    mall = normalize_space(match.group(1)) if match else ""
    return mall, clean_title(title)


def product_key(title: str) -> str:
    """중복 판정용 상품명 키(몰 이름·가격·기호 제거)."""
    return product_key_core(clean_title(title))


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


def parse_ppomppu(xml_text: str, board: str = "ppomppu") -> List[Dict[str, Any]]:
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
        deal_id = f"ppomppu-{post_id}" if board == "ppomppu" else f"ppomppu-{board}-{post_id}"
        items.append({
            "id": deal_id,
            "source": "ppomppu" if board == "ppomppu" else f"ppomppu_{board}",
            "source_name": SOURCE_NAMES.get(board if board == "ppomppu" else f"ppomppu_{board}", "뽐뿌"),
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


PPOMPPU_PMARKET_RSS = "https://www.ppomppu.co.kr/rss.php?id=pmarket"


def collect_ppomppu_pmarket(timeout: float) -> List[Dict[str, Any]]:
    text = fetch(PPOMPPU_PMARKET_RSS, timeout=timeout)
    if "<rss" not in text.lower():
        raise RuntimeError("RSS 형식이 아님")
    items = parse_ppomppu(text, board="pmarket")
    if not items:
        raise RuntimeError("목록 파싱 결과 0건(구조 변경 의심)")
    return items


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
# 소스 4: 딜바다 국내핫딜 (그누보드 목록)
# ---------------------------------------------------------------------------
DEALBADA_LIST = "https://www.dealbada.com/bbs/board.php?bo_table=deal_domestic"


def parse_dealbada(page_html: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for row in re.split(r"<tr class=\"", page_html)[1:]:
        subject = re.search(r'class="td_subject">(.*?)</td>', row, re.S)
        if not subject:
            continue
        link = re.search(r'href="([^"]*wr_id=(\d+)[^"]*)"', subject.group(1))
        anchor = re.search(r'<a href="[^"]*wr_id=\d+[^"]*"\s*>(.*?)</a>', subject.group(1), re.S)
        if not link or not anchor:
            continue
        title = normalize_space(strip_tags(anchor.group(1)))
        if not title:
            continue
        category = re.search(r'class="bo_cate_link">([^<]*)<', row)
        if category and normalize_space(category.group(1)) == "공지":
            continue
        numbers = re.findall(r'class="td_num">\s*([\d,]+)\s*<', row)
        time_match = re.search(r'class="td_date">([^<]*)<', row)
        image_match = re.search(r"<img src='([^']+)'", row)
        items.append({
            "id": f"dealbada-{link.group(2)}",
            "source": "dealbada",
            "source_name": SOURCE_NAMES["dealbada"],
            "raw_title": title,
            "product_url": html_mod.unescape("https:" + link.group(1) if link.group(1).startswith("//") else link.group(1)),
            "description": "",
            "author": "",
            "published": time_match.group(1).strip() if time_match else "",
            "recommendations": 0,
            "comments": 0,
            "views": as_int(numbers[-1]) if numbers else None,
            "image_url": image_match.group(1) if image_match else "",
        })
    return items


def collect_dealbada(timeout: float) -> List[Dict[str, Any]]:
    text = fetch(DEALBADA_LIST, timeout=timeout)
    if "딜바다" not in text:
        raise RuntimeError("딜바다 페이지가 아님(차단/변경 의심)")
    items = parse_dealbada(text)
    if not items:
        raise RuntimeError("목록 파싱 결과 0건(구조 변경 의심)")
    return items


# ---------------------------------------------------------------------------
# 소스 5: 어미새 통합 RSS (국내 딜만, 추천/댓글 없음)
# ---------------------------------------------------------------------------
EOMISAE_RSS = "https://eomisae.co.kr/rss"
EOMISAE_SKIP_CATEGORIES = {"기타 결제인증", "자유게시판", "이벤트:쿠폰"}


def parse_eomisae(xml_text: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for block in ITEM_PATTERN.findall(xml_text):
        title = _field(block, "title")
        link = _field(block, "link")
        if not title or not link:
            continue
        categories = [normalize_space(c) for c in re.findall(r"<category>(.*?)</category>", block, re.S)]
        # '국내' 딜이 아닌 글과 결제인증·잡담 글은 제외
        if "국내" not in categories or EOMISAE_SKIP_CATEGORIES & set(categories):
            continue
        if title.startswith("[리뷰]") or "구매후기" in title:
            continue
        post_id = re.search(r"/(\d+)\s*$", link)
        if not post_id:
            continue
        items.append({
            "id": f"eomisae-{post_id.group(1)}",
            "source": "eomisae",
            "source_name": SOURCE_NAMES["eomisae"],
            "raw_title": title,
            "product_url": link,
            "description": normalize_space(strip_tags(_field(block, "description")))[:200],
            "author": "",
            "published": _field(block, "pubDate"),
            "recommendations": None,
            "comments": None,
            "views": None,
        })
    return items


def collect_eomisae(timeout: float) -> List[Dict[str, Any]]:
    text = fetch(EOMISAE_RSS, timeout=timeout)
    if "<rss" not in text.lower() or "어미새" not in text:
        raise RuntimeError("어미새 RSS가 아님(차단/변경 의심)")
    items = parse_eomisae(text)
    if not items:
        raise RuntimeError("목록 파싱 결과 0건(구조 변경 의심)")
    return items


# ---------------------------------------------------------------------------
# 소스 6: 이토랜드 핫딜 게시판 (서버렌더링 목록)
# ---------------------------------------------------------------------------
ETOLAND_LIST = "https://etoland.co.kr/bbs/board.php?bo_table=hotdeal"


def parse_etoland(page_html: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for row in re.split(r'<li class="flex gap-3 px-4 py-2', page_html)[1:]:
        link = re.search(r'href="(/b/hotdeal/view/[^"]+)"', row)
        title_match = re.search(r'text-\[17px\][^>]*>(.*?)</b>', row, re.S)
        if not link or not title_match:
            continue
        title = normalize_space(strip_tags(title_match.group(1)))
        post_id = re.search(r"-(\d+)$", link.group(1))
        if not title or not post_id:
            continue
        price_match = re.search(r'font-bold text-comment">([\d,]+)원<', row)
        labels = re.search(r'class="label-s[^"]*">(.*?)</div>', row, re.S)
        label_text = normalize_space(strip_tags(labels.group(1))) if labels else ""
        store = ""
        for piece in label_text.split("·"):
            piece = piece.strip()
            if piece and "원" not in piece and "배송" not in piece:
                store = piece
                break
        comment_match = re.search(r'text-comment">\(<!-- -->(\d+)<!-- -->\)</span>', row)
        like_match = re.search(r"lucide-heart[^>]*>.*?</svg>(\d+)</span>", row, re.S)
        view_match = re.search(r"lucide-eye[^>]*>.*?</svg>(\d+)</span>", row, re.S)
        meta = re.search(r'class="caption-m[^>]*text-etoText-4[^"]*">(.*?)</div>', row, re.S)
        meta_parts = [normalize_space(strip_tags(part)) for part in re.findall(r"<span[^>]*>(.*?)</span>", meta.group(1), re.S)] if meta else []
        meta_parts = [part for part in meta_parts if part and part != "|"]
        price = as_int(price_match.group(1)) if price_match else None
        shipping = "무료" if "무료배송" in label_text else ""
        # 목록에는 제목과 가격이 따로 있으므로 뽐뿌식 제목으로 합쳐 준다.
        raw_title = title if title.startswith("[") else f"[{store}] {title}" if store else title
        if price:
            raw_title += f" ({price:,}원)" if not shipping else f" ({price:,}원/{shipping})"
        items.append({
            "id": f"etoland-{post_id.group(1)}",
            "source": "etoland",
            "source_name": SOURCE_NAMES["etoland"],
            "raw_title": raw_title,
            "product_url": "https://etoland.co.kr" + link.group(1),
            "description": "",
            "author": "",
            "published": meta_parts[1] if len(meta_parts) > 1 else "",
            "recommendations": as_int(like_match.group(1)) if like_match else 0,
            "comments": as_int(comment_match.group(1)) if comment_match else 0,
            "views": as_int(view_match.group(1)) if view_match else None,
            "image_url": (re.search(r'class="item-image[^"]*"><img[^>]*data-src="([^"]+)"', row).group(1)
                          if re.search(r'class="item-image[^"]*"><img[^>]*data-src="([^"]+)"', row) else ""),
        })
    return items


def collect_etoland(timeout: float) -> List[Dict[str, Any]]:
    text = fetch(ETOLAND_LIST, timeout=timeout)
    if "핫딜 게시판" not in text:
        raise RuntimeError("이토랜드 핫딜 페이지가 아님(차단/변경 의심)")
    items = parse_etoland(text)
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
    if raw["source"] in LIST_PRICE_SOURCES:
        price_note = "게시판 목록 표기 가격"
    if price is None:
        return None, "가격 표기 없음"
    if price < MIN_PRICE or price > MAX_PRICE:
        return None, f"가격 범위 밖({price:,}원)"
    mall, product = split_mall(title)
    if not product:
        return None, "상품명 없음"
    if len(product_key(product)) < 4:
        return None, "상품명이 너무 짧음"

    rule = SOURCE_RULES.get(raw["source"], {})
    recs = raw.get("recommendations") or 0
    comments = raw.get("comments") or 0
    views = raw.get("views") or 0
    if rule.get("price_only"):
        # 추천·댓글이 없는 RSS 피드: 상품명이 충분히 구체적인 경우만 받는다.
        if len(product_key(product)) < int(rule.get("min_name_len", 8)):
            return None, "추천 정보 없음·상품명이 짧음"
    else:
        min_recs = env_int(f"MIN_RECS_{raw['source'].upper()}", int(rule.get("min_recs", 3)))
        min_comments = env_int(f"MIN_COMMENTS_{raw['source'].upper()}", int(rule.get("min_comments", min_recs * 3)))
        min_views = env_int(f"MIN_VIEWS_{raw['source'].upper()}", int(rule.get("min_views", 0)))
        passed = recs >= min_recs or comments >= min_comments or (min_views and views >= min_views)
        if not passed:
            return None, (
                f"추천 {recs}·댓글 {comments}" + (f"·조회 {views}" if min_views else "")
                + f" (기준 추천 {min_recs}·댓글 {min_comments}" + (f"·조회 {min_views}" if min_views else "") + ")"
            )

    deal_id = raw["id"]
    previous = history.get(deal_id, {})
    previous_lowest = previous.get("lowest_price")
    lowest = previous_lowest if previous_lowest and previous_lowest < price else price
    is_new_low = bool(previous_lowest and price < previous_lowest)

    reasons = []
    if recs:
        reasons.append(f"{mall + ' 판매 · ' if mall else raw['source_name'] + ' '}커뮤니티 추천 {recs}건"
                       + (f" · 댓글 {comments}건" if comments else ""))
    elif views:
        reasons.append(f"{mall + ' 판매 · ' if mall else raw['source_name'] + ' '}조회 {views:,}회"
                       + (f" · 댓글 {comments}건" if comments else ""))
    else:
        reasons.append(f"{mall + ' 판매 · ' if mall else ''}{raw['source_name']} 신규 글")
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


def pick_balanced(accepted: List[Dict[str, Any]], max_new: int, per_source_max: int) -> List[Dict[str, Any]]:
    """소스가 한 곳에 몰리지 않게 한 소스당 per_source_max 건까지 번갈아 고른다."""
    if per_source_max <= 0:
        return accepted[:max_new]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in accepted:
        groups.setdefault(candidate["source"], []).append(candidate)
    order = sorted(groups, key=lambda name: (
        int(groups[name][0]["community"].get("recommendations") or 0),
        int(groups[name][0]["community"].get("comments") or 0),
    ), reverse=True)
    chosen: List[Dict[str, Any]] = []
    for round_index in range(per_source_max):
        for name in order:
            bucket = groups[name]
            if round_index < len(bucket) and len(chosen) < max_new:
                chosen.append(bucket[round_index])
    if len(chosen) < max_new:
        # 남은 자리는 소스 상한을 지키면서 채우고, 그래도 모자라면 상한을 풀어 마저 채운다.
        picked = {id(item) for item in chosen}
        counts: Dict[str, int] = {}
        for item in chosen:
            counts[item["source"]] = counts.get(item["source"], 0) + 1
        for candidate in accepted:
            if len(chosen) >= max_new:
                break
            if id(candidate) in picked or counts.get(candidate["source"], 0) >= per_source_max:
                continue
            chosen.append(candidate)
            picked.add(id(candidate))
            counts[candidate["source"]] = counts.get(candidate["source"], 0) + 1
        for candidate in accepted:
            if len(chosen) >= max_new:
                break
            if id(candidate) not in picked:
                chosen.append(candidate)
                picked.add(id(candidate))
    return chosen


def collect_all(timeout: float) -> List[Dict[str, Any]]:
    collected: List[Dict[str, Any]] = []
    collectors = (
        ("ppomppu", collect_ppomppu),
        ("ppomppu_pmarket", collect_ppomppu_pmarket),
        ("clien", collect_clien),
        ("etoland", collect_etoland),
        ("dealbada", collect_dealbada),
        ("eomisae", collect_eomisae),
    )
    for name, func in collectors:
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
            int(c["community"].get("views") or 0),
        ),
        reverse=True,
    )
    chosen = pick_balanced(accepted, max_new, env_int("PER_SOURCE_MAX", PER_SOURCE_MAX))
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


ETOLAND_SAMPLE = """<ul><li class="flex gap-3 px-4 py-2 hover:bg-[#feffd792]"><a class="group flex w-full gap-3 overflow-hidden" href="/b/hotdeal/view/%EC%84%A0%EC%B0%A9%EC%88%9C%ED%8A%B9%EA%B0%80-%ED%8D%BC%EB%8B%88%ED%8A%B8-9599592"><div class="item-image h-16 w-20 min-w-20 overflow-hidden"><img data-src="https://btcdn.etoland.co.kr/static/media/board/a.png" alt="x"/></div><div class="flex flex-1 flex-col overflow-hidden"><span class="inline-flex items-center gap-1 font-medium"><b class="max-w-fit flex-1 truncate font-semibold text-[17px] text-pressed">선착순특가) 퍼니트 역가수치 파인애플 브로멜라인 효소 3g 100포</b><span class="caption-m font-bold text-comment">(<!-- -->10<!-- -->)</span></span><div class="label-s flex flex-wrap items-center gap-1.5 text-etoText-2"><span class="body-m font-bold text-comment">24,270원</span><span class="text-etoText-3">·</span><span class="inline-flex items-center gap-0.5">토스</span><span class="text-etoText-3">·</span><span class="inline-flex items-center gap-0.5">무료배송</span></div><div class="caption-m flex flex-wrap items-center gap-1 text-etoText-4"><span>식품</span><span class="text-etoDivider-1">|</span><span>1시간전</span><span class="text-etoDivider-1">|</span><span class="text-etoText-3">토스쇼핑</span><span class="text-etoDivider-1">|</span><span class="inline-flex items-center gap-0.5"><svg class="lucide lucide-heart size-3.5"><path d="M2 9.5"></path></svg>7</span><span class="text-etoDivider-1">|</span><span class="inline-flex items-center gap-0.5"><svg class="lucide lucide-eye size-3.5"><circle cx="12"></circle></svg>281</span></div></div></a></li>
<li class="flex gap-3 px-4 py-2 hover:bg-[#feffd792]"><a class="group flex w-full gap-3 overflow-hidden" href="/b/hotdeal/view/%EB%84%A4%EC%9D%B4%EB%B2%84%ED%8E%98%EC%9D%B4-9599500"><div class="flex flex-1 flex-col overflow-hidden"><span class="inline-flex items-center gap-1 font-medium"><b class="max-w-fit flex-1 truncate font-semibold text-[17px] text-pressed">[네이버페이] 일일적립, 클릭 3원</b><span class="caption-m font-bold text-comment">(<!-- -->20<!-- -->)</span></span><div class="label-s flex flex-wrap items-center gap-1.5 text-etoText-2"></div><div class="caption-m flex flex-wrap items-center gap-1 text-etoText-4"><span>포인트</span><span class="text-etoDivider-1">|</span><span>3시간전</span></div></div></a></li></ul>"""

DEALBADA_SAMPLE = """<table><tr class="">
<td class="td_num">
33315            </td>
<td class="td_name sv_use"><span class="sv_wrap"><a href="//www.dealbada.com/bbs/profile.php?mb_nick=x" class="sv_member"><div class="div_nickname"> 운수저</div></a></span></td>
<td class="td_cate"><a href="//www.dealbada.com/bbs/board.php?bo_table=deal_domestic&amp;sca=x" class="bo_cate_link">식품/건강</a></td>
<td class="td_img"><a href="//www.dealbada.com/bbs/board.php?bo_table=deal_domestic&amp;wr_id=572893&page=1"><img src='http://cdn.dealbada.com/a.jpg' width=50 height=40></a></td>
<td class="td_subject"><a href="//www.dealbada.com/bbs/board.php?bo_table=deal_domestic&amp;wr_id=572893&page=1" >[네이버] 블랙모어스 프로폴리스 4병 (39,900원/무료)</a></td>
<td class="td_date">00:13</td>
<td class="td_num">1234</td>
<td class="td_num_g" style="color:#000;"><span style="color:#ac3131">0</span> / <span style="color:#1f00b5">0</span></td></tr>
<tr class=""><td class="td_num">1</td><td class="td_cate"><a href="//www.dealbada.com/bbs/board.php?bo_table=deal_domestic&amp;sca=x" class="bo_cate_link">공지</a></td><td class="td_subject"><a href="//www.dealbada.com/bbs/board.php?bo_table=deal_domestic&amp;wr_id=5976&page=1" >딜바다 핫딜 게시판 이용안내</a></td><td class="td_date">01-01</td><td class="td_num">763009</td></tr>
</table>"""

EOMISAE_SAMPLE = """<rss version="2.0"><channel><title>어미새</title><item>
<title>프링글스 치즈맛, 110g, 12개 16,320원</title><link>https://eomisae.co.kr/rt/212068649</link>
<description>프링글스 12통 로켓와우할인입니다</description><category>기타정보</category><category>국내</category>
<pubDate>Sun, 22 Nov 2026 01:19:44 +0900</pubDate></item>
<item><title>[리뷰] 문풍지 850원 무배</title><link>https://eomisae.co.kr/fi2/212067591</link>
<description>.</description><category>기타 결제인증</category><category>국내</category>
<pubDate>Sun, 22 Nov 2026 01:07:55 +0900</pubDate></item>
<item><title>배대지 추천 좀 해주세요 ㅠ</title><link>https://eomisae.co.kr/rt/212060000</link>
<description>시에라 폴로 막혔습니다</description><category>자유게시판</category><category>잡담</category>
<pubDate>Sun, 22 Nov 2026 00:57:26 +0900</pubDate></item>
<item><title>[아마존] 포터프로 (34,000원/무배)</title><link>https://eomisae.co.kr/rt/212050000</link>
<description>해외</description><category>기타정보</category><category>해외</category>
<pubDate>Sun, 22 Nov 2026 00:50:00 +0900</pubDate></item>
</channel></rss>"""


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
    os.environ["MIN_COMMENTS_PPOMPPU"] = "50"
    low, reason = build_candidate(ppomppu[0], dict())
    check("추천 기준 미달 제외", low is None and "추천" in reason, reason)
    os.environ.pop("MIN_RECS_PPOMPPU")
    os.environ.pop("MIN_COMMENTS_PPOMPPU")

    check("상품명 중복 판정", similarity(product_key("국산 면양말 10켤레"), product_key("[G마켓] 국산 면양말 10켤레")) > 0.95)
    check("다른 상품 비중복", similarity(product_key("국산 면양말 10켤레"), product_key("스테인리스 텀블러 600ml")) < 0.7)
    check("정가 숫자 무시", parse_price("[몰] 무선청소기 정가 349,000원 → 199,000원 (무료)")[0] == 199000,
          str(parse_price("[몰] 무선청소기 정가 349,000원 → 199,000원 (무료)")))
    check("개당 가격 무시", parse_price("[몰] 계란 2판 19,800원 (개당 9,900원)")[0] == 19800,
          str(parse_price("[몰] 계란 2판 19,800원 (개당 9,900원)")))

    etoland = parse_etoland(ETOLAND_SAMPLE)
    check("이토랜드 2건 파싱", len(etoland) == 2, str(len(etoland)))
    if etoland:
        row = etoland[0]
        check("이토랜드 id", row["id"] == "etoland-9599592", row["id"])
        check("이토랜드 추천/댓글/조회",
              row["recommendations"] == 7 and row["comments"] == 10 and row["views"] == 281, str(row))
        check("이토랜드 제목 합성", row["raw_title"] == "[토스] 선착순특가) 퍼니트 역가수치 파인애플 브로멜라인 효소 3g 100포 (24,270원/무료)",
              row["raw_title"])
        price, _, shipping = parse_price(row["raw_title"])
        check("이토랜드 가격 파싱", price == 24270 and shipping == "무료배송 표기", f"{price} {shipping}")
        eto_ok, why = build_candidate(row, dict())
        check("이토랜드 후보 통과", eto_ok is not None and why == "통과", why)
        if eto_ok:
            check("이토랜드 상품명", eto_ok["title"].startswith("선착순특가") and eto_ok["price"] == 24270,
                  eto_ok["title"])
    if len(etoland) > 1:
        no_price, why = build_candidate(etoland[1], dict())
        check("가격 없는 포인트 글 제외", no_price is None and why == "가격 표기 없음", why)

    dealbada = parse_dealbada(DEALBADA_SAMPLE)
    check("딜바다 1건 파싱(공지 제외)", len(dealbada) == 1, str(len(dealbada)))
    if dealbada:
        row = dealbada[0]
        check("딜바다 id/조회수", row["id"] == "dealbada-572893" and row["views"] == 1234, str(row))
        check("딜바다 URL", row["product_url"].startswith("https://www.dealbada.com/"), row["product_url"])
        d_ok, why = build_candidate(row, dict())
        check("딜바다 조회 기준 통과", d_ok is not None and why == "통과", why)
        low_view = dict(row, views=120)
        d_low, why = build_candidate(low_view, dict())
        check("딜바다 조회 적은 글 제외", d_low is None and "조회" in why, why)

    eomisae = parse_eomisae(EOMISAE_SAMPLE)
    check("어미새 1건 파싱(결제인증·잡담·해외 제외)", len(eomisae) == 1, str(len(eomisae)))
    if eomisae:
        row = eomisae[0]
        check("어미새 id", row["id"] == "eomisae-212068649", row["id"])
        e_ok, why = build_candidate(row, dict())
        check("어미새 가격만으로 통과", e_ok is not None and why == "통과", why)
        if e_ok:
            check("어미새 가격", e_ok["price"] == 16320 and e_ok["title"].startswith("프링글스"),
                  f"{e_ok['price']} {e_ok['title']}")
        short = dict(row, raw_title="[몰] 양말 (3,900원)")
        e_short, why = build_candidate(short, dict())
        check("어미새 짧은 상품명 제외", e_short is None, why)

    sample_candidates = [
        {"source": "ppomppu", "community": {"recommendations": 9, "comments": 0}},
        {"source": "ppomppu", "community": {"recommendations": 8, "comments": 0}},
        {"source": "ppomppu", "community": {"recommendations": 7, "comments": 0}},
        {"source": "dealbada", "community": {"recommendations": 0, "comments": 0, "views": 900}},
        {"source": "etoland", "community": {"recommendations": 3, "comments": 4}},
    ]
    balanced = pick_balanced(sample_candidates, 3, 2)
    check("괄호 없는 끝가격 제거", clean_title("프링글스 치즈맛, 110g, 12개 16,320원") == "프링글스 치즈맛, 110g, 12개",
          clean_title("프링글스 치즈맛, 110g, 12개 16,320원"))
    check("소스 분산 선택", [c["source"] for c in balanced] == ["ppomppu", "etoland", "dealbada"],
          str([c["source"] for c in balanced]))
    check("한 소스 상한", sum(1 for c in pick_balanced(sample_candidates, 4, 2) if c["source"] == "ppomppu") == 2,
          str(pick_balanced(sample_candidates, 4, 2)))
    uneven = [
        {"source": "etoland", "community": {"recommendations": 9}},
        {"source": "etoland", "community": {"recommendations": 8}},
        {"source": "etoland", "community": {"recommendations": 7}},
        {"source": "dealbada", "community": {"recommendations": 0}},
        {"source": "dealbada", "community": {"recommendations": 0}},
        {"source": "eomisae", "community": {"recommendations": 0}},
    ]
    uneven_pick = pick_balanced(uneven, 5, 2)
    check("빈자리 채울 때도 소스 상한", sum(1 for c in uneven_pick if c["source"] == "etoland") <= 2,
          str([c["source"] for c in uneven_pick]))

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
