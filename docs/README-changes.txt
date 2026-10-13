오늘어디로 · 2026-10-13 개선판

[올릴 파일]
- index.html, service-worker.js           : 기존 파일 교체
- embedded-data.js, creaming.webp         : 새 파일 (index.html 옆에 꼭 같이 두세요)
- data/*.json                             : 10.12 기준 자료. 서버 자료가 이보다 최신이면 올리지 마세요.
- tools/fill-coords.mjs                   : 지도 좌표 채우기 스크립트 (서버에 안 올려도 됨)

[바뀐 점]
1. 파일 크기 1MB → 약 0.55MB
   - 내장 자료를 embedded-data.js 로 분리. 컴퓨터에서 바로 열 때·인터넷 끊겼을 때만 불러옴
   - 꼭 우선해야 하는 행사 21개(pinnedIds)는 지금처럼 index.html 안에 남김
   - 크리밍 그림을 creaming.webp(4KB)로 분리
2. 자료 수정: 목요콘서트 시작일 10.08, 예스24 링크 https, meta 날짜·개수
3. "공식정보 확인" 날짜: meta와 일정별 확인일 중 더 최근 날짜를 표시
4. 지도 위치 자동검색: 지도 탭을 열 때만, 한 번에 20곳까지. Nominatim은 쓰지 않음
   행사에 좌표가 없으면 연결된 장소(venueId) 좌표를 사용
5. 화면 밖 카드는 다가올 때 그림(content-visibility) → 긴 홈 화면 스크롤이 가벼워짐
6. 출처 버튼 등 작은 버튼 키움, 아이콘 파일이 없을 때 깨진 그림 대신 크리밍 표시
7. 서비스 워커 v30: 지도 라이브러리·글꼴 미리 저장, 늦게 온 최신본도 저장

[지도 좌표 채우기]  index.html 이 있는 폴더에서
  KAKAO_REST_KEY=카카오REST키 node tools/fill-coords.mjs
  (키 없이: node tools/fill-coords.mjs --osm)
  → data/*.json 과 embedded-data.js 에 lat/lng 가 채워짐. 그다음 서버에 올리면 끝.

[추가 · 같은 날]
8. 카카오톡·문자 공유 미리보기
   - og-image.jpg(1200×630) 새 파일 + og/twitter 태그
   - 주소를 https://jngj-eodigabolcca.github.io/ 로 넣었어요. 주소가 다르면 index.html 위쪽 og:url·og:image·canonical 을 바꾸세요.
   - 카톡은 미리보기를 기억해 둬서, 예전 링크는 안 바뀔 수 있어요.
     → https://developers.kakao.com/tool/debugger/sharing 에 주소를 넣고 '캐시 초기화'
9. 다크 모드
   - 처음엔 휴대폰 설정(다크/라이트)을 따라감
   - 상단 오른쪽 🌓 버튼: 자동 → 🌙 어둡게 → ☀️ 밝게 (이 기기에 기억)
   - 지도 타일도 어둡게, 달마다 바뀌는 첫 화면 색은 은은하게 유지
