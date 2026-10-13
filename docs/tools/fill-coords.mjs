#!/usr/bin/env node
/* 오늘어디로 · 지도 좌표 한 번에 채우기
   ─────────────────────────────────────────────
   data/*.json 에서 lat/lng 가 없는 장소·행사를 찾아 좌표를 채워 넣습니다.
   이용자 휴대폰마다 지도 검색을 돌리는 대신, 여기서 한 번만 돌리면 끝.

   ▶ 준비: Node.js 18 이상
   ▶ 추천: 카카오 REST API 키 (한국 주소를 가장 정확하게 찾음, 무료)
      1) https://developers.kakao.com → 내 애플리케이션 → 앱 만들기
      2) 앱 키 > REST API 키 복사,  제품 설정 > 카카오맵 '사용 설정' 켜기
   ▶ 실행 (index.html 이 있는 폴더에서):
      KAKAO_REST_KEY=복사한키 node tools/fill-coords.mjs
      (윈도우 PowerShell:  $env:KAKAO_REST_KEY="복사한키"; node tools/fill-coords.mjs)
   ▶ 키 없이:  node tools/fill-coords.mjs --osm
      OpenStreetMap(Nominatim)으로 1초에 1곳씩 찾음. 정확도가 낮고 느려요.
   ▶ 미리보기만(파일 안 바꿈):  --dry

   찾은 좌표는 data/*.json 과 embedded-data.js(있으면)에 같이 저장됩니다.
   못 찾은 곳 목록은 tools/coords-missing.txt 에 남습니다.                    */
import fs from "node:fs";
import path from "node:path";

const ROOT = process.cwd();
const DATA = path.join(ROOT, "data");
const KEY = process.env.KAKAO_REST_KEY || "";
const USE_OSM = process.argv.includes("--osm");
const DRY = process.argv.includes("--dry");
const FILES = ["venues", "events", "restaurants", "cafes", "bakeries", "cinemas", "stays"];
const sleep = ms => new Promise(r => setTimeout(r, ms));
// 광주·전남 범위 밖이면 버림 (잘못된 핀보다 핀 없음이 낫다)
const inRegion = (lat, lng) => lat > 33.9 && lat < 35.6 && lng > 125 && lng < 127.95;
const round = n => Math.round(n * 1e7) / 1e7;

if (!KEY && !USE_OSM) {
  console.log("카카오 키가 없어요. KAKAO_REST_KEY=키 를 앞에 붙이거나, 키 없이 하려면 --osm 을 붙여 주세요.");
  process.exit(1);
}
if (!fs.existsSync(DATA)) { console.log("data 폴더가 없어요. index.html 이 있는 폴더에서 실행해 주세요."); process.exit(1) }

const fullAddr = a => String(a || "").replace(/^광주\s/, "광주광역시 ").replace(/^전남\s/, "전라남도 ").replace(/\([^)]*\)/g, "").trim();
const norm = s => String(s || "").replace(/[\s·()\[\]「」《》〈〉'"]/g, "").toLowerCase();

async function kakao(kind, query) {
  const url = `https://dapi.kakao.com/v2/local/search/${kind}.json?size=5&query=${encodeURIComponent(query)}`;
  const r = await fetch(url, { headers: { Authorization: `KakaoAK ${KEY}` } });
  if (r.status === 401 || r.status === 403) throw new Error("카카오 키가 틀렸거나 '카카오맵' 사용 설정이 꺼져 있어요.");
  if (!r.ok) return [];
  return (await r.json()).documents || [];
}
async function findKakao(x) {
  const name = x.name || x.venue || x.title || "", addr = fullAddr(x.address);
  if (addr) {                                   // 1) 주소로
    const d = (await kakao("address", addr)).find(d => inRegion(+d.y, +d.x));
    if (d) return { lat: +d.y, lng: +d.x, how: "주소" };
  }
  const core = norm(name.split(/[(·]/)[0]);     // 2) 이름 + 동네로 (이름이 맞을 때만)
  if (core.length >= 2) {
    const q = `${name.split(/[(·]/)[0].trim()} ${x.district || ""}`.trim();
    const d = (await kakao("keyword", q)).find(d => inRegion(+d.y, +d.x) && (norm(d.place_name).includes(core) || core.includes(norm(d.place_name))));
    if (d) return { lat: +d.y, lng: +d.x, how: "이름" };
  }
  return null;
}
async function findOsm(x) {
  const addr = fullAddr(x.address); if (!addr) return null;
  const r = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=3&countrycodes=kr&q=${encodeURIComponent(addr)}`,
    { headers: { "User-Agent": "oneul-eodiro-coords/1.0 (one-time batch)" } });
  await sleep(1100);                            // Nominatim 규칙: 1초에 1번
  if (!r.ok) return null;
  const d = (await r.json()).find(d => inRegion(+d.lat, +d.lon));
  return d ? { lat: +d.lat, lng: +d.lon, how: "OSM 주소" } : null;
}

const lists = {};
for (const f of FILES) {
  const p = path.join(DATA, `${f}.json`);
  if (fs.existsSync(p)) lists[f] = JSON.parse(fs.readFileSync(p, "utf8"));
}
const venueById = new Map((lists.venues || []).map(v => [v.id, v]));
let filled = 0; const missing = [];

for (const [f, list] of Object.entries(lists)) {
  const todo = list.filter(x => !(x.lat && x.lng));
  if (!todo.length) continue;
  console.log(`\n■ ${f}: 좌표 없는 곳 ${todo.length}개`);
  for (const x of todo) {
    const label = x.name || x.title;
    // 행사는 연결된 장소 좌표를 먼저 씀
    const v = x.venueId && venueById.get(x.venueId);
    let hit = v && v.lat ? { lat: v.lat, lng: v.lng, how: "연결 장소" } : null;
    if (!hit && (x.address || x.name || x.venue)) {
      try { hit = KEY ? await findKakao(x) : await findOsm(x) } catch (e) { console.log("  ✖ " + e.message); process.exit(1) }
      if (KEY) await sleep(120);
    }
    if (hit) { x.lat = round(hit.lat); x.lng = round(hit.lng); filled++; console.log(`  ✔ ${label} (${hit.how})`) }
    else { missing.push(`${f}\t${label}\t${x.address || x.venue || ""}`); console.log(`  · 못 찾음: ${label}`) }
  }
}

console.log(`\n채운 곳 ${filled}개 · 못 찾은 곳 ${missing.length}개`);
fs.writeFileSync(path.join(ROOT, "tools", "coords-missing.txt"), "자료\t이름\t주소\n" + missing.join("\n") + "\n");
if (DRY) { console.log("(--dry: 파일은 바꾸지 않았어요)"); process.exit(0) }

for (const [f, list] of Object.entries(lists)) fs.writeFileSync(path.join(DATA, `${f}.json`), JSON.stringify(list, null, 1));
// embedded-data.js (컴퓨터에서 바로 열 때 쓰는 자료)도 같이 맞춤
const emb = path.join(ROOT, "embedded-data.js");
if (fs.existsSync(emb)) {
  const src = fs.readFileSync(emb, "utf8"), i = src.indexOf("window.__DATA__=");
  const data = JSON.parse(src.slice(i + 16).replace(/;\s*$/, ""));
  for (const [f, list] of Object.entries(lists)) data[f] = list;
  fs.writeFileSync(emb, src.slice(0, i) + "window.__DATA__=" + JSON.stringify(data) + ";\n");
}
console.log("저장 완료: data/*.json" + (fs.existsSync(emb) ? ", embedded-data.js" : ""));
