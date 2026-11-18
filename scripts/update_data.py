#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
광주·전남 자동 일정 수집기
1) docs/index.html 안의 기존 수동 일정을 안전하게 읽음
2) 한국관광공사 TourAPI 행사정보 + KOPIS 공연목록 수집
3) 수동 일정을 우선 보존하면서 API 데이터 병합
4) docs/data/events.json, meta.json 생성

중요: 실제 API 인증키는 코드에 넣지 않고 GitHub Secrets에서만 읽습니다.
"""

from __future__ import annotations
import csv, hashlib, io, html, math, os, json, re, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "index.html"
DATA_DIR = ROOT / "docs" / "data"
OUT_EVENTS = DATA_DIR / "events.json"
OUT_META = DATA_DIR / "meta.json"

DATA_KEY = urllib.parse.unquote( os.environ.get("DATA_GO_KR_KEY", "").strip())
KOPIS_KEY = os.environ.get("KOPIS_API_KEY", "").strip()
SAFETY_KEY = os.environ.get("SAFETY_DATA_KEY", "").strip()        # 재난안전데이터 공유플랫폼(safetydata.go.kr) 한파쉼터 키
SAFETY_HEAT_KEY = os.environ.get("SAFETY_HEAT_KEY", "").strip() or SAFETY_KEY   # 무더위쉼터 키(따로 받았으면)

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
# 행사 검색 범위: 오늘 기준으로 자동 계산 (연도가 바뀌어도 그대로 동작)
TOUR_FROM = (NOW - timedelta(days=200)).strftime("%Y%m%d")   # 오래전에 시작해 아직 진행 중인 행사까지
TOUR_TO = (NOW + timedelta(days=365)).strftime("%Y%m%d")
DETAIL_TTL_DAYS = 7   # 상세정보는 7일에 한 번만 다시 받음 (API 하루 호출 한도 절약)
KOPIS_FROM = NOW.strftime("%Y%m%d")
KOPIS_TO = (NOW + timedelta(days=30)).strftime("%Y%m%d")
TODAY_ISO = NOW.strftime("%Y-%m-%d")

# TourAPI 지역코드: 광주광역시 5 / 전라남도 38
TOUR_AREAS = [("광주", "5")]

# KOPIS 지역(시도)코드: 광주광역시 29 / 전라남도 46
KOPIS_AREAS = [("광주", "29"), ("전남", "46")]

UA = "gwangju-eodi/1.0 GitHubActions"

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

EMBED_JS = ROOT / "docs" / "embedded-data.js"

def _embedded_text():
    """내장 자료(window.__DATA__=...)가 든 파일 내용을 돌려준다.
    예전엔 index.html 안에 있었고, 지금은 docs/embedded-data.js 로 따로 빠져 있다. 둘 다 찾아본다."""
    for f in (INDEX, EMBED_JS):
        try:
            t = f.read_text(encoding="utf-8")
        except Exception:
            continue
        if re.search(r"window\.__DATA__\s*=\s*\{", t):   # 앱 코드 속 'window.__DATA__ &&' 는 건너뜀
            return t
    return ""

def embedded_data():
    html = _embedded_text()
    m = re.search(r"window\.__DATA__\s*=\s*(?=\{)", html)
    if not m:
        print("  ⚠ 내장 자료(window.__DATA__)를 index.html·embedded-data.js 어디서도 못 찾았어요", flush=True)
        return {}
    d, _ = json.JSONDecoder().raw_decode(html[m.end():])
    return d

def embedded_manual_events():
    """현재 index.html(또는 embedded-data.js)에 들어 있는 수동 데이터를 읽는다."""
    html = _embedded_text()
    m = re.search(r"window\.__DATA__\s*=\s*(?=\{)", html)
    if not m:
        return []
    i = m.end()

    # JSON 객체의 중괄호 균형을 직접 추적한다.
    depth = 0
    in_string = False
    escape = False
    start = None
    end = None
    for pos in range(i, len(html)):
        ch = html[pos]
        if start is None:
            if ch == "{":
                start = pos
                depth = 1
            continue
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = pos + 1
                break

    if start is None or end is None:
        return []
    data = json.loads(html[start:end])
    # 기존 수동작성 자료만 원본으로 사용한다.
    return [e for e in data.get("events", []) if e.get("origin", "manual") == "manual"]

def to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None

# ── 지난번 결과(캐시) · 앱의 장소 목록 ──
PREV = {}            # id → 지난번 이벤트
VENUES = []          # index.html 안의 장소 목록 (행사↔장소 연결용)

def fresh_cache(event_id):
    """지난번에 받은 같은 행사의 상세정보가 DETAIL_TTL_DAYS 안이면 복사본을 돌려준다."""
    e = PREV.get(event_id)
    if not e or not e.get("detailAt"):
        return None
    try:
        age = (NOW.date() - datetime.strptime(e["detailAt"], "%Y-%m-%d").date()).days
    except ValueError:
        return None
    if age > DETAIL_TTL_DAYS:
        return None
    e = dict(e)
    e["verified"] = TODAY_ISO
    if e.get("url"):
        e["url"] = html.unescape(e["url"])          # 예전 저장본의 '&amp;' 깨진 링크도 바로잡음
    if not e.get("district"):
        e["district"] = district_hint(e.get("title", ""), e.get("venue", "")) or url_district(e.get("url", ""))
    return e

def match_venue(name, addr=""):
    """'전남광주통합특별시 예술의전당 (구. 광주예술의전당)' → 앱의 'artcenter' 장소로 연결"""
    n = norm_title(name)
    if len(n) < 3:
        return None
    best = None
    ad = district_from_addr(addr)
    for v in VENUES:
        vd = v.get("district") or district_from_addr(v.get("address", ""))
        if ad and vd and ad != vd:
            continue                              # 이름이 비슷해도 다른 시·군이면 다른 곳 ('영암군립 하정웅미술관' ≠ 광주 '하정웅미술관')
        names = [v.get("name", "")] + (v.get("aliases") or [])
        for cand in names:
            c = norm_title(cand)
            if len(c) < 4:
                continue
            if c == n:
                score = 10000                     # 이름이 정확히 같음
            elif c in n:
                score = len(c)                    # '…예술의전당 (구. 광주예술의전당)' 안에 장소명이 들어 있음
            elif n in c:
                score = len(n) - 1000             # 장소명이 더 긺(분관 등) → 다른 후보가 없을 때만
            else:
                continue
            if not best or score > best[0]:
                best = (score, v.get("id"))
    return best[1] if best else None

def scrub(text) -> str:
    """공개되는 meta.json 에 인증키가 절대 찍히지 않게 지운다."""
    t = str(text)
    for k in {DATA_KEY, KOPIS_KEY, SAFETY_KEY, SAFETY_HEAT_KEY, urllib.parse.quote(DATA_KEY or "", safe=""), urllib.parse.quote_plus(DATA_KEY or "")}:
        if k and len(k) > 8:
            t = t.replace(k, "***")
    return re.sub(r"(serviceKey|ServiceKey|service)=[^&\s]+", r"\1=***", t)


def http_get(url: str, timeout=20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        # 서버가 보낸 설명(어떤 변수가 틀렸는지 등)을 같이 남겨서 원인을 바로 알 수 있게
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"serviceKey=[^&\s]+", "serviceKey=***", re.sub(r"\s+", " ", body)).strip()
        raise RuntimeError(scrub(f"HTTP {e.code} {e.reason} · {body[:220]}")) from None


# ── 오래 걸리지 않게 막는 장치 ──
SOURCE_BUDGET = 6 * 60      # 출처 하나당 최대 6분. 넘으면 그때까지 받은 것만 쓰고 다음으로
_FAILS = {}


def log(*a):
    print(*a, flush=True)


def guarded(name, fn, *a, **k):
    """상세정보 호출이 3번 연속 실패하면, 이번 실행에선 그 상세정보를 더 묻지 않는다(멈춤·지연 방지)."""
    if _FAILS.get(name, 0) >= 3:
        raise RuntimeError(f"{name} 상세 건너뜀")
    try:
        r = fn(*a, **k)
        _FAILS[name] = 0
        return r
    except Exception:
        _FAILS[name] = _FAILS.get(name, 0) + 1
        if _FAILS[name] == 3:
            log(f"  ⚠ {name} 상세정보가 계속 실패해 이번엔 목록 정보만 씁니다")
        raise

def ymd(v):
    s = re.sub(r"\D", "", str(v or ""))
    if len(s) >= 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None

GJ_GU = ("동구", "서구", "남구", "북구", "광산구")
SIDO_RE = r"(?:전남광주통합특별시|전남광주특별시|광주광역시|전라남도|광주시|광주|전남)"

def district_from_addr(addr: str):
    """'전남광주통합특별시 북구 …' → '북구', '전남광주통합특별시 여수시 …' → '여수시'"""
    a = addr or ""
    m = re.search(SIDO_RE + r"\s*([가-힣]{1,4}(?:구|시|군))(?=\s|$|,|\))", a)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|\s)([가-힣]{1,4}(?:시|군)|광산구)(?=\s|$)", a)
    return m.group(1) if m and not re.fullmatch(SIDO_RE, m.group(1)) else ""

# 주소가 비어 온 공연장: 이름으로 시·군을 알아낸다 (KOPIS 공연장 주소 조회가 자주 실패함)
VENUE_DISTRICT = [
    (r"한국트로트가요센터", "영암군"), (r"남도소리울림터", "무안군"), (r"목포시민문화체육센터", "목포시"),
    (r"예울마루|여수시민회관|여수월드", "여수시"), (r"포스코광양|백운아트홀", "광양시"),
    (r"순천문화예술회관", "순천시"), (r"담양문화회관", "담양군"), (r"소록도", "고흥군"),
    (r"5\.18기념문화센터|광주서구문화센터|상무지구", "서구"), (r"국립아시아문화전당|빛고을시민문화관|광주디자인센터", "동구"),
]
CITY_DISTRICT = {"여수": "여수시", "순천": "순천시", "광양": "광양시", "목포": "목포시", "무안": "무안군", "영암": "영암군",
                 "담양": "담양군", "나주": "나주시", "화순": "화순군", "장성": "장성군", "해남": "해남군", "완도": "완도군",
                 "고흥": "고흥군", "보성": "보성군", "구례": "구례군", "곡성": "곡성군", "강진": "강진군", "장흥": "장흥군",
                 "함평": "함평군", "영광": "영광군", "진도": "진도군", "신안": "신안군"}


# 장소·주소가 비어 온 행사: 원문 링크의 누리집으로 시·군을 알아낸다
URL_DISTRICT = [
    (r"acc\.go\.kr", "동구"), (r"sorokdo\.go\.kr", "고흥군"), (r"artmuse\.gwangju\.go\.kr|gmap\.gwangju", "북구"),
    (r"gwangju\.museum\.go\.kr", "북구"), (r"yeosu\.go\.kr", "여수시"), (r"suncheon\.go\.kr", "순천시"),
    (r"mokpo\.go\.kr", "목포시"), (r"muan\.go\.kr", "무안군"), (r"goheung\.go\.kr", "고흥군"),
]


def url_district(url: str) -> str:
    for pat, d in URL_DISTRICT:
        if re.search(pat, url or "", flags=re.IGNORECASE):
            return d
    return ""


def district_hint(title: str, venue: str) -> str:
    """주소가 없을 때: ① 공연장 이름 ② 제목 끝 [여수]·[광양] 같은 지역 표시 순서로 시·군을 찾는다."""
    for pat, d in VENUE_DISTRICT:
        if re.search(pat, f"{venue} {title}"):
            return d
    for tag in re.findall(r"\[([^\]]+)\]", title or ""):
        for city, d in CITY_DISTRICT.items():
            if city in tag:
                return d
    return ""


def in_region(text: str):
    t = text or ""
    if re.search(r"경기도|경기\s*광주|^경기", t):     # 경기도 광주시는 다른 도시
        return False
    return bool(re.search(r"광주|전남|전라남", t))

def region_tag(district: str):
    return "광주" if district in GJ_GU else "전남"

def norm_title(s: str):
    return re.sub(r"[^0-9a-z가-힣]", "", (s or "").lower())

def dedup_key(e):
    api_id = str(e.get("apiId") or "").strip()
    if api_id:
        return f"{e.get('origin','api')}:{api_id}"
    return f"{norm_title(e.get('title',''))}:{e.get('start','')}"
def clean_api_text(value):
    if not value:
        return ""

    text = str(value)
    text = re.sub(r"<br\s*/?>", " / ", text, flags=re.IGNORECASE)
    # 진짜 HTML 태그만 지움 ('<집 그리고 또 다른 장소들>' 같은 제목 속 꺾쇠는 살림)
    text = re.sub(r"</?(?:p|span|a|b|strong|em|i|u|font|div|img|ul|ol|li|h[1-6]|table|tr|td|th|tbody|thead|sup|sub)\b[^>]*>", "", text, flags=re.IGNORECASE)
    for _ in range(2):                                     # &amp;lt; 처럼 두 번 겹친 것까지
        text = html.unescape(text)
    text = text.replace("\xa0", " ")                      # &middot; &#39; &amp; 등을 원래 글자로
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def extract_api_url(value):
    if not value:
        return ""

    text = str(value).strip()

    match = re.search(r'href=["\']([^"\']+)["\']', text, flags=re.IGNORECASE)
    if match:
        text = match.group(1).strip()
    # API가 주소를 HTML 그대로 줘서 '&amp;'가 남으면 링크가 깨진다 (예: ACC·소록도박물관 게시판 링크)
    return html.unescape(text).strip()

def tourapi_detail_intro(content_id, content_type_id="15"):
    params = {
        "serviceKey": DATA_KEY,
        "MobileOS": "ETC",
        "MobileApp": "gwangju-eodi",
        "_type": "json",
        "contentId": content_id,
        "contentTypeId": content_type_id,
    }

    url = (
        "https://apis.data.go.kr/B551011/KorService2/detailIntro2?"
        + urllib.parse.urlencode(params)
    )

    raw = http_get(url)
    j = json.loads(raw.decode("utf-8"))

    resp = (j or {}).get("response") or {}
    body = resp.get("body") or {}
    items = (body.get("items") or {}).get("item") or []

    if isinstance(items, list):
        return items[0] if items else {}

    if isinstance(items, dict):
        return items

    return {}
def tourapi_detail_common(content_id, content_type_id="15"):
    params = {
        "serviceKey": DATA_KEY,
        "MobileOS": "ETC",
        "MobileApp": "gwangju-eodi",
        "_type": "json",
        "contentId": content_id,
        "contentTypeId": content_type_id,
        "defaultYN": "Y",
        "firstImageYN": "Y",
        "areacodeYN": "Y",
        "catcodeYN": "N",
        "addrinfoYN": "Y",
        "mapinfoYN": "Y",
        "overviewYN": "Y",
    }

    url = (
        "https://apis.data.go.kr/B551011/KorService2/detailCommon2?"
        + urllib.parse.urlencode(params)
    )

    raw = http_get(url)
    j = json.loads(raw.decode("utf-8"))

    resp = (j or {}).get("response") or {}
    body = resp.get("body") or {}
    items = (body.get("items") or {}).get("item") or []

    if isinstance(items, list):
        return items[0] if items else {}

    if isinstance(items, dict):
        return items

    return {} 
def tourapi_detail_images(content_id):
    params = {
        "serviceKey": DATA_KEY,
        "MobileOS": "ETC",
        "MobileApp": "gwangju-eodi",
        "_type": "json",
        "contentId": content_id,
        "imageYN": "Y",
        "subImageYN": "Y",
    }

    url = (
        "https://apis.data.go.kr/B551011/KorService2/detailImage2?"
        + urllib.parse.urlencode(params)
    )

    raw = http_get(url)
    j = json.loads(raw.decode("utf-8"))

    resp = (j or {}).get("response") or {}
    body = resp.get("body") or {}
    items = (body.get("items") or {}).get("item") or []

    if isinstance(items, dict):
        items = [items]

    return items if isinstance(items, list) else []
def tourapi_festivals():
    if not DATA_KEY:
        raise RuntimeError("DATA_GO_KR_KEY가 없습니다.")

    out, seen_ids = [], set()
    num_rows = 100
    deadline = time.time() + SOURCE_BUDGET

    # 전국을 받아 주소로 거르면 6분 안에 다 못 읽어 (가나다순 뒤쪽 축제가 빠짐)
    # → 광주(29)·전남(46)만 지역 코드로 받는다. 지역 조회가 통째로 비면 예전 방식(전국)으로 한 번 더.
    passes = [("광주", {"lDongRegnCd": "29"}), ("전남", {"lDongRegnCd": "46"}), ("전국", {})]
    for pass_name, extra in passes:
        if pass_name == "전국" and out:
            break
        page_no = 1
        try:
            _tourapi_pass(extra, pass_name, num_rows, deadline, out, seen_ids)
        except Exception as ex:
            log(f"  관광공사 {pass_name} 조회 실패: {ex}")
            if pass_name == "전국" and not out:
                raise
    return out


def _tourapi_pass(extra, pass_name, num_rows, deadline, out, seen_ids):
    page_no = 1

    while time.time() < deadline:
        params = {
            "serviceKey": DATA_KEY,
            "MobileOS": "ETC",
            "MobileApp": "gwangju-eodi",
            "_type": "json",
            "numOfRows": str(num_rows),
            "pageNo": str(page_no),
            "arrange": "A",
            "eventStartDate": TOUR_FROM,
            "eventEndDate": TOUR_TO,
            **extra,
        }

        url = (
            "https://apis.data.go.kr/B551011/KorService2/searchFestival2?"
            + urllib.parse.urlencode(params)
        )

        raw = http_get(url)
        j = json.loads(raw.decode("utf-8"))

        resp = (j or {}).get("response") or {}
        header = resp.get("header") or {}

        if str(header.get("resultCode")) not in ("0000", "0"):
            raise RuntimeError(f"TourAPI 오류: {header}")

        body = resp.get("body") or {}
        items = (body.get("items") or {}).get("item") or []
        total_count = int(body.get("totalCount") or 0)

        if isinstance(items, dict):
            items = [items]

        if not items:
            break

        for x in items:
            title = clean_api_text(x.get("title"))
            if not title:
                continue

            start = ymd(x.get("eventstartdate"))
            end = ymd(x.get("eventenddate")) or start
            addr = (x.get("addr1") or "").strip()

            if not in_region(addr):
                continue
            if end and end < TODAY_ISO:
                continue          # 이미 끝난 행사는 상세정보를 받지 않음 (호출 한도 절약)

            venue_raw = (x.get("addr2") or "").strip()
            venue = (
                venue_raw
                if venue_raw and venue_raw not in addr
                else (district_from_addr(addr) or "광주·전남")
            )

            content_id = str(x.get("contentid") or "")
            if content_id in seen_ids:
                continue
            seen_ids.add(content_id)

            # 지난번에 받은 상세정보가 아직 신선하면 재사용 (호출 한도 절약)
            cached = fresh_cache(f"tour-{content_id}")
            if cached:
                cached.update({
                    "title": title, "start": start, "end": end,
                    "date": f"{start or ''} ~ {end or ''}".strip(" ~"),
                    "status": "예정" if start and start > TODAY_ISO else "진행중",
                    "origin": "auto", "api": "tourapi",
                })
                out.append(cached)
                continue

            slow = time.time() > deadline - 60     # 시간이 빠듯하면 상세는 생략
            try:
                intro = {} if slow else guarded("관광공사", tourapi_detail_intro, content_id)
            except Exception:
                intro = {}

            try:
                common = {} if slow else guarded("관광공사", tourapi_detail_common, content_id)
            except Exception:
                common = {}

            try:
                images = [] if slow else guarded("관광공사", tourapi_detail_images, content_id)
            except Exception:
                images = []

            out.append({
                "detailAt": TODAY_ISO,
                "id": f"tour-{content_id}",
                "apiId": content_id,
                "category": "축제",
                "subtype": "관광공사 행사",
                "title": title,
                "venue": (intro.get("eventplace") or venue).strip(),
                "district": district_from_addr(addr),
                "address": addr,
                "date": f"{start or ''} ~ {end or ''}".strip(" ~"),
                "time": clean_api_text(intro.get("playtime")) or "공식 상세 확인",
                "price": clean_api_text(intro.get("usetimefestival")) or "공식 상세 확인",
                "age": clean_api_text(intro.get("agelimit")),
                "program": clean_api_text(intro.get("program")),
                "booking": clean_api_text(intro.get("bookingplace")),
                "parking": clean_api_text(intro.get("parking")),
                "spendtime": clean_api_text(intro.get("spendtimefestival")),
                "sponsor": clean_api_text(
                    intro.get("sponsor1") or intro.get("sponsor2")
                ),
                "status": "예정" if start and start > TODAY_ISO else "진행중",
                "start": start,
                "end": end,
                "image": (
                    x.get("firstimage")
                    or x.get("firstimage2")
                    or common.get("firstimage")
                    or common.get("firstimage2")
                    or (images[0].get("originimgurl") if images else "")
                    or ""
                ),
                "lat": to_float(x.get("mapy")),
                "lng": to_float(x.get("mapx")),
                "contact": (
                    intro.get("sponsor1tel")
                    or intro.get("sponsor2tel")
                    or x.get("tel")
                    or ""
                ),
                "description": clean_api_text(common.get("overview")),
                "source": "한국관광공사 TourAPI",
                "url": extract_api_url(
                    intro.get("eventhomepage") or common.get("homepage")
                ),
                "origin": "auto",          # 앱이 '자동수집'으로 인식하는 값
                "api": "tourapi",
                "venueId": match_venue(intro.get("eventplace") or venue, addr),
                "verified": TODAY_ISO,
                "tags": [
                    "축제",
                    region_tag(district_from_addr(addr)),
                ],
            })

        log(f"  관광공사 {pass_name} {page_no}쪽 · {total_count:,}건 조회 · 광주·전남 누적 {len(out)}건")
        # 현재 페이지까지 다 읽었으면 종료
        if page_no * num_rows >= total_count:
            break

        page_no += 1


# ───────── 한국문화정보원 「한눈에보는문화정보」: 전시·공연 / 행사·축제 / 교육·체험 ─────────
# 공공데이터포털에서 '한국문화정보원_한눈에보는문화정보조회서비스' 활용신청 필요 (같은 인증키 사용)
# 공공데이터포털 End Point (사용자 계정 화면에서 확인: 2026-10-09)
CULTURE_BASE = "https://apis.data.go.kr/B553457/cultureinfo"
# 세부 기능 이름은 버전에 따라 다를 수 있어 순서대로 시도하고, 성공한 이름을 기억한다.
CULTURE_OPS = {"period": ["period2", "period"], "area": ["area2", "area"], "detail": ["detail2", "detail"]}
CULTURE_SIDO = ["광주", "전남"]          # 지역별 조회(area2): 광주·전남 것만 받아서 전국 목록에 묻혀 빠지는 일이 없게
CULTURE_BUDGET = 10 * 60
_CULTURE_OK = {}
CULTURE_TYPES = {"A": "전시·공연", "B": "행사·축제", "C": "교육·체험"}


def json_records(j):
    """JSON 응답에서 title 을 가진 항목들을 찾는다."""
    out = []
    def walk(o):
        if isinstance(o, dict):
            if "title" in o and not isinstance(o["title"], (dict, list)):
                out.append({k: ("" if v is None else str(v)) for k, v in o.items() if not isinstance(v, (dict, list))})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(j)
    return out


def xml_records(root):
    """응답 구조가 조금 달라도 '제목(title)을 가진 묶음'을 한 건으로 읽는다."""
    out = []
    for el in root.iter():
        kids = list(el)
        if kids and any(k.tag == "title" for k in kids):
            out.append({k.tag: (k.text or "").strip() for k in kids})
    return out


def _culture_once(op, params):
    raw = http_get(f"{CULTURE_BASE}/{op}?" + urllib.parse.urlencode(params))
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("{") or text.startswith("["):
        j = json.loads(text)
        hdr = (j.get("response") or {}).get("header") or j.get("header") or {} if isinstance(j, dict) else {}
        code = str(hdr.get("resultCode", "") or "")
        recs = json_records(j)
        if code and code not in ("00", "0000", "0") and not recs:
            raise RuntimeError(f"문화정보원 오류 {code} {hdr.get('resultMsg', '')}".strip())
        return recs
    root = ET.fromstring(raw)
    code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or ""
    msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or ""
    recs = xml_records(root)
    if code and code not in ("00", "0000", "0") and not recs:
        raise RuntimeError(f"문화정보원 오류 {code} {msg}".strip())
    return recs


# 페이지 변수 이름 표기가 서비스마다 달라서, 되는 방식을 찾으면 기억해 둔다
CULTURE_PAGING = [("PageNo", "numOfrows"), ("pageNo", "numOfRows"), ("cPage", "rows")]
_CULTURE_STYLE = {}


def culture_xml(kind, **params):
    """kind: 'period' 또는 'detail'. 레코드 목록을 돌려준다."""
    page, rows = params.pop("cPage", None), params.pop("rows", None)
    ops = [_CULTURE_OK[kind]] if kind in _CULTURE_OK else CULTURE_OPS[kind]
    styles = [_CULTURE_STYLE[kind]] if kind in _CULTURE_STYLE else CULTURE_PAGING
    tries = []
    for op in ops:
        for st in styles:
            # serviceTp 가 없는 버전도 한 번 시도
            for drop_tp in ((False, True) if "serviceTp" in params and kind not in _CULTURE_STYLE else (False,)):
                q = {"serviceKey": DATA_KEY, **{k: v for k, v in params.items() if not (drop_tp and k == "serviceTp")}}
                if page is not None:
                    q[st[0]], q[st[1]] = page, rows or "100"
                try:
                    recs = _culture_once(op, q)
                    _CULTURE_OK[kind], _CULTURE_STYLE[kind] = op, st
                    if kind == "period" and len(tries) and not getattr(culture_xml, "_said", False):
                        log(f"  문화정보원: '{op}' + {st[0]}/{st[1]}{' (serviceTp 없이)' if drop_tp else ''} 방식으로 연결됨")
                        culture_xml._said = True
                    return recs
                except Exception as e:
                    tries.append(f"{op}/{st[0]}{'-tp' if drop_tp else ''}: {str(e)[:120]}")
                    if "404" in str(e):
                        break          # 기능 이름 자체가 없음 → 다음 이름으로
            else:
                continue
            break
    raise RuntimeError(f"문화정보원 {kind} 호출 실패 → " + " | ".join(tries[:3]))


def culture_category(realm, tp):
    r = realm or ""
    if tp == "C" or re.search(r"교육|체험|강좌|강연", r):
        return "행사", "교육·체험"
    if re.search(r"전시|미술|사진", r):
        return "전시", r or "전시"
    if re.search(r"음악|클래식|국악|콘서트|대중", r):
        return "음악", r
    if tp == "B" or re.search(r"축제|행사", r):
        return "축제", r or "행사"
    return "공연", r or "공연"


CULTURE_MODE = ""


def culture_events():
    if not DATA_KEY:
        raise RuntimeError("DATA_GO_KR_KEY가 없습니다.")
    frm, to = NOW.strftime("%Y%m%d"), (NOW + timedelta(days=45)).strftime("%Y%m%d")
    out, seen = [], set()
    t0 = time.time()
    deadline = t0 + CULTURE_BUDGET
    use_area = [True]
    global CULTURE_MODE
    raw_seen = set()                    # 지역 거르기 전 기준: 이번 쪽에 처음 보는 게 하나도 없으면 끝

    def listing(tp, page):
        """지역별(광주·전남) 조회를 먼저 쓰고, 안 되면 예전처럼 기간별 전국 조회"""
        if use_area[0]:
            try:
                recs = []
                for sd in CULTURE_SIDO:
                    recs += culture_xml("area", **{"sido": sd, "from": frm, "to": to, "cPage": str(page), "rows": "100", "serviceTp": tp})
                return recs
            except Exception as e:
                use_area[0] = False
                log(f"  문화정보원 지역별 조회 안 됨 → 기간별 조회 사용 ({scrub(str(e))[:120]})")
        return culture_xml("period", **{"from": frm, "to": to, "cPage": str(page), "rows": "100", "serviceTp": tp})

    pages_read = 0
    for tp, tp_name in CULTURE_TYPES.items():
        for page in range(1, 41):
            pages_read += 1
            if time.time() > deadline:
                log("  문화정보원: 시간 예산 도달 → 여기까지만"); break
            recs = listing(tp, page)
            new_on_page = 0
            for r in recs:
                rs = (r.get("seq") or r.get("id") or "") + "|" + str(r.get("title") or "")
                if rs not in raw_seen:
                    raw_seen.add(rs); new_on_page += 1
                place_text = " ".join([r.get("area", ""), r.get("sigungu", ""), r.get("place", "")])
                if not in_region(r.get("area", "") or place_text):
                    continue
                seq = r.get("seq") or r.get("id") or ""
                title = clean_api_text(r.get("title"))
                if RECRUIT_RE.search(title):
                    continue          # 취업·교육생 모집 공고는 동네 여가 정보가 아니라서 뺀다
                if not seq or not title or seq in seen:
                    continue
                seen.add(seq)
                start, end = ymd(r.get("startDate")), ymd(r.get("endDate"))
                end = end or start
                cat, sub = culture_category(r.get("realmName", ""), tp)
                eid = f"culture-{seq}"

                cached = fresh_cache(eid)
                if cached:
                    cached.update({"start": start, "end": end, "date": f"{start or ''} ~ {end or ''}".strip(" ~")})
                    out.append(cached)
                    continue

                d = {}
                try:   # 상세: 가격·주소·전화·원문 링크 (없어도 목록 정보만으로 표시)
                    if time.time() > t0 + CULTURE_BUDGET * 0.6:
                        raise RuntimeError("시간 예산 → 목록 정보만")   # 목록을 끝까지 받는 게 먼저
                    recs_d = guarded("문화정보원", culture_xml, "detail", seq=seq)
                    d = recs_d[0] if recs_d else {}
                except Exception:
                    pass
                addr = d.get("placeAddr", "")
                link = extract_api_url(d.get("url") or d.get("placeUrl") or "")
                district = (district_from_addr(addr) or district_from_addr(" ".join([r.get("area", ""), r.get("sigungu", "")]))
                            or district_hint(title, r.get("place", "")) or url_district(link))
                price = clean_api_text(d.get("price"))
                out.append({
                    "detailAt": TODAY_ISO,
                    "id": eid,
                    "apiId": seq,
                    "category": cat,
                    "subtype": sub,
                    "title": title,
                    "venue": clean_api_text(r.get("place")),
                    "venueId": match_venue(r.get("place", ""), addr),
                    "district": district,
                    "address": addr,
                    "date": f"{start or ''} ~ {end or ''}".strip(" ~"),
                    "time": clean_api_text(d.get("time") or d.get("dtguidance")) or "시간은 공식 원문에서 확인",
                    "price": price or "가격은 공식 원문에서 확인",
                    "contact": clean_api_text(d.get("phone")),
                    "description": clean_api_text(d.get("contents1"))[:400],
                    "status": "예정" if start and start > TODAY_ISO else "진행중",
                    "start": start,
                    "end": end,
                    "image": (r.get("thumbnail") or d.get("imgUrl") or "").replace("http://", "https://"),
                    "lat": to_float(r.get("gpsY")),
                    "lng": to_float(r.get("gpsX")),
                    "source": "한국문화정보원 문화정보",
                    "url": link,
                    "origin": "auto",
                    "api": "culture",
                    "verified": TODAY_ISO,
                    "tags": [t for t in dict.fromkeys([cat, sub, region_tag(district),
                                         "무료" if re.search(r"무료", price or "") else "",
                                         "체험" if tp == "C" else ""]) if t],
                })
            if not recs or new_on_page == 0:
                break            # 더 받을 게 없음 (한 쪽에 몇 개씩 주든 상관없이)
        log(f"  문화정보원 {tp_name}: 지금까지 광주·전남 {len(out)}건")
    CULTURE_MODE = f"{'지역별' if use_area[0] else '기간별'} 조회 · {pages_read}쪽 읽음"
    return out



# ───────── 공공데이터포털 표준데이터 (CSV): 전국문화축제 · 전국공연행사정보 ─────────
# 시·군·구가 직접 올리는 작은 동네 축제·행사를 보강한다. 표준데이터 API 신청이 막혀 있어 CSV 파일로 받는다.
# 사용법: 공공데이터포털에서 「전국문화축제표준데이터」, 「전국공연행사정보표준데이터」 CSV를 내려받아
#         저장소 맨 바깥(또는 raw/ 폴더)에 올리면, 하루 2번 자동 실행 때 광주·전남 것만 골라 일정에 더해요.
#         (파일 이름은 상관없음 · 첫 줄 열 이름으로 알아봄 · 새 파일을 올리면 가장 최근 것을 씀)
STD_NOTE = ""
STD_WINDOW_DAYS = 60          # 오늘부터 이 기간 안에 열리거나 진행 중인 것만

# 2026-11-16: 표준데이터를 API로 먼저 받는다 (CSV를 매달 다시 올리지 않아도 최신 자료) → 안 되면 위 CSV를 읽는다.
#   공공데이터포털에서 아래 두 API를 '활용신청'(자동 승인)하면 기존 인증키(DATA_GO_KR_KEY) 그대로 쓴다.
#   · 전국공연행사정보표준데이터  https://www.data.go.kr/data/15013106/standard.do
#   · 전국문화축제표준데이터      https://www.data.go.kr/data/15013104/standard.do
#   API 영문 항목 이름 → CSV 한글 열 이름으로 바꿔서, 아래 변환 코드를 CSV와 똑같이 쓴다.
#   (축제 쪽 영문 이름은 표준 규칙으로 짐작한 것 · 다르면 meta.json 에 '항목 이름 다름: …'이 남으니 여기만 고치면 됨)
STD_API = {
    "공연행사": ("https://api.data.go.kr/openapi/tn_pubr_public_pblprfr_event_info_api", {
        "eventNm": "행사명", "opar": "장소", "eventCo": "행사내용", "eventStartDate": "행사시작일자",
        "eventEndDate": "행사종료일자", "eventStartTime": "행사시작시각", "chrgeInfo": "요금정보", "admfee": "관람요금",
        "mnnstNm": "주관기관명", "auspcInsttNm": "주최기관명", "phoneNumber": "전화번호", "homepageUrl": "홈페이지주소",
        "rdnmadr": "소재지도로명주소", "lnmadr": "소재지지번주소", "latitude": "위도", "longitude": "경도",
        "instt_nm": "제공기관명"}),
    "문화축제": ("https://api.data.go.kr/openapi/tn_pubr_public_cltur_fstvl_api", {
        "fstvlNm": "축제명", "opar": "개최장소", "fstvlStartDate": "축제시작일자", "fstvlEndDate": "축제종료일자",
        "fstvlCo": "축제내용", "mnnst": "주관기관명", "mnnstNm": "주관기관명", "auspcInstt": "주최기관명",
        "auspcInsttNm": "주최기관명", "phoneNumber": "전화번호", "homepageUrl": "홈페이지주소", "relateInfo": "관련정보",
        "rdnmadr": "소재지도로명주소", "lnmadr": "소재지지번주소", "latitude": "위도", "longitude": "경도",
        "instt_nm": "제공기관명"}),
}
STD_API_MAX_PAGES = 40        # 1쪽 1000건 · 전국 자료라도 넉넉함 (하루 2번 × 두 종류 = 개발계정 한도 안)

def _std_api_rows(label, must):
    """표준데이터 API에서 전국 자료를 받아 CSV와 같은 한글 열 이름의 행 목록으로 돌려준다. 못 받으면 (None, 이유)."""
    if not DATA_KEY:
        return None, "인증키 없음"
    url, fmap = STD_API[label]
    rows, page, total = [], 1, None
    while page <= STD_API_MAX_PAGES:
        q = {"serviceKey": DATA_KEY, "pageNo": str(page), "numOfRows": "1000", "type": "json"}
        raw = http_get(url + "?" + urllib.parse.urlencode(q), timeout=40).decode("utf-8", "replace").strip()
        if not raw.startswith("{"):
            # 인증 실패 등은 XML 로 오는 경우가 많다 → 이유만 짧게
            m = re.search(r"<(?:returnAuthMsg|resultMsg|errMsg)>([^<]+)<", raw)
            return None, "API 응답 이상: " + (m.group(1) if m else raw[:80])
        j = json.loads(raw)
        resp = j.get("response", j)
        hdr = resp.get("header", {}) or {}
        code = str(hdr.get("resultCode", "00")).strip()
        if code == "03":                      # NODATA
            break
        if code not in ("00", "0", "000"):
            return None, f"API 오류 {code} {hdr.get('resultMsg', '')}".strip()
        body = resp.get("body", {}) or {}
        items = body.get("items", [])
        if isinstance(items, dict):
            items = items.get("item", [])
        if isinstance(items, dict):
            items = [items]
        for it in items or []:
            rows.append({fmap.get(k, k): ("" if v is None else str(v)) for k, v in it.items()})
        try:
            total = int(body.get("totalCount") or 0)
        except (TypeError, ValueError):
            total = 0
        if not items or page * 1000 >= total:
            break
        page += 1
    if rows and not any(all(rw.get(m) for m in must) for rw in rows[:50]):
        keys = sorted({k for rw in rows[:5] for k in rw})[:14]
        return None, "항목 이름 다름: " + ",".join(keys)
    return rows, f"API {len(rows)}행"

def _std_csv_files(must_have):
    files = []
    for d in (ROOT / "raw", ROOT):
        if d.exists():
            files += sorted(d.glob("*.csv"))
    found = []
    for f in files:
        raw = f.read_bytes()
        text = None
        for enc in ("utf-8-sig", "cp949", "euc-kr"):
            try:
                text = raw.decode(enc); break
            except UnicodeDecodeError:
                continue
        if not text:
            continue
        head = text.split("\n", 1)[0]
        if all(m in head for m in must_have):
            found.append((f.stat().st_mtime, f.name, text))
    found.sort(reverse=True)
    return found[0] if found else None

def _std_pick(row, *names):
    for n in names:
        v = (row.get(n) or "").strip()
        if v:
            return v
    return ""

def _std_date(v):
    v = (v or "").strip()
    m = re.match(r"(\d{4})[-./]?(\d{1,2})[-./]?(\d{1,2})", v)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
    except ValueError:
        return None

def _std_category(title, desc, festival):
    t = f"{title} {desc}"
    if re.search(r"전시|展|미술|사진전|갤러리", title):
        return "전시", "전시"
    if festival or re.search(r"축제|페스티벌|문화제|한마당", title):
        return "축제", "지역 축제"
    if re.search(r"콘서트|음악회|연주회|오케스트라|국악|버스킹|공연|뮤지컬|연극|무용", t):
        return "공연", "지역 공연"
    return "축제", "지역 행사"

def std_csv_events():
    """표준데이터 CSV 두 종류를 읽어 광주·전남의 가까운 일정만 앱 일정 형식으로 바꾼다."""
    global STD_NOTE
    out, notes = [], []
    today = NOW.date()
    until = (today + timedelta(days=STD_WINDOW_DAYS)).isoformat()
    kinds = [
        ("문화축제", ("축제명", "축제시작일자"), True,
         dict(title=("축제명",), place=("개최장소",), start=("축제시작일자",), end=("축제종료일자",), desc=("축제내용",),
              org=("주관기관명", "주최기관명"), phone=("전화번호", "주관기관전화번호"), url=("홈페이지주소", "관련정보"),
              addr=("소재지도로명주소", "소재지지번주소"), lat=("위도",), lng=("경도",), time=(), price=(), provider=("제공기관명",))),
        ("공연행사", ("행사명", "행사시작일자"), False,
         dict(title=("행사명",), place=("장소", "행사장소", "개최장소"), start=("행사시작일자",), end=("행사종료일자",),
              desc=("행사내용",), org=("주관기관", "주관기관명", "주최기관", "주최기관명"), phone=("전화번호",),
              url=("홈페이지주소",), addr=("소재지도로명주소", "소재지지번주소"), lat=("위도",), lng=("경도",),
              time=("행사시작시각",), price=("요금정보", "관람요금"), provider=("제공기관명",))),
    ]
    for label, must, festival, col in kinds:
        # ① API 로 먼저 (매일 최신) → ② 안 되면 저장소에 올린 CSV
        try:
            api_rows, api_note = _std_api_rows(label, must)
        except Exception as e:
            api_rows, api_note = None, scrub(f"API 실패 ({str(e)[:80]})")
        if api_rows is not None:
            rows_iter, fname = api_rows, api_note
        else:
            got = _std_csv_files(must)
            if not got:
                notes.append(f"{label} CSV 없음 · {api_note}")
                continue
            _, csv_name, text = got
            rows_iter, fname = csv.DictReader(io.StringIO(text)), f"{csv_name} · {api_note}"
        n_in = 0
        for r in rows_iter:
            r = {(k or "").strip(): (v or "") for k, v in r.items()}
            title = clean_api_text(_std_pick(r, *col["title"]))
            start = _std_date(_std_pick(r, *col["start"]))
            end = _std_date(_std_pick(r, *col["end"])) or start
            if not title or not start:
                continue
            addr = _std_pick(r, *col["addr"])
            place = clean_api_text(_std_pick(r, *col["place"]))
            provider = _std_pick(r, *col["provider"])
            if not in_region(" ".join([addr, provider, place])):
                continue
            if end < today.isoformat() or start > until:
                continue
            if RECRUIT_RE.search(title):
                continue
            desc = clean_api_text(_std_pick(r, *col["desc"]))
            cat, sub = _std_category(title, desc, festival)
            district = district_from_addr(addr) or district_hint(title, place) or ""
            price = clean_api_text(_std_pick(r, *col["price"])) if col["price"] else ""
            tm = _std_pick(r, *col["time"]) if col["time"] else ""
            uid = hashlib.sha1(f"{label}|{title}|{start}|{place}".encode("utf-8")).hexdigest()[:12]
            link = extract_api_url(_std_pick(r, *col["url"]))
            out.append({
                "id": f"std-{uid}",
                "category": cat, "subtype": sub, "title": title,
                "venue": place or provider, "venueId": match_venue(place, addr),
                "district": district, "address": addr,
                "date": f"{start} ~ {end}".strip(" ~"),
                "time": tm or "시간은 공식 원문에서 확인",
                "price": price or ("무료" if re.search(r"무료", desc) else "가격은 공식 원문에서 확인"),
                "contact": _std_pick(r, *col["phone"]),
                "description": desc[:400],
                "status": "예정" if start > TODAY_ISO else "진행중",
                "start": start, "end": end, "image": "",
                "lat": to_float(_std_pick(r, *col["lat"])), "lng": to_float(_std_pick(r, *col["lng"])),
                "source": f"공공데이터포털 {label} 표준데이터" + (f" ({provider})" if provider else ""),
                "url": link or "https://www.data.go.kr/",
                "origin": "auto", "api": "stdevents", "verified": TODAY_ISO,
                "tags": [t for t in dict.fromkeys([cat, sub, region_tag(district) if district else "",
                         "무료" if re.search(r"무료", price + " " + desc) else ""]) if t],
            })
            n_in += 1
        notes.append(f"{label} {n_in}건 ({fname})")
    STD_NOTE = " · ".join(notes)
    return out

KOPIS_BASE = "http://www.kopis.or.kr/openApi/restful"
KOPIS_MUSIC = ("대중음악", "서양음악(클래식)", "한국음악(국악)", "클래식", "국악")

_KOPIS_BLOCKED = []      # 이번 실행 중 한 번이라도 차단되면 더 묻지 않는다(차단이 길어지지 않게)

def kopis_xml(path, **params):
    if _KOPIS_BLOCKED:
        raise RuntimeError(f"KOPIS 차단 중이라 더 묻지 않음 ({_KOPIS_BLOCKED[0]})")
    time.sleep(1.5)          # KOPIS 는 짧은 시간에 많이 물으면 차단(Request Blocked)한다 → 천천히
    params["service"] = KOPIS_KEY
    url = f"{KOPIS_BASE}/{path}?" + urllib.parse.urlencode(params)
    try:
        raw = http_get(url)
    except RuntimeError as e:
        if "Blocked" in str(e) or "HTTP 429" in str(e):
            _KOPIS_BLOCKED.append(str(e)[:120])
            log("  ⚠ KOPIS 가 요청을 막음 → 이번 실행에선 KOPIS 를 더 묻지 않음")
        raise
    root = ET.fromstring(raw)
    err = root.findtext(".//returncode") or root.findtext(".//errmsg")
    if err and not root.findall(".//db"):
        raise RuntimeError(f"KOPIS 오류: {err}")
    return root

_FACILITY = {}
def kopis_facility_addr(mt10id):
    if not mt10id:
        return ""
    if mt10id not in _FACILITY:
        try:
            _FACILITY[mt10id] = (guarded("KOPIS 공연장", kopis_xml, f"prfplc/{mt10id}").findtext(".//adres") or "").strip()
        except Exception:
            _FACILITY[mt10id] = ""
    return _FACILITY[mt10id]

def kopis_performances():
    """앞으로 45일 공연 중 광주·전남만. 전국 목록을 끝까지 넘기며 지역으로 거른다.
    (시도코드는 통합특별시 출범 뒤 바뀔 수 있어 '지역명'으로 거르는 쪽이 안전)"""
    if not KOPIS_KEY:
        raise RuntimeError("KOPIS_API_KEY가 없습니다.")

    rows_per_page = 100
    hits = []
    deadline = time.time() + SOURCE_BUDGET
    period = dict(stdate=NOW.strftime("%Y%m%d"), eddate=(NOW + timedelta(days=45)).strftime("%Y%m%d"))

    def pages(extra, max_pages):
        got = []
        for page in range(1, max_pages + 1):
            if time.time() > deadline - 120:
                log("  KOPIS 목록: 시간 예산 도달 → 여기까지만"); break
            dbs = kopis_xml("pblprfr", **period, cpage=str(page), rows=str(rows_per_page), **extra).findall(".//db")
            got.extend(dbs)
            if len(dbs) < rows_per_page:
                break
        return got

    # 1) 지역코드(광주 29 · 전남 46)로 좁혀서 묻기 → 몇 번만 물으면 끝 (차단 위험 ↓)
    for code in ("29", "46"):
        dbs = pages({"signgucode": code}, 10)
        inr = [d for d in dbs if in_region(d.findtext("area") or "")]
        if dbs and len(inr) >= len(dbs) * 0.8:
            hits.extend(inr)
        elif dbs:
            log(f"  KOPIS 지역코드 {code} 가 통하지 않음 → 전국 목록에서 거르기")
            hits = []
            break
    else:
        log(f"  KOPIS 지역코드로 광주·전남 {len(hits)}건")
    # 2) 지역코드가 안 통하면 전국 목록을 넘기며 거르기 (최대 30쪽)
    if not hits:
        for db in pages({}, 30):
            if in_region(db.findtext("area") or ""):
                hits.append(db)
        log(f"  KOPIS 전국 목록에서 광주·전남 {len(hits)}건")
    # 같은 공연이 두 번 잡히지 않게
    seen_ids, uniq = set(), []
    for d in hits:
        mid = d.findtext("mt20id")
        if mid and mid not in seen_ids:
            seen_ids.add(mid); uniq.append(d)
    hits = uniq
    log(f"  KOPIS 광주·전남 공연 {len(hits)}건 · 상세 받는 중")

    out = []
    for db in hits:
        get = lambda tag, d=db: (d.findtext(tag) or "").strip()
        mid, title = get("mt20id"), clean_api_text(get("prfnm"))
        if not mid or not title:
            continue
        start = ymd(get("prfpdfrom"))
        end = ymd(get("prfpdto")) or start
        genre = get("genrenm") or "공연"
        venue = get("fcltynm")
        state = get("prfstate") or ("예정" if start and start > TODAY_ISO else "진행중")

        cached = fresh_cache(f"kopis-{mid}")
        if cached:
            cached.update({"start": start, "end": end, "status": state,
                           "date": f"{start or ''} ~ {end or ''}".strip(" ~")})
            out.append(cached)
            continue

        # 상세: 가격·시간·출연·공연장 주소 (새 공연이거나 7일 지난 것만)
        detail = {}
        try:
            if time.time() > deadline:
                raise RuntimeError("시간 예산")
            d = guarded("KOPIS", kopis_xml, f"pblprfr/{mid}").find(".//db")
            if d is not None:
                detail = {t: (d.findtext(t) or "").strip() for t in
                          ("pcseguidance", "dtguidance", "prfcast", "prfruntime", "prfage", "mt10id", "entrpsnmP")}
                links = [(r.findtext("relatenm") or "", r.findtext("relateurl") or "") for r in d.findall(".//relate")]
                detail["ticket"] = next((u for _, u in links if u), "")
        except Exception:
            pass
        addr = kopis_facility_addr(detail.get("mt10id"))
        district = district_from_addr(addr) or district_hint(title, venue)

        out.append({
            "detailAt": TODAY_ISO,
            "id": f"kopis-{mid}",
            "apiId": mid,
            "category": "음악" if genre in KOPIS_MUSIC else "공연",
            "subtype": genre,
            "title": title,
            "venue": venue,
            "venueId": match_venue(venue, addr),
            "district": district,
            "address": addr,
            "date": f"{start or ''} ~ {end or ''}".strip(" ~"),
            "time": detail.get("dtguidance") or "시간은 KOPIS에서 확인",
            "price": detail.get("pcseguidance") or "가격은 KOPIS에서 확인",
            "age": detail.get("prfage", ""),
            "cast": detail.get("prfcast", ""),
            "runtime": detail.get("prfruntime", ""),
            "status": state,
            "start": start,
            "end": end,
            "image": get("poster").replace("http://", "https://"),
            "source": "KOPIS 공연예술통합전산망",
            "url": detail.get("ticket") or f"https://www.kopis.or.kr/por/db/pblprfr/pblprfrView.do?menuId=MNU_00020&mt20Id={mid}",
            "origin": "auto",
            "api": "kopis",
            "verified": TODAY_ISO,
            "tags": ["공연", genre, region_tag(district)],
        })
    return out


# ───────── 광주시립미술관 누리집: 현재·예정 전시 (특별전·기획전 포함) ─────────
# 공공데이터(문화정보원·관광공사)에 시립미술관 기획전이 거의 안 올라와서 누리집 전시 목록을 직접 읽는다.
GMA_BASE = "https://gjartmuse.jeonnam-gwangju.go.kr"
GMA_LISTS = [("N", "artmuse0209000000", "진행중"), ("F", "artmuse0210000000", "예정")]
GMA_SKIP = re.compile(r"G&J|지앤제이")            # G&J갤러리는 서울 인사동 분관 → 뺌


def _text(html_part: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_part, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</(p|div|li|dd|dt|tr|h\d)>", "\n", t, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"[ \t\xa0]+", " ", t)


def gma_exhibitions():
    found = {}
    for tp, page_id, status in GMA_LISTS:
        url = f"{GMA_BASE}/pj/pjExhibit.php?pageID={page_id}&action=list&exhiTp={tp}"
        page = http_get(url, timeout=25).decode("utf-8", "replace")
        anchors = list(re.finditer(r'<a\b[^>]*href="([^"]*eSeq=(\d+)[^"]*)"[^>]*>(.*?)</a>', page, re.S | re.I))
        for m in anchors:
            href, seq, inner = html.unescape(m.group(1)), m.group(2), m.group(3)
            names = [_text(inner).strip()] + re.findall(r'(?:title|alt)="([^"]+)"', inner)
            names = [re.sub(r"\s+", " ", html.unescape(n)).strip(" -·") for n in names if n and n.strip()]
            g = found.setdefault(seq, {"seq": seq, "titles": [], "href": "", "ctx": "", "status": status})
            g["titles"] += names
            if re.search(r"exhiCd=\d", href) and not g["href"]:
                g["href"] = href
                g["ctx"] = _text(page[m.start(): m.start() + 4000])     # 이 전시 칸의 글자 (장소·기간)
    out = []
    for seq, g in found.items():
        ctx = g["ctx"] or ""
        dm = re.search(r"(20\d\d)[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})\.?\s*[~∼-]\s*(20\d\d)[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", ctx)
        if not dm:
            continue
        y1, m1, d1, y2, m2, d2 = (int(x) for x in dm.groups())
        start, end = f"{y1:04d}-{m1:02d}-{d1:02d}", f"{y2:04d}-{m2:02d}-{d2:02d}"
        if end < TODAY_ISO:
            continue
        before = ctx[: dm.start()]
        pm = re.search(r"장소\s*[:：]?\s*([^\n]+)", ctx) or re.search(r"([^\n]*(?:전시실|미술관|갤러리|로비|광장)[^\n]*)", before)
        place = re.sub(r"\s*(전시)?기간.*$", "", pm.group(1)).strip(" :·") if pm else ""
        place = re.sub(r"^(광주|시립)미술관\s*", "", place)          # '광주미술관 본관 제1전시실' → '본관 제1전시실'
        if re.search(r"상설전|전시\s*$|≪|《|\.\.\.|…|체크|['\"]", place):
            place = ""                                                    # 제목 조각을 장소로 잘못 잡은 경우
        # 링크 글자에 '제목 · 기간 … · 장소 …'가 통째로 들어 있는 경우가 있어 나눠서 쓴다
        for t in g["titles"]:
            pm2 = re.search(r"장소\s*[:：]?\s*([^·\n]+)", t)
            if pm2 and not place:
                place = re.sub(r"^(광주|시립)미술관\s*", "", pm2.group(1).strip(" :·"))
        cut = lambda t: re.split(r"\s*·?\s*(?:전시)?(?:기간|장소)\s*[:：]?\s", re.sub(r"^[>\s·]+", "", t))[0].strip(" ·")
        titles = [cut(t) for t in g["titles"]]
        titles = [t for t in titles if len(t) >= 2 and not re.fullmatch(r"(자세히|더보기|상세보기|view|more)", t, re.I)]
        full = [t for t in titles if not re.search(r"(\.\.\.|…)$", t)]
        title = max(full or titles or [""], key=len)
        title = re.sub(r"(\.\.\.|…)$", "", title).strip()
        title = re.sub(r"\s*(상세\s*페이지\s*이동|자세히\s*보기|상세\s*보기)\s*$", "", title).strip()   # 링크 안내 글자가 제목에 붙는 경우
        if not title or GMA_SKIP.search(place + title):
            continue
        detail_url = urllib.parse.urljoin(GMA_BASE + "/pj/", g["href"]) if g["href"] else f"{GMA_BASE}/pj/pjExhibit.php?action=view&eSeq={seq}"
        image, price = "", "누리집 확인"
        try:
            dp = http_get(detail_url, timeout=20).decode("utf-8", "replace")
            im = re.search(r'["\']([^"\']*/upload/pj/PJ_EXHIBIT/[^"\']+\.(?:jpe?g|png|gif|webp))["\']', dp, re.I)
            if im:
                image = urllib.parse.urljoin(GMA_BASE, im.group(1))
            dt = _text(dp)
            fee = re.search(r"일반[^0-9\n]{0,20}([\d,]{3,})\s*원", dt)
            if fee:
                price = f"유료 · 일반 {fee.group(1)}원"
            elif re.search(r"관람료[^\n]{0,30}무료|무료\s*관람", dt):
                price = "무료"
        except Exception as e:
            log(f"  · 시립미술관 상세 실패 {seq}: {scrub(str(e))[:80]}")
        photo = "사진전시관" in place
        # 분관(하정웅미술관)에서 하는 전시는 그 장소로 — 본관 주소·지하철 안내가 붙으면 엉뚱한 곳으로 가요
        hjw = "하정웅미술관" in place
        if hjw:
            room = re.search(r"\(([^()]*전시실[^()]*)\)", place)
            place = room.group(1) if room else ""
        out.append({
            "id": f"gma-{seq}", "apiId": seq, "category": "전시", "subtype": "미술",
            "title": title,
            "venue": ("하정웅미술관" + (f" {place}" if place else "")) if hjw else "광주시립미술관" + (f" · {place}" if place and "시립미술관" not in place else (f" ({place})" if place else "")),
            "venueId": "hajungwoong" if hjw else ("gmoa-photo-gallery" if photo else "gma"),
            "district": "서구" if hjw else "북구", "address": "광주 서구 상무대로 1165" if hjw else ("" if photo else "광주 북구 하서로 52"),
            "date": f"{m1:02d}.{d1:02d} — {m2:02d}.{d2:02d}", "start": start, "end": end,
            "time": "운영시간·휴관일은 누리집 확인", "price": price,
            "status": "진행중" if start <= TODAY_ISO else "예정",
            "image": image, "imageCredit": "광주시립미술관" if image else "",
            "source": "광주시립미술관 누리집", "url": detail_url,
            "tags": ["전시", "미술", "실내"] + (["무료"] if price == "무료" else []),
            "origin": "auto", "api": "gma", "verified": TODAY_ISO,
        })
    if not out:
        raise RuntimeError("시립미술관 전시 목록을 읽지 못함 (누리집 구조가 바뀌었을 수 있음)")
    return out


def smart_event_key(e):
    title = norm_title(e.get("title", ""))
    title = re.sub(r"^(?:20\d{2}|제?\d+회)+", "", title)      # 앞의 연도·회차 제거
    return f"{title}:{e.get('start', '')}:{e.get('end', '')}"


def loose_title(e):
    """수동 등록 행사와 API 행사가 제목만 조금 다를 때 잡기 위한 키"""
    t = norm_title(e.get("title", ""))
    t = re.sub(r"^(?:20\d{2}|제?\d+회)+", "", t)
    return t[:12]


RECRUIT_RE = re.compile(r"(교육생|수강생|훈련생|참가자|참여자|참여기업|단원|작가)\s*모집|모집\s*공고|취업|채용|양성과정|장기과정|사관학교|부트캠프")


def tidy_event(e):
    """자동 수집 일정 공통 정리: 깨진 기호(&middot; 등) 풀기. 모집 공고면 None."""
    e = dict(e)
    for k in ("title", "venue", "price", "time", "description", "program"):
        if isinstance(e.get(k), str):
            e[k] = clean_api_text(e[k])
    if RECRUIT_RE.search(e.get("title", "")):
        return None
    return e


def title_core(t):
    t = re.sub(r"\[[^\]]*\]", " ", t or "")          # [광주] 같은 지역 꼬리표만 뺌 (부제는 남겨서 다른 공연과 구분)
    return re.sub(r"^(?:20\d{2}|제?\d+회)+", "", norm_title(t))


_TOKEN_STOP = re.compile(r"^(acc|국립아시아문화전당|아시아문화전당|공연|특별공연|기획공연|광주|전남|20\d{2}|제?\d+회)$")


def title_tokens(t):
    t = re.sub(r"\[[^\]]*\]", " ", t or "").lower()
    return {w for w in re.split(r"[^0-9a-z가-힣]+", t) if len(w) >= 2 and not _TOKEN_STOP.match(w)}


def same_words_title(x, y):
    """단어 순서만 바뀐 같은 제목: 'ACC 렛츠플레이, 저승할망' ↔ '[광주] 저승할망 - ACC 렛츠플레이'"""
    a, b = title_tokens(x), title_tokens(y)
    if not a or not b:
        return False
    s, l = (a, b) if len(a) <= len(b) else (b, a)
    if len("".join(s)) < 4:
        return False
    return s <= l and (len(s) >= 2 or len(s) == len(l))


def same_event(a, b):
    x, y = title_core(a.get("title")), title_core(b.get("title"))
    if len(x) < 4 or len(y) < 4 or not (x == y or x in y or y in x or same_words_title(a.get("title"), b.get("title"))):
        return False
    if a.get("venueId") and b.get("venueId") and a["venueId"] != b["venueId"]:
        return False
    return (a.get("start") or "0000") <= (b.get("end") or "9999") and (b.get("start") or "0000") <= (a.get("end") or "9999")


def merge_events(manual, api_events):
    merged, seen, manual_titles = [], set(), set()
    for e in manual:
        manual_titles.add(loose_title(e))

    # 수동 데이터가 앞에 있으므로 같은 항목이면 수동 데이터가 우선.
    for e in manual + api_events:
        if e.get("end") and e["end"] < TODAY_ISO:
            continue
        k = smart_event_key(e)
        if k in seen:
            continue
        if e.get("origin") == "auto" and len(loose_title(e)) >= 6 and loose_title(e) in manual_titles:
            continue                                  # 직접 확인한 같은 행사가 이미 있음
        if e.get("origin") == "auto" and any(same_event(e, m) for m in manual):
            continue                                  # 제목 한쪽이 다른 쪽에 들어 있고 기간이 겹침
        seen.add(k)
        merged.append(e)

    # 출처끼리 겹치는 것 정리: 직접 입력 > KOPIS·관광공사 > 문화정보원 순으로 남김
    pri = lambda e: 0 if e.get("origin") != "auto" else (2 if e.get("api") == "culture" else 1)
    kept = []
    for e in sorted(merged, key=pri):
        if not any(same_event(e, k) for k in kept):
            kept.append(e)
    merged = kept
    merged.sort(key=lambda e: (e.get("start") or "9999-99-99", e.get("title") or ""))
    return merged


# ───────── 매주 정해진 요일에 열리는 정기 프로그램 ─────────
# 공공데이터(관광공사·KOPIS·문화정보원)에 올라오지 않는 곳의 '매주 ○요일' 행사를
# 실행할 때마다 앞으로 WEEKS_AHEAD 주치 날짜로 새로 만들어 넣는다. (지난 날짜는 자동으로 빠짐)
# 줄을 더하면 다른 정기 행사도 똑같이 자동으로 들어간다.
#   weekday: 월0 화1 수2 목3 금4 토5 일6   months: 운영하는 달 (쉬는 달은 빼기)
#   skip: 쉬는 날짜 "YYYY-MM-DD" 목록 (명절·행사 등으로 공연이 없는 토요일)
WEEKS_AHEAD = 5
RECURRING = [
    {
        "id": "gtcc-saturday",
        "weekday": 5,
        "months": [4, 5, 6, 7, 8, 9, 10, 11],
        "skip": [],
        "event": {
            "category": "공연", "subtype": "국악",
            "title": "전통문화관 토요상설공연",
            "venue": "광주전통문화관", "venueId": "traditional-culture",
            "district": "동구", "address": "광주 동구 의재로 222",
            "lat": 35.1329, "lng": 126.9695,
            "time": "13:00 전통 체험(민속놀이·한복) · 15:00 서석당 공연 (약 1시간)",
            "description": "무등산 들머리 광주전통문화관에서 매주 토요일 열리는 무료 프로그램이에요. 오후 1시부터 민속놀이·한복 입기 등 전통 체험, 3시부터 서석당에서 국악·전통춤 공연이 이어져요. 나이 제한 없이 누구나 볼 수 있어요.",
            "child": "민속놀이·한복 체험 (나이 제한 없음)", "parking": "무료 주차",
            "price": "무료",
            "verify": "매주 토요일 정기 프로그램 · 그날 공연 여부·출연진은 누리집 확인",
            "source": "광주문화재단 전통문화관",
            "url": "http://www.gtcc.or.kr/", "contact": "062-670-8550",
            "tags": ["공연", "국악", "무료", "전통체험"],
            "ticket": {"status": "무료", "note": "현장 등록 · 연령 제한 없음"},
        },
    },
]


# ───────── 관심 축제 지키기 (scripts/festival_watch.json) ─────────
# 해마다 열리지만 공공데이터 등록이 늦거나 빠지는 축제를, 이름이 바뀌거나 시기가 옮겨져도 놓치지 않게 한다.
#  ① 관광공사 TourAPI를 '이름·옛 이름'으로 직접 검색(지역코드와 무관하게) → 행사 상세의 날짜 사용
#  ② 못 찾으면 공식 누리집에서 올해·내년 날짜를 읽음
#  ③ 그래도 없는데 열릴 때가 다가오면 meta.json 에 '일정 못 찾음'을 빨갛게 남김 (관리자 화면 #admin 에 보임)
# 찾은 날짜는 docs/data/annual.json(축제 달력)의 this 에도 적어, 달력이 해마다 저절로 맞춰지게 한다.
WATCH_PATH = Path(__file__).resolve().parent / "festival_watch.json"
OUT_ANNUAL = DATA_DIR / "annual.json"
WATCH_MISSING = []
WATCH_FOUND = []
MANUAL_FOR_WATCH = []

_DASH = r"[~∼〜\-–—]"
_DAY = r"\s*일?\.?\s*(?:\([^)]{1,3}\))?\s*"
_RANGE_Y = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})" + _DAY + _DASH +
                      r"\s*(?:(20\d{2})\s*[.\-/년]\s*)?(?:(\d{1,2})\s*[.\-/월]\s*)?(\d{1,2})(?!\d)")
_RANGE_NOY = re.compile(r"(?<![\d.])(\d{1,2})\s*[.월]\s*(\d{1,2})" + _DAY + _DASH + r"\s*(?:(\d{1,2})\s*[.월]\s*)?(\d{1,2})(?!\d)")


def _mk_range(y, m1, d1, y2, m2, d2):
    from datetime import date
    try:
        y, m1, d1 = int(y), int(m1), int(d1)
        m2 = int(m2) if m2 else m1
        y2 = int(y2) if y2 else (y + 1 if m2 < m1 else y)
        a, b = date(y, m1, d1), date(y2, m2, int(d2))
    except (ValueError, TypeError):
        return None
    if b < a or (b - a).days > 31:
        return None
    return a.isoformat(), b.isoformat()


def watch_dates_in_text(text, names, need_near=True):
    """글 속에서 축제 날짜(시작~끝)를 찾는다. need_near 면 축제 이름 근처(앞뒤 300자)에 있는 날짜만."""
    t = re.sub(r"\s+", " ", text or "")
    y_now = NOW.year
    keys = [n for n in names if n]
    def near(i):
        if not need_near:
            return True
        win = t[max(0, i - 300): i + 300]
        return any(k in win or k.replace(" ", "") in win.replace(" ", "") for k in keys)
    found = []
    for m in _RANGE_Y.finditer(t):
        if near(m.start()):
            r = _mk_range(*m.groups())
            if r: found.append(r)
    for m in _RANGE_NOY.finditer(t):
        if not near(m.start()):
            continue
        before = t[max(0, m.start() - 120): m.start()]
        ys = re.findall(r"(20\d{2})", before)
        if not ys:
            continue
        r = _mk_range(ys[-1], m.group(1), m.group(2), None, m.group(3), m.group(4))
        if r: found.append(r)
    found = [r for r in found if int(r[0][:4]) in (y_now, y_now + 1) and r[1] >= TODAY_ISO]
    return sorted(set(found))[0] if found else None


def tourapi_keyword(keyword):
    params = {"serviceKey": DATA_KEY, "MobileOS": "ETC", "MobileApp": "gwangju-eodi", "_type": "json",
              "keyword": keyword, "contentTypeId": "15", "numOfRows": "30", "pageNo": "1", "arrange": "A"}
    raw = http_get("https://apis.data.go.kr/B551011/KorService2/searchKeyword2?" + urllib.parse.urlencode(params))
    j = json.loads(raw.decode("utf-8"))
    resp = (j or {}).get("response") or {}
    if str((resp.get("header") or {}).get("resultCode")) not in ("0000", "0"):
        raise RuntimeError(f"TourAPI 키워드 검색 오류: {resp.get('header')}")
    items = ((resp.get("body") or {}).get("items") or {}) or {}
    items = items.get("item") if isinstance(items, dict) else []
    return [items] if isinstance(items, dict) else (items or [])


def _watch_event(w, start, end, how, extra=None):
    tpl = dict(w.get("template") or {})
    y = start[:4]
    title = tpl.pop("title", w["names"][0]).replace("{year}", y)
    md = lambda s: f"{int(s[5:7])}.{int(s[8:10])}"
    e = {
        "id": f"watch-{w['id']}-{y}", "title": title, **tpl,
        "start": start, "end": end, "date": f"{md(start)} ~ {md(end)}",
        "status": "예정" if start > TODAY_ISO else "진행중",
        "origin": "auto", "api": "watch", "verified": TODAY_ISO,
        "verify": f"{how} · 시간·요금은 공식 누리집 확인",
        "source": how,
        "ticket": {"status": "확인필요", "note": "공식 안내 확인", "checked": TODAY_ISO},
    }
    al = list(e.get("aliases") or [])
    for n in w["names"]:
        if n not in al and n != title:
            al.append(n)
    e["aliases"] = al
    if extra:
        e.update({k: v for k, v in extra.items() if v})
    return e


def _watch_annual(w, start, end):
    """축제 달력(annual.json)의 올해 날짜를 찾은 날짜로 맞춘다. 예전 해 날짜는 last 로 남김."""
    try:
        ann = read_json(OUT_ANNUAL, [])
        aid = w.get("annual_id") or f"an-{w['id']}"
        for a in ann:
            if a.get("id") != aid:
                continue
            cur = a.get("this") or {}
            new = {"year": int(start[:4]), "start": start, "end": end, "round": cur.get("round", "") if cur.get("year") == int(start[:4]) else ""}
            if cur.get("start") == start and cur.get("end") == end:
                return
            if cur and cur.get("year") != new["year"]:
                a["last"] = cur
            a["this"] = new
            a["month"] = int(start[5:7])
            write_json(OUT_ANNUAL, ann)
            log(f"  ✎ 축제 달력 맞춤: {a.get('name')} {start}~{end}")
            return
    except Exception as ex:
        log(f"  ⚠ 축제 달력 맞추기 실패 (무시): {ex}")


def _watch_done_this_year(w):
    """올해 것이 이미 들어 있거나 이미 끝났으면 '못 찾음' 경고를 내지 않는다."""
    y = NOW.year
    keys = [norm_title(n) for n in w["names"]]
    hit = lambda e: (e.get("start") or "")[:4] == str(y) and any(k and k in norm_title(e.get("title", "") + " ".join(e.get("aliases") or [])) for k in keys)
    if any(hit(e) for e in MANUAL_FOR_WATCH) or any(hit(e) for e in PREV.values()):
        return True
    for a in read_json(OUT_ANNUAL, []):
        if a.get("id") == (w.get("annual_id") or f"an-{w['id']}") and (a.get("this") or {}).get("year") == y:
            return True
    return False


def _watch_window(w):
    """열릴 수 있는 달(months)의 45일 전부터 그 달 끝까지면 True"""
    from datetime import date
    today = NOW.date()
    for m in w.get("months") or []:
        first = date(today.year, int(m), 1)
        last = (date(today.year + (m == 12), (m % 12) + 1, 1) - timedelta(days=1))
        if first - timedelta(days=45) <= today <= last:
            return True
    return False


def watch_festivals():
    WATCH_MISSING.clear(); WATCH_FOUND.clear()
    conf = read_json(WATCH_PATH, {})
    deadline = time.time() + SOURCE_BUDGET
    out = []
    for w in conf.get("festivals", []):
        if not w.get("id") or not w.get("names") or time.time() > deadline:
            continue
        keys = [norm_title(n) for n in w["names"]]
        got = None
        # ① 관광공사 이름 검색
        if DATA_KEY:
            for name in w["names"]:
                try:
                    items = tourapi_keyword(name)
                except Exception as ex:
                    log(f"  관광공사 '{name}' 검색 실패: {ex}"); continue
                for x in items:
                    t = clean_api_text(x.get("title"))
                    if not in_region(x.get("addr1") or "") or not any(k and (k in norm_title(t) or norm_title(t) in k) for k in keys):
                        continue
                    cid = str(x.get("contentid") or "")
                    try:
                        intro = guarded("관광공사", tourapi_detail_intro, cid)
                    except Exception:
                        intro = {}
                    s, e = ymd(intro.get("eventstartdate")), ymd(intro.get("eventenddate"))
                    if s and (e or s) >= TODAY_ISO:
                        got = _watch_event(w, s, e or s, "한국관광공사 TourAPI (이름 검색)", {
                            "id": f"tour-{cid}", "apiId": cid, "title": t,
                            "image": x.get("firstimage") or x.get("firstimage2") or "",
                            "lat": to_float(x.get("mapy")), "lng": to_float(x.get("mapx")),
                            "time": clean_api_text(intro.get("playtime")),
                            "price": clean_api_text(intro.get("usetimefestival")),
                            "venue": clean_api_text(intro.get("eventplace")),
                        })
                        break
                if got:
                    break
        # ② 공식 누리집에서 날짜 읽기
        if not got:
            own = (w.get("template") or {}).get("url", "")
            for url in w.get("pages") or []:
                try:
                    raw = http_get(url, timeout=15).decode("utf-8", "replace")
                except Exception as ex:
                    log(f"  {url} 읽기 실패: {ex}"); continue
                text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)))
                r = watch_dates_in_text(text, w["names"] + list((w.get("template") or {}).get("aliases") or []),
                                        need_near=not (own and url.rstrip("/") == own.rstrip("/")))
                if r:
                    got = _watch_event(w, r[0], r[1], f"공식 누리집 날짜 자동 확인 ({urllib.parse.urlparse(url).netloc})")
                    break
        if got:
            out.append(got); WATCH_FOUND.append(f"{w['names'][0]} {got['start']}~{got['end']}")
            _watch_annual(w, got["start"], got["end"])
            log(f"  ✔ 관심 축제 찾음: {got['title']} {got['start']}~{got['end']} ({got['source']})")
        elif _watch_window(w) and not _watch_done_this_year(w):
            WATCH_MISSING.append(w["names"][0])
            log(f"  ⚠ 관심 축제 일정 못 찾음: {w['names'][0]} → festivals_extra.json 에 직접 넣어 주세요")
    return out


def recurring_events():
    out = []
    today = NOW.date()
    for r in RECURRING:
        skip = set(r.get("skip") or [])
        d = today + timedelta(days=(r["weekday"] - today.weekday()) % 7)
        for _ in range(WEEKS_AHEAD):
            iso = d.isoformat()
            if d.month in r.get("months", range(1, 13)) and iso not in skip:
                e = json.loads(json.dumps(r["event"], ensure_ascii=False))
                e.update({
                    "id": f"{r['id']}-{iso}",
                    "title": f"{e['title']} ({d.month}/{d.day})",
                    "date": f"{d.month:02d}.{d.day:02d} — {d.month:02d}.{d.day:02d}",
                    "start": iso, "end": iso,
                    "status": "예정", "origin": "manual", "recurring": r["id"],
                    "verified": TODAY_ISO,
                })
                e.setdefault("ticket", {})["checked"] = TODAY_ISO
                out.append(e)
            d += timedelta(days=7)
    return out



# ───────── 🧸 키즈카페·실내놀이터 (2026-11-16) ─────────
# 자료: 행정안전부 「전국기타유원시설업소표준데이터」(지방행정 인허가 · 키즈카페 등 기타유원시설업, 매일 갱신)
#   https://www.data.go.kr/en/data/15124679/standard.do → URL(file.localdata.go.kr)에서 CSV를 받아 저장소 맨 바깥에 올림
#   (파일 이름 상관없음 · 첫 줄에 '문화체육업종명'·'사업장명'·'영업상태명'이 있으면 알아봄 · 가끔 새로 올리면 됨)
# 같은 업종에 오락실·캠핑장·물놀이장도 섞여 있어 이름으로 키즈카페·실내놀이터만 고른다. 영업 중인 곳만.
# 결과: docs/data/kidscafes.json (앱 '아이랑 어디 갈까?' → '🧸 키즈카페' 칸)
OUT_KIDSCAFES = DATA_DIR / "kidscafes.json"
# EPSG:5174 (Bessel 중부원점 TM, 보정계수 없음) → WGS84
def tm5174_to_wgs84(x, y):
    a=6377397.155; f=1/299.1528128; e2=2*f-f*f
    lat0=math.radians(38.0); lon0=math.radians(127.0028902777778); k0=1.0; FE=200000.0; FN=500000.0
    def M(phi):
        return a*((1-e2/4-3*e2**2/64-5*e2**3/256)*phi-(3*e2/8+3*e2**2/32+45*e2**3/1024)*math.sin(2*phi)+(15*e2**2/256+45*e2**3/1024)*math.sin(4*phi)-(35*e2**3/3072)*math.sin(6*phi))
    m=M(lat0)+(y-FN)/k0
    mu=m/(a*(1-e2/4-3*e2**2/64-5*e2**3/256)); e1=(1-math.sqrt(1-e2))/(1+math.sqrt(1-e2))
    p1=mu+(3*e1/2-27*e1**3/32)*math.sin(2*mu)+(21*e1**2/16-55*e1**4/32)*math.sin(4*mu)+(151*e1**3/96)*math.sin(6*mu)+(1097*e1**4/512)*math.sin(8*mu)
    ep2=e2/(1-e2); C1=ep2*math.cos(p1)**2; T1=math.tan(p1)**2; N1=a/math.sqrt(1-e2*math.sin(p1)**2); R1=a*(1-e2)/(1-e2*math.sin(p1)**2)**1.5; D=(x-FE)/(N1*k0)
    lat=p1-(N1*math.tan(p1)/R1)*(D*D/2-(5+3*T1+10*C1-4*C1*C1-9*ep2)*D**4/24+(61+90*T1+298*C1+45*T1*T1-252*ep2-3*C1*C1)*D**6/720)
    lon=lon0+(D-(1+2*T1+C1)*D**3/6+(5-2*C1+28*T1-3*C1*C1+8*ep2+24*T1*T1)*D**5/120)/math.cos(p1)
    # Bessel → WGS84 (7변수, EPSG:5174 towgs84)
    X0,Y0,Z0=-115.80,474.99,674.11; rx,ry,rz=[math.radians(s/3600) for s in (1.16,-2.31,-1.63)]; s=6.43e-6
    N=a/math.sqrt(1-e2*math.sin(lat)**2); h=0
    X=(N+h)*math.cos(lat)*math.cos(lon); Y=(N+h)*math.cos(lat)*math.sin(lon); Z=(N*(1-e2)+h)*math.sin(lat)
    X2=X0+(1+s)*(X - rz*Y + ry*Z); Y2=Y0+(1+s)*(rz*X + Y - rx*Z); Z2=Z0+(1+s)*(-ry*X + rx*Y + Z)
    a2=6378137.0; f2=1/298.257223563; e22=2*f2-f2*f2; p=math.hypot(X2,Y2); lat2=math.atan2(Z2,p*(1-e22))
    for _ in range(6):
        N2=a2/math.sqrt(1-e22*math.sin(lat2)**2); lat2=math.atan2(Z2+e22*N2*math.sin(lat2),p)
    return round(math.degrees(lat2),6), round(math.degrees(math.atan2(Y2,X2)),6)

KC_EXCLUDE = re.compile(r"오락|게임|GAME|코인|COIN|ARCADE|아케이드|캠핑|야영|글램핑|풀빌라|리조트|워터|물놀이|물체험|썰매|풀장|수영|촬영장|치유센터|정원|문구|하천|VR|브이알|클라임|야구장|시네마|더섬|에코|관광|농수산|하우스|공룡공원|아이스|해상체험|드라이빙|스테이|힐링파크|오랑쥬|놀이동산|베리팜|포레스트|광장|드론", re.I)
KC_INCLUDE = re.compile(r"키즈|kids|베이비|주니어|플레이|play|팡팡|방방|점핑|점프|트램|바운스|다쿵|월드킹|코코몽|타요|큐리|와글|무키무키|코코별|키키카카|헬로파크|히어로|롤리폴리|리틀펀트|바니바니|블럭랜드|위치스|퐁퐁|루미잼|델꼬|호호펀|아이플러스|아이즐링|키즈캐슬|조이|구구단|허니비|메르시|쁘띠놀|킨더|러블레싱|옐로우지브라|스페이스파크|반짝반짝|에어빌리지|휴파크|하이점프|박스플레이|볼락스|나르샤|어린이체험", re.I)

def _kc_phone(v):
    d = re.sub(r"\D", "", v or "")
    if len(d) < 9:
        return ""
    if d.startswith("02"):
        return f"02-{d[2:-4]}-{d[-4:]}"
    return f"{d[:3]}-{d[3:-4]}-{d[-4:]}" if len(d) in (10, 11) else d

def build_kidscafes():
    """기타유원시설업 CSV → 광주·전남 영업 중 키즈카페 목록 (kidscafes.json). 파일이 없으면 아무것도 안 함."""
    got = _std_csv_files(("문화체육업종명", "사업장명", "영업상태명"))
    if not got:
        return None, "기타유원시설업 CSV 없음"
    _, fname, text = got
    items = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        nm = re.sub(r"\s+", " ", r.get("사업장명", "")).strip()
        if r.get("영업상태명") not in ("영업/정상", "영업", "정상") or not nm:
            continue
        if KC_EXCLUDE.search(nm) or not KC_INCLUDE.search(nm):
            continue
        addr = re.sub(r"\s+", " ", r.get("도로명주소") or r.get("지번주소") or "").strip()
        if not in_region(addr):
            continue
        lat = lng = None
        try:
            x, y = float(r.get("좌표정보(X)") or 0), float(r.get("좌표정보(Y)") or 0)
            if x and y:
                lat, lng = tm5174_to_wgs84(x, y)
        except (TypeError, ValueError):
            pass
        rides = re.sub(r"\s+", " ", r.get("놀이기구수내역", "")).strip(" ,")
        items.append({
            # 관리번호는 시·군마다 따로 매겨서 겹침 → 지자체 코드를 붙여야 가게마다 하나 (2026-11-17)
            "id": "kc-" + ((r.get("개방자치단체코드", "") + "-" + r["관리번호"]) if r.get("관리번호") else hashlib.sha1((nm + addr).encode("utf-8")).hexdigest()[:10]),
            "name": nm, "kind": "키즈카페", "district": district_from_addr(addr), "address": addr,
            "phone": _kc_phone(r.get("전화번호")), "lat": lat, "lng": lng,
            "rides": rides if rides and not re.fullmatch(r"[\d\s]+", rides) else "",
            "since": (r.get("인허가일자") or "")[:10], "inout": "실내",
        })
    n_csv, pfc_note = len(items), ""
    try:                                                      # 2026-11-18: 어린이놀이시설 자료로 '음식점으로 신고한 키즈카페'까지
        extra, pfc_note = _pfc_kidscafes(items)
        items += extra
    except Exception as e:
        pfc_note = scrub(f"놀이시설 자료 실패: {e}")[:160]
    items.sort(key=lambda v: (v["district"], v["name"]))
    write_json(OUT_KIDSCAFES, {"updated": TODAY_ISO, "source": "행정안전부 지방행정 인허가(기타유원시설업) · 행정안전부 어린이놀이시설 정보 · " + fname, "items": items})
    return len(items), f"키즈카페 {len(items)}곳 (인허가 {n_csv} + {pfc_note})"

PFC_URL = "https://apis.data.go.kr/1741000/pfc3/getPfctInfo3"
PFC_PLACES = [("A013", "놀이제공영업소"), ("A004", "식품접객업소")]
PFC_FOOD_OK = re.compile(r"키즈|키카|kids|놀이터|놀이카페|플레이|play|점핑|점프|방방|트램|바운스|볼풀|어린이|아이|베이비|팡팡|랜드|파크", re.I)
PFC_FOOD_NO = re.compile(r"한우|고기|갈비|삼겹|정육|식당|가든|뷔페|부페|회관|횟집|오리|치킨|피자|버거|맥도날드|롯데리아|아웃백|애슐리|빕스|쿠우쿠우|놀이방")

def _kc_norm(n):
    n = re.sub(r"\(.*?\)|주식회사|\(주\)|㈜|유한회사|놀이시설|놀이터|키즈카페|키즈 카페|kids ?cafe|점$|\s+", "", str(n or ""), flags=re.I)
    return n.lower()

def _pfc_kidscafes(have):
    """행정안전부 어린이놀이시설 정보(설치장소: 놀이제공영업소·식품접객업소)에서 광주·전남 실내 키즈카페를 찾아,
    이미 있는 가게(이름·주소·좌표가 같은 곳)는 빼고 새 곳만 돌려준다."""
    if not DATA_KEY:
        return [], "놀이시설: 키 없음"
    known_names = {_kc_norm(v["name"]) for v in have}
    known_addr = {re.sub(r"\s+", "", v.get("address", ""))[:24] for v in have if v.get("address")}
    out, fields, seen, total_rows = [], [], set(), 0
    for code, label in PFC_PLACES:
        page = 1
        while page <= 60:
            qs = urllib.parse.urlencode({"serviceKey": DATA_KEY, "pageIndex": page, "recordCountPerPage": 100, "instlPlaceCd": code, "returnType": "json", "type": "json"})
            raw = http_get(PFC_URL + "?" + qs, timeout=40).decode("utf-8", "replace")
            try:
                j = json.loads(raw)
            except ValueError:
                raise RuntimeError("JSON 아님 · " + re.sub(r"\s+", " ", raw)[:160])
            rows = _sd_rows(j)
            if not rows:
                break
            total_rows += len(rows)
            if not fields:
                fields = list(rows[0].keys())
            for r in rows:
                R = {str(k): ("" if v is None else str(v).strip()) for k, v in r.items()}
                low = {k.lower(): v for k, v in R.items()}
                nm = low.get("pfctnm") or next((v for k, v in low.items() if k.endswith("nm") and "plc" not in k and "place" not in k and v), "")
                addr = next((low[k] for k in ("ronaaddr", "rnaddr", "roadaddr", "lotnoaddr", "addr") if low.get(k)), "") or \
                       next((v for k, v in low.items() if "addr" in k and v), "")
                if not nm or not in_region(addr):
                    continue
                st = " ".join(v for k, v in low.items() if any(w in k for w in ("use", "opr", "oper", "stts", "prhb")))
                if re.search(r"이용금지|폐쇄|철거|휴업|폐업", st):
                    continue
                io = " ".join(v for k, v in low.items() if "idrodr" in k or "inout" in k or "inner" in k)
                if re.search(r"실외|야외", io) and not re.search(r"실내", io):
                    continue
                if code == "A004" and (not PFC_FOOD_OK.search(nm) or PFC_FOOD_NO.search(nm)):
                    continue                                   # 고깃집·패밀리레스토랑 놀이방 등은 빼고 키즈카페다운 곳만
                if KC_EXCLUDE.search(nm):
                    continue
                key = _kc_norm(nm)
                a24 = re.sub(r"\s+", "", addr)[:24]
                if key in known_names or a24 in known_addr or key in seen:
                    continue
                seen.add(key)
                lat = lng = None
                for k, v in low.items():
                    try:
                        f = float(v)
                    except ValueError:
                        continue
                    if lat is None and ("lat" in k or k.endswith("_y") or k == "y") and 33 <= f <= 39:
                        lat = round(f, 6)
                    if lng is None and ("lot" in k or "lng" in k or "lon" in k or k.endswith("_x") or k == "x") and 124 <= f <= 132:
                        lng = round(f, 6)
                out.append({"id": "kcp-" + (low.get("pfctsn") or hashlib.sha1((nm + addr).encode("utf-8")).hexdigest()[:10]),
                            "name": re.sub(r"\s*놀이시설$", "", nm), "kind": "키즈카페", "district": district_from_addr(addr), "address": addr,
                            "phone": "", "lat": lat, "lng": lng, "rides": "", "since": (low.get("instldt") or low.get("instlymd") or "")[:10],
                            "inout": "실내", "src": "놀이시설·" + label})
            if len(rows) < 100:
                break
            page += 1
            time.sleep(0.3)
    nxy = sum(1 for x in out if x["lat"] and x["lng"])
    return out, f"놀이시설 새로 {len(out)} · 좌표 {nxy} (받은 줄 {total_rows} · 항목 {','.join(fields[:20])})"


# ════════════════ 한파쉼터·무더위쉼터 (재난안전데이터 공유플랫폼) · 2026-11-18 ════════════════
# 앱의 '🧣 한파쉼터 / 🌡 무더위쉼터' 칸이 읽는 docs/data/shelters.json 을 만든다.
# 공유플랫폼 자료는 월 1회 바뀌므로, 7일 안에 받아 둔 게 있으면 다시 받지 않는다(호출 수 아끼기).
# 응답 항목 이름이 문서마다 조금씩 달라서, 이름·주소·위도·경도 칸을 값 모양으로 찾아낸다.
OUT_SHELTERS = DATA_DIR / "shelters.json"
SAFETY_BASE = "https://www.safetydata.go.kr/V2/api/"
SHELTER_APIS = [("cold", "DSSP-IF-10804", lambda: SAFETY_KEY),        # 한파쉼터
                ("heat", "DSSP-IF-10942", lambda: SAFETY_HEAT_KEY)]   # 무더위쉼터 (2026-11-18 승인 · 키가 따로면 SAFETY_HEAT_KEY)

_SD_HOST = []   # 한 번 통한 주소는 이번 실행 내내 그대로 씀

def _sd_get(qs):
    """공유플랫폼은 해외(GitHub 서버)에서 접속하면 보안 연결(SSL)이 느리거나 끊기는 일이 있어,
    https → http, www 있는/없는 주소를 차례로 길게 기다리며 두 번씩 시도한다 (2026-11-18: handshake timed out)."""
    bases = _SD_HOST or ["https://www.safetydata.go.kr/V2/api/", "http://www.safetydata.go.kr/V2/api/", "https://safetydata.go.kr/V2/api/"]
    last = None
    for base in bases:
        for attempt in range(2):
            try:
                body = http_get(base + qs, timeout=90)
                if not _SD_HOST:
                    _SD_HOST.append(base)
                return body
            except Exception as e:
                last = e
                log(f"  ⚠ 쉼터 접속 재시도 ({base.split('/')[2]} · {attempt + 1}번째): {scrub(str(e))[:120]}")
                time.sleep(5)
    raise RuntimeError(f"공유플랫폼 접속 안 됨 (https·http 모두 시도) · {scrub(str(last))[:160]}")

def _sd_rows(j):
    """응답 JSON 안에서 '자료 줄 목록'을 찾아 돌려준다 (body / items / item 어디에 있든)."""
    best = []
    def walk(x):
        nonlocal best
        if isinstance(x, list) and x and all(isinstance(r, dict) for r in x[:5]):
            if len(x) > len(best):
                best = x
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(j)
    return best

def _sd_total(j):
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k.lower() in ("totalcount", "total_count", "totcnt") and str(v).isdigit():
                    return int(v)
                r = walk(v)
                if r is not None:
                    return r
        return None
    return walk(j)

def _hm(v):
    d = re.sub(r"\D", "", str(v or ""))
    if len(d) == 3: d = "0" + d
    return f"{d[:2]}:{d[2:4]}" if len(d) >= 4 and int(d[:2]) <= 24 else ""

def _sd_pick(row):
    """한 줄에서 이름·주소·위도·경도·운영시간·인원을 고른다.
    2026-11-18 실제 항목: REARE_NM(쉼터명) · RONA_DADDR(도로명주소)/DADDR · WKDY_OPER_BGNG_HR/…END_HR(평일 시간) ·
    UTZTN_PSBLTY_TNOP(이용가능인원) · FCLTY_SCLAS/FCLT_TYPE(시설 종류) · LAT/LOT(위도·경도). 모르는 이름이면 아래 값 모양 규칙으로."""
    R = {str(k).upper(): v for k, v in row.items()}
    g = lambda *ks: next((str(R[k]).strip() for k in ks if k in R and str(R[k] or "").strip()), "")
    nm, addr = g("REARE_NM", "RSTR_NM", "SHNT_NM", "FCLTY_NM"), g("RONA_DADDR", "DADDR", "RN_DTL_ADRES", "DTL_ADRES")
    b0, e0 = _hm(g("WKDY_OPER_BGNG_HR", "WKDY_OPER_BGNG_TM")), _hm(g("WKDY_OPER_END_HR", "WKDY_OPER_END_TM"))
    cap, kind = g("UTZTN_PSBLTY_TNOP", "USE_PSBL_NOPE"), g("FCLTY_SCLAS", "FCLTY_MCLAS", "FCLT_TYPE")
    if kind.isdigit(): kind = ""
    lat = lng = None
    for k in ("LAT", "LA", "YCORD", "Y"):
        try: lat = float(R[k]); break
        except (KeyError, TypeError, ValueError): pass
    for k in ("LOT", "LO", "LON", "XCORD", "X"):
        try: lng = float(R[k]); break
        except (KeyError, TypeError, ValueError): pass
    if lat is not None and not (33 <= lat <= 39): lat = None
    if lng is not None and not (124 <= lng <= 132): lng = None
    if nm and addr and lat is not None and lng is not None:
        return nm, addr, lat, lng, (f"{b0}~{e0}" if b0 and e0 else ""), cap, kind
    return _sd_pick_guess(row)

def _sd_pick_guess(row):
    """한 줄에서 이름·주소·위도·경도·운영시간·인원을 값 모양으로 고른다."""
    nm = addr = hours = ""; lat = lng = None; cap = ""; kind = ""
    for k, v in row.items():
        ku, sv = str(k).upper(), str(v if v is not None else "").strip()
        if not sv:
            continue
        try:
            fv = float(sv)
        except ValueError:
            fv = None
        if fv is not None and 33 <= fv <= 39 and lat is None and ("LAT" in ku or ku in ("LA", "Y", "YCRD", "LAT_CRD") or ku.endswith("_LA")):
            lat = fv; continue
        if fv is not None and 124 <= fv <= 132 and lng is None and ("LOT" in ku or "LON" in ku or "LNG" in ku or ku in ("LO", "X", "XCRD") or ku.endswith("_LO")):
            lng = fv; continue
        if ("ADDR" in ku or "ADRES" in ku or "ADR" in ku) and len(sv) > len(addr) and re.search(r"[시군구]\s", sv + " "):
            addr = sv; continue
        if ("NM" in ku or "NAME" in ku) and not nm and not re.search(r"(CTPV|SGG|SIDO|SIGUN|EMD|TYPE|SE_|_SE|CLSF|CL_)", ku):
            nm = sv; continue
        if ("CL" in ku or "TYPE" in ku or "SE" in ku) and ("NM" in ku) and not kind:
            kind = sv; continue
        if ("CPCT" in ku or "PSBL" in ku or "NOPE" in ku or "PERSON" in ku) and sv.isdigit():
            cap = sv; continue
        if re.fullmatch(r"\d{1,2}:?\d{2}", sv) or re.fullmatch(r"\d{4}", sv):
            hours = (hours + "~" + (sv if ":" in sv else sv[:2] + ":" + sv[2:])) if hours else (sv if ":" in sv else sv[:2] + ":" + sv[2:])
    return nm, addr, lat, lng, hours, cap, kind

def build_shelters():
    old = {}
    try:
        old = json.loads(OUT_SHELTERS.read_text(encoding="utf-8"))
    except Exception:
        old = {}
    old_items = old.get("items", []) if isinstance(old, dict) else []
    fresh = isinstance(old, dict) and old.get("v") == 2 and old.get("updated", "") >= (NOW - timedelta(days=7)).strftime("%Y-%m-%d")   # v2: 항목 이름 맞춤(11-18)
    notes, items = [], [x for x in old_items]
    for typ, api, keyf in SHELTER_APIS:
        key = keyf()
        if not key:
            notes.append(f"{typ}: 키 없음(SAFETY_DATA_KEY)"); continue
        if fresh and any(x.get("type") == typ for x in old_items):
            notes.append(f"{typ}: 7일 안에 받은 자료 사용"); continue
        got, fields, page, total = [], [], 1, None
        while page <= 120:
            qs = api + "?" + urllib.parse.urlencode({"serviceKey": key, "pageNo": page, "numOfRows": 1000, "returnType": "json"})
            j = json.loads(_sd_get(qs).decode("utf-8", "replace"))
            rows = _sd_rows(j)
            if total is None:
                total = _sd_total(j)
            if not rows:
                if page == 1:
                    raise RuntimeError(scrub("자료 줄을 못 찾음 · " + json.dumps(j, ensure_ascii=False)[:200]))
                break
            if not fields:
                fields = list(rows[0].keys())
            for r in rows:
                nm, addr, lat, lng, hours, cap, kind = _sd_pick(r)
                if not nm or not (in_region(addr) or (lat and lng and 33.8 <= lat <= 35.6 and 125.0 <= lng <= 127.9 and not addr)):
                    continue
                it = {"id": f"{typ}-" + hashlib.sha1((nm + addr).encode("utf-8")).hexdigest()[:10], "type": typ, "name": nm,
                      "address": addr, "district": district_from_addr(addr), "lat": round(lat, 5) if lat else lat, "lng": round(lng, 5) if lng else lng,
                      "hours": ("평일 " + hours) if hours else "", "facility": " · ".join(x for x in (kind, f"{cap}명" if cap and cap != "0" else "") if x)}
                got.append({k: v for k, v in it.items() if v not in ("", None)})
            if total is not None and page * 1000 >= total:
                break
            if len(rows) < 1000:
                break
            page += 1
            time.sleep(0.4)
        if got:
            items = [x for x in items if x.get("type") != typ] + got
        nxy = sum(1 for x in got if x.get("lat") and x.get("lng")); nhr = sum(1 for x in got if x.get("hours"))
        notes.append(f"{typ}: {len(got)}곳 · 좌표 {nxy} · 시간 {nhr} (전국 {total or '?'}줄 · {page}쪽 · 항목 {','.join(fields[:24])})")
    items.sort(key=lambda x: (x.get("type", ""), x.get("district", ""), x.get("name", "")))
    if items != old_items or not fresh:
        doc = {"v": 2, "updated": TODAY_ISO if any("곳 (" in n for n in notes) else old.get("updated", TODAY_ISO) if isinstance(old, dict) else TODAY_ISO,
               "source": "행정안전부 재난안전데이터 공유플랫폼 (한파쉼터·무더위쉼터)", "items": items}
        OUT_SHELTERS.parent.mkdir(parents=True, exist_ok=True)
        OUT_SHELTERS.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")   # 9천 곳이 넘어 빈칸 없이 작게
    return len(items), " / ".join(notes)


def main():
    global VENUES
    data = embedded_data()
    VENUES = data.get("venues", [])
    manual = [e for e in data.get("events", []) if e.get("origin", "manual") == "manual"]
    rec = recurring_events()
    rec_ids = {e["id"] for e in rec}
    manual = [e for e in manual if e.get("id") not in rec_ids] + rec   # 정기 행사도 '직접 확인한 일정'으로 취급
    log(f"✔ 정기 프로그램 {len(rec)}건 ({', '.join(e['title'] for e in rec)})")

    # 시·군 축제 보강 목록 (관광공사 API에 안 잡히는 광양·강진 등). 이 파일 하나만 고치면 됨.
    extra_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "festivals_extra.json")
    try:
        extra = json.load(open(extra_path, encoding="utf-8")) if os.path.exists(extra_path) else []
        extra = [dict(e, origin="manual") for e in (extra if isinstance(extra, list) else extra.get("events", []))
                 if e.get("title") and e.get("start")]
        have = {e.get("id") for e in manual}
        extra = [e for e in extra if e.get("id") not in have and not any(same_event(e, m) for m in manual)]
        manual += extra
        log(f"✔ 시·군 축제 보강 {len(extra)}건")
    except Exception as ex:
        log(f"⚠ festivals_extra.json 읽기 실패 (무시하고 계속): {ex}")

    MANUAL_FOR_WATCH[:] = manual            # 관심 축제: 직접 넣은 일정에 올해 것이 있으면 '못 찾음' 경고 안 냄
    previous = read_json(OUT_EVENTS, [])
    for e in previous:
        if e.get("id"):
            PREV[e["id"]] = e

    api_events, statuses = [], []
    old_meta = read_json(OUT_META, {})
    KOPIS_GAP_H = 11.5            # KOPIS 는 자주 물으면 막는다 → 하루 한 번(09:10 실행)만 묻고, 18:10 은 받아 둔 자료 사용
    for source_id, source_name, fn in [
        ("tourapi", "관광공사 행사·축제", tourapi_festivals),
        ("watch", "관심 축제 지키기", watch_festivals),
        ("kopis", "KOPIS 공연", kopis_performances),
        ("culture", "문화정보원 전시·체험", culture_events),
        ("gma", "광주시립미술관 전시", gma_exhibitions),
        ("stdevents", "공공데이터 표준(축제·공연행사)", std_csv_events),
    ]:
        if source_id == "kopis":
            last = old_meta.get("kopis_last_try")
            try:
                gap = (NOW - datetime.fromisoformat(last)).total_seconds() / 3600 if last else 99
            except ValueError:
                gap = 99
            last_ok = old_meta.get("kopis_last_ok")
            failed_last = any(x.get("id") == "kopis" and not x.get("ok", True)   # 지난번 상태가 실패였으면
                              for x in old_meta.get("sources", []))
            try:
                ok_gap = (NOW - datetime.fromisoformat(last_ok)).total_seconds() / 3600 if last_ok else None
            except ValueError:
                ok_gap = None
            gap_limit = 6 if failed_last else KOPIS_GAP_H     # 아침에 실패했으면 저녁(18:10)에 한 번 더 시도
            if gap < gap_limit and os.environ.get("KOPIS_FORCE") != "1":
                kept = [p for p in previous if p.get("api") == "kopis" or p.get("origin") == "kopis"]
                api_events.extend(kept)
                age_txt = f"{int(ok_gap)}시간 전" if ok_gap is not None else "지난번"
                if failed_last:
                    st = {"ok": False, "message": f"지난 수집 실패 · 다음 실행 때 다시 시도 · {age_txt} 자료 {len(kept)}건 사용"}
                else:
                    st = {"ok": True, "message": f"하루 한 번만 받아요 · {age_txt} 자료 {len(kept)}건 사용"}
                statuses.append({"id": source_id, "name": source_name, "count": len(kept), **st})
                log(f"⏭ {source_name}: {gap:.1f}시간 전에 물어봐서 이번엔 건너뜀 (차단 방지)")
                continue
            old_meta["kopis_last_try"] = NOW.isoformat(timespec="seconds")
        try:
            t0 = time.time(); log(f"▶ {source_name} 수집 시작")
            rows = fn()
            log(f"✔ {source_name} {len(rows)}건 ({int(time.time() - t0)}초)")
            api_events.extend(rows)
            if source_id == "watch":
                st = {"id": "watch", "name": source_name, "ok": not WATCH_MISSING, "count": len(rows),
                      "message": (f"일정 못 찾음: {', '.join(WATCH_MISSING)} → scripts/festivals_extra.json 에 직접 넣어 주세요"
                                  if WATCH_MISSING else (f"찾음: {', '.join(WATCH_FOUND)}" if WATCH_FOUND else "지켜보는 중 (열릴 때가 아님)"))}
                statuses.append(st)
                continue
            if source_id == "kopis":
                old_meta["kopis_last_ok"] = old_meta.get("kopis_last_try")
            statuses.append({"id": source_id, "name": source_name, "ok": True, "count": len(rows),
                             "message": "정상 수집" + (f" ({CULTURE_MODE})" if source_id == "culture" and CULTURE_MODE else "")
                                        + (f" ({STD_NOTE})" if source_id == "stdevents" and STD_NOTE else "")})
        except Exception as e:
            # 한 곳이 실패하면 그 출처의 지난번 자료를 그대로 유지
            kept = [p for p in previous if p.get("api") == source_id or p.get("origin") == source_id]
            api_events.extend(kept)
            statuses.append({"id": source_id, "name": source_name, "ok": False, "count": 0,
                             "message": scrub(f"수집 실패 · 지난 자료 {len(kept)}건 유지 ({str(e)[:200]})")})

    api_events = [t for t in (tidy_event(e) for e in api_events) if t]   # 지난 자료에도 정리 규칙 적용
    merged = merge_events(manual, api_events)
    if api_events or not previous:
        write_json(OUT_EVENTS, merged)
    else:
        merged = previous                            # 전부 실패: 기존 배포 데이터 유지

    auto_n = sum(1 for e in merged if e.get("origin") == "auto")
    meta = {
        "updated_at": NOW.isoformat(timespec="seconds"),
        "auto_update": True,
        "sources": statuses,                         # 앱 상단 상태 표시가 읽는 키
        "api_status": statuses,                      # (예전 이름 · 호환용)
        "counts": {"total": len(merged), "auto": auto_n, "manual": len(merged) - auto_n},
        "manual_count": len(manual),
        "events_count": len(merged),
        "note": "직접 확인한 일정 우선 병합 · 출처별 실패 시 지난 자료 유지",
    }
    try:                                             # 🧸 키즈카페 (실패해도 일정 수집과 무관)
        n_kc, kc_note = build_kidscafes()
        meta["kidscafes"] = {"count": n_kc or 0, "message": kc_note}
    except Exception as e:
        meta["kidscafes"] = {"count": 0, "message": scrub(f"키즈카페 만들기 실패 ({str(e)[:120]})")}
    try:
        n_sh, sh_note = build_shelters()
        meta["shelters"] = {"count": n_sh or 0, "message": sh_note}
        log(f"✔ 쉼터 {n_sh}곳 · {sh_note}")
    except Exception as e:
        meta["shelters"] = {"count": 0, "message": scrub(f"쉼터 수집 실패: {e}")[:300]}
        log("  ⚠ 쉼터 수집 실패:", scrub(str(e))[:200])
    for k in ("local", "place_fill", "kopis_last_try", "kopis_last_ok"):   # 다른 수집기가 남긴 기록은 유지
        if k in old_meta:
            meta[k] = old_meta[k]
    write_json(OUT_META, meta)
    print(json.dumps({k: v for k, v in meta.items() if k not in ("local", "place_fill")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
