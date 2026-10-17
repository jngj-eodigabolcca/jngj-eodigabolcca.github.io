#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
반려견 동반 카페 공공데이터 수집기 (GitHub Actions에서 하루 두 번, 장소 보강 전에 실행)

출처: 한국관광공사 「반려동물 동반여행 서비스」(TourAPI · KorPetTourService2)
  · 광주·전남 주소의 음식점(관광 타입 39) 가운데 카페·찻집·디저트만 고른다.
  · 동반 조건(동반 구분 · 가능 동물 · 필요 사항)은 detailPetTour2 로 받는다.
결과: docs/data/pet_places.json
  → enrich_places.py 가 이 목록을 '멍카페' 기본 목록으로 쓰고,
    공공데이터에 아직 없는 기존 목록은 '운영자 확인'으로 따로 표시해 덧붙인다.

원칙
  · 실패해도 지난번 결과(pet_places.json)를 그대로 둔다. 빈 목록으로 덮어쓰지 않는다.
  · 상세정보는 14일에 한 번만 다시 받는다 (하루 호출 한도 보호).

※ 이 API는 data.go.kr 에서 '한국관광공사_반려동물_동반여행_서비스' 활용신청을 따로 해야 쓸 수 있다.
   (기존 DATA_GO_KR_KEY 를 그대로 쓰며, 신청만 승인되면 된다.)
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_data as u  # 공통 함수 재사용

OUT = u.DATA_DIR / "pet_places.json"
BASE = "https://apis.data.go.kr/B551011/KorPetTourService2"
SOURCE = "한국관광공사 반려동물 동반여행 공공데이터"
DETAIL_TTL_DAYS = 14
MAX_CALLS = 250          # 한 번 실행 상한 (TourAPI 하루 한도 보호)
MAX_PAGES = 40           # 목록 100건 × 40쪽까지
RUN_BUDGET = 8 * 60      # 이 시간을 넘으면 받은 것까지만 저장

calls = 0
CAFE_RE = re.compile(r"카페|커피|cafe|coffee|찻집|티하우스|다방|디저트|베이커리|브런치", re.I)


def api(op: str, **params):
    global calls
    if calls >= MAX_CALLS:
        raise RuntimeError("이번 실행의 호출 상한 도달")
    calls += 1
    base = {"serviceKey": u.DATA_KEY, "MobileOS": "ETC", "MobileApp": "gwangju-eodi", "_type": "json"}
    base.update(params)
    raw = u.http_get(f"{BASE}/{op}?" + urllib.parse.urlencode(base))
    try:
        j = json.loads(raw.decode("utf-8"))
    except ValueError:
        # 활용신청 전이거나 키 문제면 XML 오류문이 온다
        msg = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace"))
        raise RuntimeError(u.scrub("응답이 JSON이 아님 · " + re.sub(r"\s+", " ", msg).strip()[:160]))
    resp = (j or {}).get("response") or {}
    head = resp.get("header") or {}
    if str(head.get("resultCode")) not in ("0000", "0"):
        raise RuntimeError(f"반려동물 API 오류: {head.get('resultCode')} {head.get('resultMsg')}")
    body = resp.get("body") or {}
    items = body.get("items") if isinstance(body.get("items"), dict) else {}
    items = (items or {}).get("item") or []
    return ([items] if isinstance(items, dict) else items), int(body.get("totalCount") or 0)


def is_cafe(x: dict) -> bool:
    if str(x.get("cat3") or "") == "A05020900":            # 옛 분류: 카페/전통찻집
        return True
    if str(x.get("lclsSystm2") or "").startswith("FD05"):  # 새 분류: 카페·찻집
        return True
    return bool(CAFE_RE.search(x.get("title") or ""))


def clean(v) -> str:
    return u.clean_api_text(v) if v else ""


def to_item(x: dict, d: dict, prev: dict | None) -> dict:
    addr = (x.get("addr1") or "").strip()
    pet_parts = [clean(d.get("acmpyTypeCd")), clean(d.get("acmpyPsblCpam")) and "가능: " + clean(d.get("acmpyPsblCpam")),
                 clean(d.get("acmpyNeedMtr")) and "필요: " + clean(d.get("acmpyNeedMtr"))]
    pet = " · ".join(p for p in pet_parts if p)
    allow = clean(d.get("acmpyPsblCpam"))
    item = {
        "id": "pet-tour-" + str(x.get("contentid")),
        "name": clean(x.get("title")),
        "district": u.district_from_addr(addr),
        "address": (addr + " " + (x.get("addr2") or "")).strip(),
        "phone": (x.get("tel") or "").strip(),
        "hours": (prev or {}).get("hours", ""),
        "pet": pet or "동반 조건은 매장에 확인",
        "note": clean(d.get("etcAcmpyInfo")),
        "big": False,          # '넓은 야외 공간' 표시는 공공데이터에 없어 비워 둠
        "bigDog": bool(re.search(r"대형|모든\s*견종|전\s*견종|크기\s*제한\s*없", allow)),
        "lat": u.to_float(x.get("mapy")),
        "lng": u.to_float(x.get("mapx")),
        "image": (x.get("firstimage") or "").replace("http://", "https://"),
        "imageCredit": "한국관광공사" if x.get("firstimage") else "",
        "tourId": str(x.get("contentid") or ""),
        "source": SOURCE,
        "publicListed": True,
        "verified": d.get("_at") or u.TODAY_ISO,
        "detailAt": d.get("_at") or u.TODAY_ISO,
    }
    return item


def fresh(prev: dict | None) -> bool:
    try:
        return bool(prev) and (datetime.strptime(u.TODAY_ISO, "%Y-%m-%d")
                               - datetime.strptime(prev["detailAt"], "%Y-%m-%d")).days < DETAIL_TTL_DAYS
    except Exception:
        return False


def main():
    old = u.read_json(OUT, {})
    if not u.DATA_KEY:
        print("  DATA_GO_KR_KEY 없음 → 반려동물 공공데이터 수집 건너뜀 (지난 결과 유지)", flush=True)
        return
    prev_by_id = {p.get("tourId"): p for p in (old.get("items") or [])}
    deadline = time.time() + RUN_BUDGET
    found, page, total = [], 1, None
    try:
        while page <= MAX_PAGES and time.time() < deadline:
            rows, total = api("areaBasedList2", contentTypeId="39", numOfRows="100", pageNo=str(page), arrange="A")
            if not rows:
                break
            for x in rows:
                if u.in_region(x.get("addr1") or "") and is_cafe(x):
                    found.append(x)
            if page * 100 >= (total or 0):
                break
            page += 1
    except Exception as e:
        print(f"  ⚠ 반려동물 목록 수집 실패: {u.scrub(str(e))} → 지난 결과 유지", flush=True)
        meta = u.read_json(u.OUT_META, {})
        meta["pet_places"] = {"ok": False, "message": u.scrub(str(e))[:160], "checked_at": u.NOW.isoformat(timespec="seconds"),
                              "count": len(old.get("items") or [])}
        u.write_json(u.OUT_META, meta)
        return

    items = []
    for x in found:
        cid = str(x.get("contentid") or "")
        prev = prev_by_id.get(cid)
        if fresh(prev):
            item = to_item(x, {}, prev)
            for k in ("pet", "note", "bigDog", "verified", "detailAt", "hours"):   # 지난번 상세 재사용
                item[k] = prev.get(k, item[k])
            items.append(item)
            continue
        d = {}
        if time.time() < deadline:
            try:
                rows, _ = api("detailPetTour2", contentId=cid)
                d = rows[0] if rows else {}
                d["_at"] = u.TODAY_ISO
            except RuntimeError:
                pass        # 상한·오류 → 목록 정보만
            except Exception:
                pass
        item = to_item(x, d, prev)
        if not d and prev:                     # 상세를 못 받았으면 지난번 상세 유지
            for k in ("pet", "note", "bigDog", "verified", "detailAt"):
                item[k] = prev.get(k, item[k])
        items.append(item)

    items = [i for i in items if i["name"] and i["district"]]
    items.sort(key=lambda i: (i["district"], i["name"]))
    if not items and old.get("items"):
        print("  ⚠ 이번엔 0곳이라 지난 결과를 그대로 둡니다", flush=True)
        return
    u.write_json(OUT, {"fetched": u.TODAY_ISO, "source": SOURCE, "items": items})
    meta = u.read_json(u.OUT_META, {})
    meta["pet_places"] = {"ok": True, "count": len(items), "api_calls": calls, "scanned": total,
                          "checked_at": u.NOW.isoformat(timespec="seconds")}
    u.write_json(u.OUT_META, meta)
    print(f"  반려견 동반 카페(공공데이터) {len(items)}곳 · 호출 {calls}회", flush=True)


if __name__ == "__main__":
    main()
