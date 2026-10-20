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
import html, os, json, re, time, urllib.error, urllib.parse, urllib.request
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
    return e

def match_venue(name, addr=""):
    """'전남광주통합특별시 예술의전당 (구. 광주예술의전당)' → 앱의 'artcenter' 장소로 연결"""
    n = norm_title(name)
    if len(n) < 3:
        return None
    best = None
    for v in VENUES:
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
    for k in {DATA_KEY, KOPIS_KEY, urllib.parse.quote(DATA_KEY or "", safe=""), urllib.parse.quote_plus(DATA_KEY or "")}:
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
        return match.group(1).strip()

    return text

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
                district = district_from_addr(addr) or district_from_addr(" ".join([r.get("area", ""), r.get("sigungu", "")]))
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
                    "url": extract_api_url(d.get("url") or d.get("placeUrl") or ""),
                    "origin": "auto",
                    "api": "culture",
                    "verified": TODAY_ISO,
                    "tags": [t for t in [cat, sub, region_tag(district),
                                         "무료" if re.search(r"무료", price or "") else "",
                                         "체험" if tp == "C" else ""] if t],
                })
            if not recs or new_on_page == 0:
                break            # 더 받을 게 없음 (한 쪽에 몇 개씩 주든 상관없이)
        log(f"  문화정보원 {tp_name}: 지금까지 광주·전남 {len(out)}건")
    CULTURE_MODE = f"{'지역별' if use_area[0] else '기간별'} 조회 · {pages_read}쪽 읽음"
    return out


KOPIS_BASE = "http://www.kopis.or.kr/openApi/restful"
KOPIS_MUSIC = ("대중음악", "서양음악(클래식)", "한국음악(국악)", "클래식", "국악")

def kopis_xml(path, **params):
    time.sleep(0.5)          # KOPIS 는 짧은 시간에 많이 물으면 차단(Request Blocked)한다 → 천천히
    params["service"] = KOPIS_KEY
    url = f"{KOPIS_BASE}/{path}?" + urllib.parse.urlencode(params)
    root = ET.fromstring(http_get(url))
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

    previous = read_json(OUT_EVENTS, [])
    for e in previous:
        if e.get("id"):
            PREV[e["id"]] = e

    api_events, statuses = [], []
    old_meta = read_json(OUT_META, {})
    KOPIS_GAP_H = 11.5            # KOPIS 는 자주 물으면 막는다 → 하루 한 번(09:10 실행)만 묻고, 18:10 은 받아 둔 자료 사용
    for source_id, source_name, fn in [
        ("tourapi", "관광공사 행사·축제", tourapi_festivals),
        ("kopis", "KOPIS 공연", kopis_performances),
        ("culture", "문화정보원 전시·체험", culture_events),
        ("gma", "광주시립미술관 전시", gma_exhibitions),
    ]:
        if source_id == "kopis":
            last = old_meta.get("kopis_last_try")
            try:
                gap = (NOW - datetime.fromisoformat(last)).total_seconds() / 3600 if last else 99
            except ValueError:
                gap = 99
            if gap < KOPIS_GAP_H and os.environ.get("KOPIS_FORCE") != "1":
                kept = [p for p in previous if p.get("api") == "kopis" or p.get("origin") == "kopis"]
                api_events.extend(kept)
                statuses.append({"id": source_id, "name": source_name, "ok": True, "count": len(kept),
                                 "message": f"하루 한 번만 받아요 · {int(gap)}시간 전 자료 {len(kept)}건 사용"})
                log(f"⏭ {source_name}: {gap:.1f}시간 전에 물어봐서 이번엔 건너뜀 (차단 방지)")
                continue
            old_meta["kopis_last_try"] = NOW.isoformat(timespec="seconds")
        try:
            t0 = time.time(); log(f"▶ {source_name} 수집 시작")
            rows = fn()
            log(f"✔ {source_name} {len(rows)}건 ({int(time.time() - t0)}초)")
            api_events.extend(rows)
            statuses.append({"id": source_id, "name": source_name, "ok": True, "count": len(rows),
                             "message": "정상 수집" + (f" ({CULTURE_MODE})" if source_id == "culture" and CULTURE_MODE else "")})
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
    for k in ("local", "place_fill", "kopis_last_try"):   # 다른 수집기가 남긴 기록은 유지
        if k in old_meta:
            meta[k] = old_meta[k]
    write_json(OUT_META, meta)
    print(json.dumps({k: v for k, v in meta.items() if k not in ("local", "place_fill")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
