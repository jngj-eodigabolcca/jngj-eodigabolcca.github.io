#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
한국문화정보원 「전국 반려동물 동반 가능 문화시설 위치 데이터」 → docs/data/pet_public.json

이 자료는 2022년 구축, 2025년 3월 일부 보완된 1회성 자료라 자동 수집하지 않는다.
새 판이 나오면 data.go.kr(15111389)에서 CSV를 받아 아래처럼 한 번만 다시 만든다.

  python scripts/build_pet_public.py  받은파일.csv

  → scripts/data/kcisa_pet_jngj.csv  (광주·전남 해당 줄만 따로 보관, 다음부터는 인자 없이 실행 가능)
  → docs/data/pet_public.json        (앱이 읽는 파일)

쓰는 분류: 카페·식당(멍카페), 박물관·미술관·문예회관·여행지(가볼 곳 동반 가능/불가 표시)
"""
from __future__ import annotations
import csv, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SLIM = ROOT / "scripts" / "data" / "kcisa_pet_jngj.csv"
OUT = ROOT / "docs" / "data" / "pet_public.json"
SIDO = ("광주광역시", "전라남도", "전남광주통합특별시")
CATS = ("카페", "식당", "박물관", "미술관", "문예회관", "여행지")
SOURCE = "한국문화정보원 반려동물 동반 가능 문화시설 공공데이터"


def clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v in ("정보없음", "해당없음", "없음", "nan", "-") else v


def num(v):
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return None


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SLIM
    rows = list(csv.DictReader(open(src, encoding="utf-8-sig")))
    rows = [r for r in rows if r.get("시도 명칭") in SIDO and r.get("카테고리3") in CATS]
    if src != SLIM:                                   # 광주·전남 해당 줄만 저장소에 보관
        SLIM.parent.mkdir(parents=True, exist_ok=True)
        with open(SLIM, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
    out = []
    for r in rows:
        district = clean(r["시군구 명칭"])
        out.append({
            "name": clean(r["시설명"]),
            "cat": r["카테고리3"],
            "district": district,
            "address": clean(r["도로명주소"]) or clean(r["지번주소"]),
            "lat": num(r["위도"]), "lng": num(r["경도"]),
            "phone": clean(r["전화번호"]),
            "hours": clean(r["운영시간"]), "closed": clean(r["휴무일"]),
            "allow": "Y" if r["반려동물 동반 가능정보"].strip().upper() == "Y" else "N",
            "size": clean(r["입장 가능 동물 크기"]),
            "limit": "" if clean(r["반려동물 제한사항"]) == "제한사항 없음" else clean(r["반려동물 제한사항"]),
            "indoor": r["장소(실내) 여부"].strip().upper() == "Y",
            "outdoor": r["장소(실외)여부"].strip().upper() == "Y",
            "fee": clean(r["애견 동반 추가 요금"]),
            "desc": clean(r["기본 정보_장소설명"]),
            "written": clean(r["최종작성일"])[:10],
        })
    out = [x for x in out if x["name"]]
    out.sort(key=lambda x: (x["cat"], x["district"], x["name"]))
    OUT.write_text(json.dumps({"source": SOURCE, "dataset": "data.go.kr 15111389", "items": out},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"광주·전남 {len(out)}곳 → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
