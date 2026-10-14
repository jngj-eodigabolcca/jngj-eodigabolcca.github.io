#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
장소 정보 자동 보강기 (GitHub Actions에서 하루 두 번, 일정 수집 뒤에 실행)

index.html 안의 장소·맛집·빵집·멍카페·영화관 목록에서 빈칸만 채워
docs/data/{venues,restaurants,bakeries,cafes,cinemas}.json 으로 내보낸다.
앱은 이 파일이 있으면 내장 데이터 대신 이걸 쓴다.

채우는 것 (직접 입력한 값은 절대 덮어쓰지 않음)
  · 지도 좌표(lat/lng)   : 한국관광공사 TourAPI → 없으면 OpenStreetMap(Photon·Nominatim)
  · 맛집 영업시간·쉬는 날·대표메뉴·전화 : TourAPI 음식점 상세
  · 장소 전화·대표사진    : TourAPI (사진은 비어 있을 때만 · 공공누리 사진, 출처 '한국관광공사' 표기)

원칙: 잘못된 핀보다 핀 없음이 낫다.
  이름이 맞고 같은 구·시·군일 때만, 또는 주소의 도로명+건물번호가 맞을 때만 받아들인다.

찾은 결과는 scripts/place_cache.json 에 저장해서 다음부터는 API를 다시 부르지 않는다.
(못 찾은 곳은 14일 뒤에 다시 시도)
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_data as u  # 같은 폴더의 수집기에서 공통 함수 재사용

CACHE_FILE = u.ROOT / "scripts" / "place_cache.json"
LISTS = {  # 앱 데이터 이름 : 종류
    "venues": "place",
    "restaurants": "food",
    "bakeries": "food",
    "cafes": "food",
    "cinemas": "place",
}
MAX_TOUR_CALLS = 450        # 하루 호출 한도(개발계정 1,000회)를 넘지 않도록 한 번 실행당 상한
RUN_BUDGET = 15 * 60        # 이 시간이 지나면 찾기를 멈추고 지금까지 결과를 저장 (나머지는 다음 실행 때)
MAX_OSM_LOOKUPS = 400       # OpenStreetMap은 1초에 1번 규칙 → 한 번 실행에 약 8분 이내
RETRY_MISS_DAYS = 14
IMG_TRY_V = 2          # 사진 찾기 방식을 고치면 숫자를 올려서 사진 없는 곳을 한 번 더 찾아본다
IMG_RETRY_MAX = 40     # 한 번 실행에 사진 다시 찾기는 40곳까지 (관광공사 호출 한도 보호)
UA = "gwangju-eodi/1.0 (https://github.com/jngj-eodigabolcca/jngj-eodigabolcca.github.io)"

tour_calls = 0
osm_calls = 0


def in_area(lat, lng):
    return lat is not None and lng is not None and 33.9 < lat < 35.6 and 125.0 < lng < 127.95


def core_name(name: str) -> str:
    """'말바우시장 (2·4·7·9일장)' → '말바우시장'"""
    n = re.split(r"[(\[·|/]", name or "")[0]
    return u.norm_title(n)


def item_key(kind: str, x: dict) -> str:
    return f"{kind}|{x.get('name') or x.get('title')}|{x.get('address', '')}"


def street_and_no(addr: str):
    """'광주 북구 하서로 52' → ('하서로', '52')"""
    m = re.search(r"([가-힣0-9·]+(?:로|길))\s*(\d+(?:-\d+)?)", addr or "")
    return (m.group(1), m.group(2)) if m else (None, None)


# ───────── 한국관광공사 TourAPI ─────────
def tour_get(op: str, **params):
    global tour_calls
    if tour_calls >= MAX_TOUR_CALLS:
        raise RuntimeError("이번 실행의 TourAPI 호출 상한 도달")
    tour_calls += 1
    base = {"serviceKey": u.DATA_KEY, "MobileOS": "ETC", "MobileApp": "gwangju-eodi", "_type": "json"}
    base.update(params)
    url = f"https://apis.data.go.kr/B551011/KorService2/{op}?" + urllib.parse.urlencode(base)
    j = json.loads(u.http_get(url).decode("utf-8"))
    body = ((j or {}).get("response") or {}).get("body") or {}
    items = (body.get("items") or {}) if isinstance(body.get("items"), dict) else {}
    items = items.get("item") or []
    return [items] if isinstance(items, dict) else items


def tour_find(x: dict, kind: str):
    name = x.get("name") or ""
    core = core_name(name)
    if len(core) < 2:
        return None
    district = x.get("district") or u.district_from_addr(x.get("address", ""))
    base = re.split(r"[(\[]", name)[0].strip()
    # 관광공사 검색은 띄어쓰기·기호까지 맞아야 걸려서 여러 표기로 찾아본다
    # 예: '광주-기아 챔피언스필드' → '광주기아 챔피언스필드', '챔피언스필드' / '광주 월드컵경기장' → '월드컵경기장'
    keywords = []
    longest = max(re.split(r"[\s\-·]+", base), key=len)
    for k in (base, longest, base.replace("-", " "), base.replace("-", "").replace(" ", ""),
              re.sub(r"^(광주|전남)[\s\-]*", "", base), re.sub(r"(필드|경기장|공원|미술관|박물관)$", r" \1", longest)):
        k = k.strip()
        if len(k) >= 2 and k not in keywords:
            keywords.append(k)
    items = []
    for kw in keywords[:5]:
        params = {"keyword": kw, "numOfRows": "20", "pageNo": "1", "arrange": "A"}
        if kind == "food":
            params["contentTypeId"] = "39"
        items = tour_get("searchKeyword2", **params)
        if any(u.in_region(it.get("addr1") or "") for it in items):
            break
    best = None
    for it in items:
        addr = it.get("addr1") or ""
        if not u.in_region(addr):
            continue
        t = u.norm_title(re.split(r"[(\[]", it.get("title") or "")[0])
        if not t or not (t == core or (len(core) >= 3 and (core in t or t in core))):
            continue
        if district and u.district_from_addr(addr) not in ("", district):
            continue                                    # 이름은 같아도 다른 동네 → 다른 곳
        score = (t == core) * 2 + (u.district_from_addr(addr) == district)
        if not best or score > best[0]:
            best = (score, it)
    return best[1] if best else None


def tour_food_detail(content_id: str):
    items = tour_get("detailIntro2", contentId=content_id, contentTypeId="39")
    return items[0] if items else {}


class Temporary(Exception):
    """네트워크 오류 등 일시적 실패: 캐시에 '못 찾음'으로 남기지 않는다."""


# ───────── OpenStreetMap (앱의 브라우저 위치찾기와 같은 규칙) ─────────
osm_fail = 0                # 지도 서비스 연속 실패 횟수


def osm_json(url: str):
    """OpenStreetMap 조회. 5번 연속 실패(차단·지연)하면 이번 실행에선 더 묻지 않는다."""
    global osm_calls, osm_fail
    if osm_fail >= 5:
        raise Temporary("OpenStreetMap 응답 없음 → 이번 실행은 건너뜀")
    osm_calls += 1
    time.sleep(1.1)                                     # 1초에 1번 이용 규칙
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        osm_fail = 0
        return data
    except Exception:
        osm_fail += 1
        if osm_fail == 5:
            print("  ⚠ OpenStreetMap이 응답하지 않아 이번 실행에선 관광공사 자료로만 찾습니다", flush=True)
        raise Temporary("OpenStreetMap 연결 실패")   # 네트워크 문제는 '못 찾음'이 아니라 '다음에 다시'



def osm_find(x: dict):
    if osm_calls >= MAX_OSM_LOOKUPS:
        raise Temporary("OpenStreetMap 조회 상한")   # 못 찾은 게 아니라 못 물어본 것 → 다음 실행 때
    name, addr = x.get("name") or "", x.get("address") or ""
    addr_q = re.sub(r"^광주\s", "광주광역시 ", re.sub(r"^전남\s", "전라남도 ", addr))
    core = core_name(name)
    district = x.get("district") or u.district_from_addr(addr)
    city = "광주" if district in u.GJ_GU else re.sub(r"[시군]$", "", district or "광주")
    street, no = street_and_no(addr)

    # 1) 이름 + 도시
    try:
        j = osm_json("https://photon.komoot.io/api/?limit=5&lat=35.16&lon=126.85&q=" + urllib.parse.quote(f"{name} {city}"))
        for f in j.get("features", []):
            lng, lat = f["geometry"]["coordinates"]
            pn = u.norm_title((f.get("properties") or {}).get("name", ""))
            if in_area(lat, lng) and len(core) >= 3 and pn and (core in pn or pn in core):
                return {"lat": round(lat, 6), "lng": round(lng, 6), "geoSrc": "OpenStreetMap(이름)"}
    except Temporary:
        raise
    except Exception:
        pass
    # 2) 주소 (도로명 + 건물번호가 정확히 맞을 때만)
    if street and no:
        try:
            j = osm_json("https://nominatim.openstreetmap.org/search?format=json&addressdetails=1&limit=3&countrycodes=kr&q="
                         + urllib.parse.quote(addr_q))
            for r in j or []:
                lat, lng = float(r["lat"]), float(r["lon"])
                hn = (r.get("address") or {}).get("house_number", "")
                rd = (r.get("address") or {}).get("road", "")
                if in_area(lat, lng) and hn == no and street in rd:
                    return {"lat": round(lat, 6), "lng": round(lng, 6), "geoSrc": "OpenStreetMap(주소)"}
        except Temporary:
            raise
        except Exception:
            pass
    return None


# ───────── 관광공사 음식점 자동 발굴 ─────────
# 앱에 직접 넣은 맛집(광주 2025 맛집 등)은 전남 시·군마다 1~2곳뿐이라, 코스에 식당을 붙이기 어려웠다.
# 관광공사에 등록된 음식점을 시·군마다 몇십 곳씩 받아 맛집 목록에 더한다. (일주일에 한 번만 새로 받음)
FOOD_FILE = u.ROOT / "scripts" / "food_discovered.json"
FOOD_REFRESH_DAYS = 7
FOOD_AREAS = [("38", 14, 30), ("5", 5, 15)]   # (지역코드, 최대 페이지, 시·군·구마다 최대 몇 곳) · 전남 38 / 광주 5
FOOD_MIN_MODIFIED = "2021"                    # 너무 오래 안 고친 자료(폐업 가능성)는 뺀다
FOOD_DETAIL_MAX = 80                          # 한 번 실행에 영업시간 상세는 80곳까지 (호출 한도 보호)
CAT3 = {"A05020100": "한식", "A05020200": "양식", "A05020300": "일식", "A05020400": "중식",
        "A05020700": "이색음식", "A05020800": "", "A05020900": "카페"}
food_details = 0


def discover_food(existing: list) -> list:
    saved = u.read_json(FOOD_FILE, {})
    fresh = saved.get("fetched") and (
        datetime.strptime(u.TODAY_ISO, "%Y-%m-%d") - datetime.strptime(saved["fetched"], "%Y-%m-%d")
    ).days < FOOD_REFRESH_DAYS
    items = saved.get("items") or []
    if not fresh and u.DATA_KEY:
        got, per = [], {}
        try:
            for area, pages, cap in FOOD_AREAS:
                for page in range(1, pages + 1):
                    rows = tour_get("areaBasedList2", areaCode=area, contentTypeId="39",
                                    numOfRows="100", pageNo=str(page), arrange="Q")
                    for it in rows:
                        addr = (it.get("addr1") or "").strip()
                        title = u.clean_api_text(it.get("title") or "").strip()
                        lat, lng = u.to_float(it.get("mapy")), u.to_float(it.get("mapx"))
                        dist = u.district_from_addr(addr)
                        cat = CAT3.get(it.get("cat3") or "", "")
                        if not (title and u.in_region(addr) and in_area(lat, lng) and dist):
                            continue
                        if cat == "카페" or re.search(r"카페|커피|베이커리|제과|빵", title):
                            continue                      # 카페·빵집은 따로 다룸
                        if str(it.get("modifiedtime") or "9999") < FOOD_MIN_MODIFIED:
                            continue
                        if per.get(dist, 0) >= cap or any(g["tourId"] == str(it.get("contentid")) for g in got):
                            continue
                        per[dist] = per.get(dist, 0) + 1
                        got.append({
                            "id": f"tour-food-{it.get('contentid')}", "tourId": str(it.get("contentid") or ""),
                            "name": title, "menu": cat, "district": dist, "address": addr,
                            "lat": round(lat, 6), "lng": round(lng, 6), "geoSrc": "한국관광공사",
                            "phone": (it.get("tel") or "").strip(),
                            "badge": "관광공사 등록 음식점", "source": "한국관광공사 TourAPI",
                            "note": "", "verified": u.TODAY_ISO, "origin": "auto",
                        })
                    if len(rows) < 100:
                        break
            if got:
                items = got
                u.write_json(FOOD_FILE, {"fetched": u.TODAY_ISO, "items": items})
                print(f"  관광공사 음식점 {len(items)}곳 새로 받음 · 시군구 {len(per)}곳", flush=True)
        except Exception as e:
            print(f"  ⚠ 음식점 발굴 실패 → 지난번 목록 사용: {u.scrub(e)}", flush=True)
    # 앱에 직접 넣은 맛집과 겹치면 직접 넣은 쪽만 남김
    have = {(u.norm_title(x.get("name")), x.get("district")) for x in existing}
    return [x for x in items if (u.norm_title(x["name"]), x["district"]) not in have]


# ───────── 한 곳 조사 ─────────
def lookup(x: dict, kind: str) -> dict:
    """찾은 정보 묶음(캐시에 저장되는 값)을 돌려준다. 못 찾으면 {'miss': 날짜}."""
    global food_details
    found = {}
    it = None
    if kind == "food" and x.get("tourId") and x.get("lat"):
        # 관광공사에서 발굴한 음식점: 위치는 이미 있으니 영업시간 상세만 아이디로 바로 묻는다
        if food_details >= FOOD_DETAIL_MAX:
            raise Temporary("상세 상한")
        food_details += 1
        try:
            d = tour_food_detail(x["tourId"])
        except RuntimeError:
            raise
        except Exception as e:
            raise Temporary(str(e))
        clean = u.clean_api_text
        if clean(d.get("opentimefood")):
            found.update(hours=clean(d.get("opentimefood")), hoursSrc="한국관광공사")
        if clean(d.get("restdatefood")):
            found["closed"] = clean(d.get("restdatefood"))
        if clean(d.get("firstmenu")):
            found["menu"] = clean(d.get("firstmenu"))
        found["checked"] = u.TODAY_ISO
        return found if len(found) > 1 else {"miss": u.TODAY_ISO}
    if u.DATA_KEY:
        try:
            it = tour_find(x, kind)
        except RuntimeError:
            raise                                       # 호출 상한
        except Exception as e:
            raise Temporary(str(e))
    if it:
        lat, lng = u.to_float(it.get("mapy")), u.to_float(it.get("mapx"))
        if in_area(lat, lng):
            found.update(lat=round(lat, 6), lng=round(lng, 6), geoSrc="한국관광공사")
        if it.get("tel"):
            found["phone"] = it["tel"].strip()
        if it.get("firstimage"):
            found["image"] = it["firstimage"].replace("http://", "https://")
            found["imageCredit"] = "한국관광공사"
        found["tourId"] = str(it.get("contentid") or "")
        if kind == "food" and str(it.get("contenttypeid")) == "39" and found["tourId"]:
            try:
                d = tour_food_detail(found["tourId"])
            except RuntimeError:
                raise
            except Exception:
                d = {}
            clean = u.clean_api_text
            if clean(d.get("opentimefood")):
                found["hours"] = clean(d.get("opentimefood"))
                found["hoursSrc"] = "한국관광공사"
            if clean(d.get("restdatefood")):
                found["closed"] = clean(d.get("restdatefood"))
            if clean(d.get("firstmenu")):
                found["menu"] = clean(d.get("firstmenu"))
            if clean(d.get("infocenterfood")) and "phone" not in found:
                found["phone"] = clean(d.get("infocenterfood"))
    if "lat" not in found:
        hit = osm_find(x)
        if hit:
            found.update(hit)
    if not found:
        return {"miss": u.TODAY_ISO}
    found["checked"] = u.TODAY_ISO
    return found


def needs_lookup(x: dict, kind: str) -> bool:
    if not x.get("lat"):
        return True
    if kind == "place" and not x.get("image"):
        return True            # 사진 없는 장소 → 관광공사(공공누리) 사진으로 채움
    return kind == "food" and not x.get("hours")


def apply(x: dict, found: dict) -> dict:
    """빈칸만 채운다. 직접 입력한 값은 그대로."""
    y = dict(x)
    if found.get("menu") and y.get("menu") in CAT3.values():
        y["menu"] = ""                      # '한식' 같은 분류 대신 실제 대표메뉴로
    for k in ("lat", "lng", "phone", "hours", "closed", "menu"):
        if found.get(k) and not y.get(k):
            y[k] = found[k]
            if k == "lat":
                y["geoSrc"] = found.get("geoSrc", "")
            if k == "hours":
                y["hoursSrc"] = found.get("hoursSrc", "")
    if found.get("image") and not y.get("image"):
        y["image"], y["imageCredit"] = found["image"], found.get("imageCredit", "")
    if found.get("tourId") and not y.get("tourId"):
        y["tourId"] = found["tourId"]
    return y


def main():
    data = u.embedded_data()
    if not data:
        print("  ⚠ 내장 자료가 비어 있어 이번엔 장소 파일을 덮어쓰지 않아요", flush=True)
        return
    data["restaurants"] = list(data.get("restaurants") or []) + discover_food(data.get("restaurants") or [])
    cache = u.read_json(CACHE_FILE, {})
    stats = {}
    stopped = False
    img_retries = 0
    deadline = time.time() + RUN_BUDGET
    looked = 0

    for list_name, kind in LISTS.items():
        rows = data.get(list_name) or []
        out, filled_geo, filled_hours = [], 0, 0
        for x in rows:
            key = item_key(list_name, x)
            hit = cache.get(key)
            stale_miss = hit and hit.get("miss") and (
                datetime.strptime(u.TODAY_ISO, "%Y-%m-%d") - datetime.strptime(hit["miss"], "%Y-%m-%d")
            ).days >= RETRY_MISS_DAYS
            if not stopped and time.time() > deadline:
                stopped = True
                print("  시간 예산 도달 → 지금까지 찾은 것만 저장하고, 나머지는 다음 실행 때", flush=True)
            retry_img = (hit and not hit.get("miss") and kind == "place" and not x.get("image")
                         and not hit.get("image") and hit.get("imgTriedV") != IMG_TRY_V
                         and img_retries < IMG_RETRY_MAX)
            if retry_img and not stopped:
                img_retries += 1
                try:
                    it = tour_find(x, kind) if u.DATA_KEY else None     # 좌표는 이미 있으니 관광공사 사진만
                    if it and it.get("firstimage"):
                        hit = {**hit, "image": it["firstimage"].replace("http://", "https://"),
                               "imageCredit": "한국관광공사", "tourId": str(it.get("contentid") or "")}
                    hit = {**hit, "imgTried": u.TODAY_ISO, "imgTriedV": IMG_TRY_V}
                    cache[key] = hit
                except RuntimeError:
                    stopped = True                      # 호출 상한
                except Exception:
                    pass                                # 일시적 오류 → 다음 실행 때
            elif needs_lookup(x, kind) and (hit is None or stale_miss) and not stopped:
                looked += 1
                if looked % 25 == 0:
                    print(f"  장소 정보 찾는 중 {looked}곳 (관광공사 {tour_calls}회 · 지도 {osm_calls}회)", flush=True)
                try:
                    hit = lookup(x, kind)
                    cache[key] = hit
                except RuntimeError:
                    stopped = True                      # 호출 상한 → 나머지는 다음 실행 때
                except Temporary:
                    pass                                # 다음 실행 때 다시
            y = apply(x, hit or {}) if hit and not hit.get("miss") else dict(x)
            filled_geo += bool(y.get("lat") and not x.get("lat"))
            filled_hours += bool(y.get("hours") and not x.get("hours"))
            out.append(y)
        u.write_json(u.DATA_DIR / f"{list_name}.json", out)
        stats[list_name] = {
            "total": len(out),
            "with_coords": sum(1 for y in out if y.get("lat")),
            "coords_filled": filled_geo,
            "hours_filled": filled_hours,
        }

    u.write_json(CACHE_FILE, cache)

    meta = u.read_json(u.OUT_META, {})
    meta["place_fill"] = {
        "checked_at": u.NOW.isoformat(timespec="seconds"),
        "tourapi_calls": tour_calls,
        "osm_lookups": osm_calls,
        "unfinished": stopped,
        "lists": stats,
    }
    u.write_json(u.OUT_META, meta)
    print(json.dumps(meta["place_fill"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
