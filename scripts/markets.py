#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
전통시장 보강: 공공데이터포털 「전국전통시장표준데이터」 CSV 로 광주·전남 시장을 빠짐없이 채운다.

사용법: data.go.kr 에서 '전국전통시장표준데이터' CSV 를 받아 저장소 맨 바깥(또는 raw/)에 올리면
       enrich_places.py 가 실행될 때 앱 장소 목록(venues)에 없는 시장만 덧붙인다.
       (도서관 CSV 와 같은 방식 · 이미 직접 적어둔 시장은 건드리지 않음)

장날: '시장개설주기'의 '2, 7일' / '5일장(1·6일)' / '12,17,22,27일' 같은 표기를 끝자리 숫자로 바꿔
      market.days 에 넣는다. → 앱의 '오늘 장 서는 곳'에 바로 반영.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_data as u          # noqa: E402
import collect_local as cl       # noqa: E402  (CSV 읽기 도우미 재사용)

IN_AREA = re.compile(r"^\s*(광주|전라남도|전남)")   # 주소 맨 앞이 광주·전남인 것만 (본문에 '광주'가 들어간 타지역 제외)
REGION_SIDO = ("광주광역시", "전라남도", "전남광주통합특별시", "전남광주특별시")
SUFFIX = re.compile(r"(전통|재래|상설|정기|민속|5일|오일|공설|종합)*(시장|장터)$")


def core(name: str) -> str:
    n = re.sub(r"\([^)]*\)", "", name or "")
    n = u.norm_title(n)
    return SUFFIX.sub("", n) or n


def short_district(d: str) -> str:
    return re.sub(r"(시|군|구)$", "", d or "")


def ckey(name: str, district: str) -> str:
    """'광양 옥곡정기시장' · '옥곡5일시장' → '옥곡' (앞의 시·군 이름도 뗌)"""
    c = re.sub(r"^광주(?=.{2})", "", core(name)).replace("예술", "")
    sd = short_district(district)
    if sd and c.startswith(sd) and len(c) > len(sd):
        c = c[len(sd):]
    return c


def similar(a: str, b: str) -> bool:
    if a == b:
        return True
    s, l = sorted((a, b), key=len)
    # '남광주'↔'남광주해뜨는', '대인'↔'대인(예술)' 처럼 앞부분이 같으면 같은 시장(묶음)으로 본다
    return len(s) >= 2 and (l.startswith(s) or (s in l and len(s) / len(l) >= 0.7))


def parse_days(cycle: str):
    """'5일장(2,7일)' → [2, 7] · '매일'·'상설' → None"""
    c = (cycle or "").strip()
    if not c or re.search(r"매일|상설|연중|상시", c) and not re.search(r"\d+\s*[,·.]\s*\d+", c):
        return None
    c = re.sub(r"[5오]\s*일\s*장", " ", c)          # '5일장' 의 5 는 장날이 아님
    nums = [int(n) for n in re.findall(r"\d+", c) if 0 < int(n) <= 31]
    days = sorted({n % 10 for n in nums})
    return days or None


def _std_row(r):
    p = lambda *n: cl.pick(r, *n)
    name = p("시장명", "mrktNm")
    addr = p("소재지도로명주소", "rdnmadr") or p("소재지지번주소", "lnmadr")
    sido = p("시도명", "ctprvnNm")
    return name, addr, sido, p


def std_markets(existing: list[dict]) -> list[dict]:
    rows, fname = cl.read_csv_rows("시장명", {})
    if rows is None:
        print("  전통시장 CSV 없음 → 시장 보강 건너뜀 (data.go.kr '전국전통시장표준데이터')", flush=True)
        return []
    have = {}
    for v in existing:
        if re.search(r"시장|장터", v.get("name", "")) or v.get("market"):
            d = u.district_from_addr(v.get("address", "")) or v.get("district", "")
            have.setdefault(d, set()).add(ckey(v.get("name"), d))

    out, seen = [], set()
    for r in rows:
        name, addr, sido, p = _std_row(r)
        if not name or not addr:
            continue
        if sido and sido not in REGION_SIDO:
            continue
        if not sido and not IN_AREA.match(addr):
            continue
        district = u.district_from_addr(addr)
        if not district:
            continue
        c = ckey(name, district)
        if any(similar(c, k) for k in have.get(district, set())):
            continue                                    # 이미 앱에 있는 시장
        key = district + c
        if key in seen:
            continue
        seen.add(key)

        cycle = p("시장개설주기", "mrktEstblCycle", "개설주기")
        days = parse_days(cycle)
        items = [x.strip() for x in re.split(r"[,·/+]", p("취급품목", "trtmntPrdlst")) if x.strip()][:6]
        mtype = "5일장" if days else (p("시장유형", "mrktType") or "상설시장")
        parking = p("주차장보유여부", "prkplceYn")
        stores = p("점포수", "storNumber")
        if not name.startswith(short_district(district)) and (len(core(name)) <= 2 or core(name) in ("중앙", "공설", "매일", "신", "동부", "서부", "남부", "북부", "종합")):
            name = f"{re.sub(r'(시|군)$', '', district)} {name}"     # '중앙시장' → '여수 중앙시장'
        out.append({
            "id": "mk-" + hashlib.md5(key.encode()).hexdigest()[:10],
            "name": name,
            "kind": "골목·시장",
            "district": district,
            "address": addr,
            "phone": p("전화번호", "phoneNumber"),
            "hours": "장날 오전이 가장 붐벼요" if days else "점포별 상이",
            "closed": "점포별 상이",
            "price": "무료",
            "parking": "주차장 있음" if parking.upper() in ("Y", "있음") else ("주차장 없음" if parking.upper() in ("N", "없음") else ""),
            "kids": "",
            "url": p("홈페이지주소", "homepageUrl"),
            "inout": "야외",
            "desc": f"{district} {mtype}" + (f" · 점포 약 {stores}곳" if stores and stores != "0" else "") + (f" · {', '.join(items[:4])}" if items else ""),
            "note": ("장날: " + "·".join(str(d or 10) for d in sorted(days, key=lambda d: d or 10)) + "일") if days else ("매일 열어요" if not cycle or re.search(r"매일|상설", cycle) else cycle),
            "aliases": [],
            "lat": u.to_float(p("위도", "latitude")),
            "lng": u.to_float(p("경도", "longitude")),
            "market": {
                "type": mtype,
                **({"days": days} if days else {}),
                "eats": [],
                "buy": items,
                "tip": "장날·운영시간은 상인회나 시·군 안내로 한 번 더 확인하세요.",
                "src": f"전국전통시장표준데이터 ({p('데이터기준일자', 'referenceDate') or fname})",
            },
            "verified": u.TODAY_ISO,
            "infoSource": "전국전통시장표준데이터",
        })
    print(f"  전통시장 표준데이터({fname}): 앱에 없던 시장 {len(out)}곳 추가", flush=True)
    return out


# ───────── 박물관·미술관: 「전국박물관미술관정보표준데이터」 CSV ─────────
MUSEUM_SUFFIX = re.compile(r"(시립|군립|도립|국립|사립)?")


def mcore(name: str) -> str:
    n = u.norm_title(re.sub(r"\([^)]*\)", "", name or ""))
    return re.sub(r"^(국립|시립|군립|도립|전남|광주|전라남도)", "", n)


def _hm(v):
    m = re.match(r"(\d{1,2}):?(\d{2})", (v or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def _won(v):
    v = (v or "").strip()
    if not v:
        return ""
    if re.fullmatch(r"0+|무료", v):
        return "무료"
    if v.isdigit():
        return f"{int(v):,}원" if int(v) <= 50000 else ""     # 1000020 같은 입력 오류는 버림
    return v


def std_museums(existing: list[dict]) -> list[dict]:
    rows, fname = cl.read_csv_rows("박물관미술관구분", {})
    if rows is None:
        print("  박물관·미술관 CSV 없음 → 보강 건너뜀 (data.go.kr '전국박물관미술관정보표준데이터')", flush=True)
        return []
    have = {}
    for v in existing:
        d = u.district_from_addr(v.get("address", "")) or v.get("district", "")
        have.setdefault(d, set()).add(mcore(v.get("name")))

    out, seen = [], set()
    for r in rows:
        p = lambda *n: cl.pick(r, *n)
        name = p("시설명", "fcltyNm")
        addr = p("소재지도로명주소", "rdnmadr") or p("소재지지번주소", "lnmadr")
        if not name or not addr or not IN_AREA.match(addr):
            continue
        district = u.district_from_addr(addr)
        if not district:
            continue
        c = mcore(name)
        cores = have.get(district, set())
        if c in cores or any(len(c) >= 3 and len(k) >= 3 and (c in k or k in c) for k in cores):
            continue
        key = district + c
        if key in seen:
            continue
        seen.add(key)

        closed = p("휴관정보", "rstdeInfo")
        if re.search(r"휴관\s*중|폐관|리모델링", closed):
            continue                                    # 지금 문 닫은 곳은 넣지 않음
        owner = p("박물관미술관구분", "fcltyType")      # 공립·사립·국립·대학
        kind = "미술관" if "미술" in name or "미술" in owner else "박물관"
        if not re.search(r"(박물관|미술관|기념관|전시관|문학관|역사관|갤러리|관)$", re.sub(r"\([^)]*\)", "", name).strip()):
            name = f"{name} ({owner or ''} 박물관·미술관)".replace("( ", "(")   # '미암' 처럼 이름만 있는 곳
        wk = (_hm(p("평일관람시작시각", "weekdayOperOpenHhmm")), _hm(p("평일관람종료시각", "weekdayOperColseHhmm", "weekdayOperCloseHhmm")))
        hd = (_hm(p("공휴일관람시작시각", "holidayOperOpenHhmm")), _hm(p("공휴일관람종료시각", "holidayCloseOpenHhmm", "holidayOperCloseHhmm")))
        if wk[0] == "00:00":
            wk = ("", "")
        if hd[0] == "00:00":
            hd = ("", "")
        hours = " · ".join(x for x in [
            f"평일 {wk[0]}–{wk[1]}" if all(wk) and wk[0] != wk[1] else "",
            f"휴일 {hd[0]}–{hd[1]}" if all(hd) and hd[0] != hd[1] and hd != wk else "",
        ] if x) or "운영시간 공식 확인"
        adult, youth, child = (_won(p("어른관람료", "adultChrge")), _won(p("청소년관람료", "yngbgsChrge")), _won(p("어린이관람료", "childChrge")))
        if adult == youth == child == "무료":
            price = "무료"
        else:
            price = " · ".join(x for x in [f"어른 {adult}" if adult else "", f"청소년 {youth}" if youth else "", f"어린이 {child}" if child else ""] if x)
        extra = p("관람료기타정보", "etcChrgeInfo")
        if re.fullmatch(r"0+|무료|-", extra) or (extra == "무료" and price == "무료"):
            extra = ""
        if len(extra) > 50:
            extra = extra[:50] + "…"
        if not price and extra:
            price = "요금 공식 확인"
        price = (price + (" · " + extra if extra else "")).strip(" ·") or "공식 안내 확인"
        intro = p("박물관미술관소개", "fcltyIntrcn")
        out.append({
            "id": "mu-" + hashlib.md5(key.encode()).hexdigest()[:10],
            "name": name,
            "kind": kind,
            "district": district,
            "address": addr,
            "phone": p("운영기관전화번호", "operPhoneNumber"),
            "hours": hours,
            "closed": closed or "공식 안내 확인",
            "price": price,
            "parking": "",
            "kids": "",
            "url": p("운영홈페이지", "homepageUrl"),
            "inout": "실내",
            "desc": (intro[:180] + ("…" if len(intro) > 180 else "")) if intro else f"{district}의 {owner + ' ' if owner else ''}{kind}.",
            "access": p("교통안내정보", "trnsportInfo")[:120],
            "note": "",
            "aliases": [],
            "lat": u.to_float(p("위도", "latitude")),
            "lng": u.to_float(p("경도", "longitude")),
            "verified": u.TODAY_ISO,
            "infoSource": f"전국박물관미술관정보표준데이터 ({p('데이터기준일자', 'referenceDate') or fname})",
        })
    print(f"  박물관·미술관 표준데이터({fname}): 앱에 없던 곳 {len(out)}곳 추가", flush=True)
    return out


# ───────── 이미 앱에 있는 시장의 좌표 바로잡기 ─────────
# 예전에 손으로 넣은 5일장(m5-…)은 좌표를 시·군 중심 한 점으로 찍어 둔 곳이 많고(나주 시장 7곳이 한 점),
# 고흥 시장 4곳은 엉뚱하게 목포 좌표가 들어가 있었다. 표준데이터 CSV 에 정확한 좌표가 있으면 그걸로 바꾼다.
def _km(a_lat, a_lng, b_lat, b_lng):
    import math
    r = math.radians
    h = math.sin(r(b_lat - a_lat) / 2) ** 2 + math.cos(r(a_lat)) * math.cos(r(b_lat)) * math.sin(r(b_lng - a_lng) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def fix_market_coords(venues: list[dict]) -> list[dict]:
    rows, fname = cl.read_csv_rows("시장명", {})
    if rows is None:
        return venues
    idx = {}
    for r in rows:
        name, addr, sido, p = _std_row(r)
        lat, lng = u.to_float(p("위도", "latitude")), u.to_float(p("경도", "longitude"))
        district = u.district_from_addr(addr or "")
        if name and district and lat and lng and 33.9 < lat < 35.6 and 125.0 < lng < 127.95:
            idx.setdefault(district, []).append((ckey(name, district), lat, lng))
    fixed = 0
    out = []
    for v in venues:
        y = v
        if v.get("market") or re.search(r"시장|장터", v.get("name", "")):
            d = u.district_from_addr(v.get("address", "")) or v.get("district", "")
            c = ckey(v.get("name"), d)
            hit = next(((la, ln) for k, la, ln in idx.get(d, []) if similar(c, k)), None)
            if hit:
                cur = (v.get("lat"), v.get("lng"))
                far = not cur[0] or _km(float(cur[0]), float(cur[1]), hit[0], hit[1]) > 0.8
                if far:
                    y = {**v, "lat": round(hit[0], 6), "lng": round(hit[1], 6), "geoSrc": "전국전통시장표준데이터"}
                    y.pop("approx", None)
                    fixed += 1
        out.append(y)
    print(f"  시장 좌표 바로잡기({fname}): {fixed}곳", flush=True)
    return out
