# 영상·콘텐츠 플랫폼 수집 정책

뉴유니버스 nu-browser-use 데이터 수집 인프라의 수집 범위와 금지선. 검증일: 2026-08-14.

## 원칙

1. **원문 비보관** — 수집 대상은 Derived Annotation(메타데이터, 집계 지표, 홍보 에셋)만. 원문 저작물(에피소드 영상, 대본 전문)은 보관하지 않는다.
2. **무인증·공개 경로만** — 브라우저가 익명 방문자로서 받는 것과 동일한 데이터만. 로그인 우회, 앱 전용 API 역설계, 서명/토큰 위조는 접근통제 회피에 해당하며 금지.
3. **연결 품질 > 수집량** — 전수보다 재현 가능한 파이프라인.
4. **배포 3단계** — C: 로컬 → NAS `X:\nu-browser-use\<name>_export`(WSL `/mnt/newunivers-sdb/nu-browser-use/`) → G드라이브 `개발팀\<NAME>`. 누락 금지.

## 수집 범위 (허용)

| 분류 | 내용 | 예시 |
|---|---|---|
| 카탈로그 메타데이터 | 제목, 장르/테마, 화수, 등급, 언어, 방영일 | Vigloo programs.json, ReelShort books.json |
| Engagement 지표 | viewCount, likeCount, bookmarkCount, read_count + 시점별 스냅샷/델타 | Vigloo `?episode=1` 페이지 스냅샷 |
| 홍보 에셋 | 포스터, 타이틀 이미지, 확장 썸네일 (CDN 무토큰 공개) | Vigloo assets 609장, ReelShort posters 602장 |
| 에피소드 스틸컷 | 플레이어가 공개 제공하는 썸네인 이미지 | Vigloo episode_thumbs 10,657장 |
| 참조 메타데이터 | DP 크레딧, 렌즈/조명/컬러 태그, 팔레트 | ShotDeck 150샷 |

## 수집 금지

- **에피소드 영상 원문** — HLS 스트림 세그먼트, 다운로드 파일 전부. 상업 콘텐츠 전체 복제는 원칙 1 위반이자 저작권 침해.
- **접근통제 우회** — 앱 전용 API 역설계, 재생 토큰 위조, 서명 체인을 보호장치 해제 목적으로 사용. (ReelShort 서명 재현은 웹이 익명 방문자에게 주는 카탈로그 읽기에 한해서만 사용한다.)
- **AI봇 명시 차단 소스 직접 스크래핑** — PromptHero, SimplyScripts, TV Tropes 등 ToS 리스크 명시 소스.

## 영상 데이터가 필요할 때의 합법 경로

1. **플랫폼 제휴/라이선스 계약** — 사업 개발 담당자 경유. 엔지니어링 경유 아님.
2. **공개 라이선스 코퍼스** — Wan2.2(Apache-2.0), LTX-Video(OpenRAIL-M), VideoFeedback(Apache-2.0), MultiCamVideo/ReCamMaster(Apache-2.0) 등 검증 완료 목록은 프로젝트 메모리 참조.
3. **자체 제작** — 생성 → NU Signal(시청자 행동) 수집 폐쇄루프.

## 소스별 검증 상태 (2026-08-14)

| 소스 | 상태 | 접근 방식 |
|---|---|---|
| ShotDeck | ✅ 수집 완료 | CDP + 페이지 내 fetch() (로그인 세션) |
| Vigloo | ✅ 수집 완료 | 순수 HTTP: sitemap → `__NEXT_DATA__` / 썸네인 API |
| ReelShort | ✅ 수집 완료 | 순수 HTTP: 웹 클라이언트 서명 재현 (카탈로그 한정) |
| DramaBox | ⬜ 미탐색 | 진입점 확인 필요 (sitemap 404) |
| netshort | ⬜ 진입점 404 | 재탐색 필요 |
| dramawave.tech | ❌ 사망 | DNS 실패 |
| MicroDrama Radar | ❌ 차단 | Vercel 429 |

## 스냅샷·증분 운영

- 모든 콜렉터는 재실행 시 캐시 스킵으로 증분 수집.
- Engagement 스냅샷(`vigloo_snapshot.py`)은 실행일 기준 `snapshots/YYYY-MM-DD.json`으로 저장, 이전 스냅샷 대비 델타 자동 계산.
