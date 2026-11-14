#!/usr/bin/env python3
"""
오늘, 전남광주 어디가볼까? — 🏥 약국·응급실 / 🌡 쉼터 자료 만들기

놓는 곳: 저장소의 scripts/collect_care.py  (다른 수집 스크립트들과 같은 폴더)

만드는 파일 (앱이 읽는 곳: docs/data/)
  docs/data/pharmacies.json  약국 + 요일별 영업시간   (국립중앙의료원, 하루 2번 자동 수집 권장)
  docs/data/emergency.json   응급의료기관 + 응급실 전화 (국립중앙의료원)
  docs/data/shelters.json    무더위쉼터 / 한파쉼터      (CSV를 내려받아 변환, 해마다 1번)

준비
  1) 공공데이터포털(data.go.kr)에서 아래 두 서비스 '활용신청'
     - 국립중앙의료원_전국 약국 정보 조회 서비스
     - 국립중앙의료원_전국 응급의료기관 정보 조회 서비스
  2) 발급받은 '일반 인증키(Decoding)'를 GitHub 저장소 Secrets에 DATA_GO_KR_KEY 로 저장
  3) .github/workflows/update-data.yml 의 기존 수집 단계 뒤에 한 줄 추가:
       python scripts/collect_care.py
     (env: DATA_GO_KR_KEY: ${{ secrets.DATA_GO_KR_KEY }})

쉼터 (해마다 1번, 손으로)
  scripts/shelters/ 폴더에 공공데이터포털·국민재난안전포털에서 내려받은 CSV를 넣는다.
  파일 이름에 '무더위' 또는 'heat' → 무더위쉼터, '한파' 또는 'cold' → 한파쉼터로 본다.
  열 이름(쉼터명칭·시설명 / 도로명주소·상세주소 / 위도·경도 …)은 자동으로 찾는다.

안전장치
  - 인터넷·인증키 문제로 받아오지 못하면 기존 파일을 그대로 둔다 (빈 목록으로 덮어쓰지 않음).
  - 새로 받은 개수가 기존의 절반도 안 되면 이상하다고 보고 덮어쓰지 않는다.
  - 표준 라이브러리만 쓴다 (pip 설치 필요 없음).
"""
import csv, glob, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("CARE_OUT") or os.path.join(ROOT, "docs", "data")
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")

PH_URL = "https://apis.data.go.kr/B552657/ErmctInsttInfoInqireService/getParmacyListInfoInqire"
ER_URL = "https://apis.data.go.kr/B552657/ErmctInfoInqireService/getEgytListInfoInqire"
# 통합특별시 출범 뒤 기관 자료의 시·도 이름이 바뀌었을 수 있어 셋 다 물어보고 합친다 (겹치면 하나로)
SIDO = ["광주광역시", "전라남도", "전남광주통합특별시"]
REGION_RE = re.compile(r"광주|전남|전라남도")
GU_RE = re.compile(r"(?:광주광역시|광주|전남광주통합특별시|전남광주특별시|전라남도|전남)\s*((?:동|서|남|북|광산)구|\S+?[시군])(?=\s|$)")


def log(*a):
    print("[care]", *a, file=sys.stderr)


def district(addr):
    m = GU_RE.search(addr or "")
    return m.group(1) if m else ""


def num(v):
    try:
        f = float(v)
        return round(f, 6) if f else None
    except (TypeError, ValueError):
        return None


def fetch_all(url, key, params):
    """공공데이터포털 목록 API를 끝까지 넘겨 가며 받기 (XML)."""
    out, page = [], 1
    while True:
        q = {"serviceKey": key, "pageNo": page, "numOfRows": 500, **params}
        full = url + "?" + urllib.parse.urlencode(q)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(full, timeout=30) as r:
                    body = r.read()
                break
            except Exception as e:  # 잠깐 끊기면 두 번 더
                if attempt == 2:
                    raise
                log("다시 시도", url.rsplit("/", 1)[-1], params, e)
                time.sleep(2 + attempt * 3)
        root = ET.fromstring(body)
        code = (root.findtext(".//resultCode") or "").strip()
        if code not in ("00", "0", "0000", ""):
            raise RuntimeError(f"API 오류 {code}: {root.findtext('.//resultMsg')}")
        rows = [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]
        out += rows
        total = int(root.findtext(".//totalCount") or 0)
        if not rows or len(out) >= total:
            return out
        page += 1


def hours(it):
    h = {}
    for d in range(1, 9):  # 1(월)~7(일), 8(공휴일)
        s, c = it.get(f"dutyTime{d}s", ""), it.get(f"dutyTime{d}c", "")
        if s and c:
            h[str(d)] = f"{s.zfill(4)}-{c.zfill(4)}"
    return h


def pharmacies(key):
    seen, out = set(), []
    for sido in SIDO:
        try:
            rows = fetch_all(PH_URL, key, {"Q0": sido})
        except Exception as e:
            log("약국", sido, "건너뜀:", e)
            continue
        for it in rows:
            hid = it.get("hpid") or (it.get("dutyName", "") + it.get("dutyAddr", ""))
            addr = it.get("dutyAddr", "")
            if hid in seen or not REGION_RE.search(addr):
                continue
            seen.add(hid)
            out.append({"id": hid, "name": it.get("dutyName", ""), "address": addr, "district": district(addr),
                        "tel": it.get("dutyTel1", ""), "lat": num(it.get("wgs84Lat")), "lng": num(it.get("wgs84Lon")),
                        "h": hours(it)})
    return out


def emergency(key):
    seen, out = set(), []
    for sido in SIDO:
        try:
            rows = fetch_all(ER_URL, key, {"Q0": sido})
        except Exception as e:
            log("응급실", sido, "건너뜀:", e)
            continue
        for it in rows:
            hid = it.get("hpid") or (it.get("dutyName", "") + it.get("dutyAddr", ""))
            addr = it.get("dutyAddr", "")
            if hid in seen or not REGION_RE.search(addr):
                continue
            seen.add(hid)
            out.append({"id": hid, "name": it.get("dutyName", ""), "address": addr, "district": district(addr),
                        "tel": it.get("dutyTel1", ""), "erTel": it.get("dutyTel3", ""),
                        "level": it.get("dutyEmclsName", ""), "lat": num(it.get("wgs84Lat")), "lng": num(it.get("wgs84Lon"))})
    return out


# ── 쉼터 CSV ──
COLS = {
    "name": ["쉼터명칭", "쉼터명", "시설명", "시설명칭", "RSTR_NM", "FCLTY_NM", "restarea_nm"],
    "address": ["도로명주소", "소재지도로명주소", "상세주소", "도로명전체주소", "주소", "소재지지번주소", "지번주소", "RN_DTL_ADRES", "DTL_ADRES"],
    "lat": ["위도", "LA", "LAT", "la", "lat", "Y좌표"],
    "lng": ["경도", "LO", "LOT", "lo", "lng", "lon", "X좌표"],
    "hours": ["운영시간", "평일운영시간", "WKDAY_OPER_HR"],
    "facility": ["시설구분", "시설유형", "FCLTY_TY", "FCLTY_SE_NM"],
    "capacity": ["이용가능인원", "수용인원", "USE_PSBL_NMPR"],
}


def pick(row, names):
    for n in names:
        if n in row and str(row[n]).strip():
            return str(row[n]).strip()
    return ""


def read_csv(path):
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            with open(path, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    log("읽지 못함(인코딩):", path)
    return []


def shelters():
    files = sorted(glob.glob(os.path.join(ROOT, "scripts", "shelters", "*.csv")))
    out, seen = [], set()
    for p in files:
        base = os.path.basename(p).lower()
        kind = "cold" if ("한파" in base or "cold" in base) else "heat" if ("무더위" in base or "heat" in base or "폭염" in base) else None
        if not kind:
            log("파일 이름에 무더위/한파가 없어 건너뜀:", p)
            continue
        for r in read_csv(p):
            name, addr = pick(r, COLS["name"]), pick(r, COLS["address"])
            if not name or not REGION_RE.search(addr):
                continue
            k = (kind, name, addr)
            if k in seen:
                continue
            seen.add(k)
            cap = pick(r, COLS["capacity"])
            out.append({"type": kind, "name": name, "address": addr, "district": district(addr),
                        "lat": num(pick(r, COLS["lat"])), "lng": num(pick(r, COLS["lng"])),
                        "hours": pick(r, COLS["hours"]), "facility": " · ".join(x for x in [pick(r, COLS["facility"]), f"{cap}명" if cap else ""] if x)})
    return out, bool(files)


def save(name, rows, source):
    path = os.path.join(OUT, f"{name}.json")
    old = 0
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                j = json.load(f)
            old = len(j["items"] if isinstance(j, dict) else j)
        except Exception:
            old = 0
    if not rows:
        log(f"{name}: 새 자료 0건 → 기존 파일 유지 ({old}건)")
        return
    if old and len(rows) < old * 0.5:
        log(f"{name}: 새 자료 {len(rows)}건이 기존 {old}건의 절반도 안 돼서 덮어쓰지 않음")
        return
    os.makedirs(OUT, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"updated": NOW, "source": source, "items": rows}, f, ensure_ascii=False, separators=(",", ":"))
    log(f"{name}: {len(rows)}건 저장 (기존 {old}건)")


def main():
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if key:
        save("pharmacies", pharmacies(key), "국립중앙의료원 전국 약국 정보 조회 서비스")
        save("emergency", emergency(key), "국립중앙의료원 전국 응급의료기관 정보 조회 서비스")
    else:
        log("DATA_GO_KR_KEY 가 없어 약국·응급실은 건너뜀")
    rows, had = shelters()
    if had:
        save("shelters", rows, "행정안전부·시군 무더위쉼터/한파쉼터 목록")
    else:
        log("scripts/shelters/*.csv 가 없어 쉼터는 건너뜀")


if __name__ == "__main__":
    main()
