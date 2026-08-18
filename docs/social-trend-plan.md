# 소셜미디어 트렌드 수집 작업 계획

작성: 2026-08-19. 현재 상태 기준.

## 현재 운영 (daily 사이클 4소스, 5일치 시계열)

| 소스 | 방식 | 상태 |
|---|---|---|
| Bluesky | getTrendingTopics (무인증 unspecced) | 🔄 일간 |
| Mastodon | 인스턴스별 trends tags/links | 🔄 일간 |
| Lemmy | 커뮤니티 관찰 | 🔄 일간 |
| Telegram | 채널 카드 | 🔄 일간 |

**쌓인 데이터**: 39스냅샷 / 2,510행. 재등장 토픽 81개 — 5일 지속 "AI data centers 반발", "Twitch AI 학습 논란" 등이 이미 보임.

## 단계 계획

### Phase 1 — 지금 데이터로 되는 것 (키 불필요, 오늘)
1. **trend_velocity.py**: 스냅샷 시계열 → 소멸/지속/부상/급등 분류. 당일-온리 API라 스냅샷 자체가 자산인데 분석기가 없음. recurring 81개는 이미 계산 가능.
2. **숏드 도메인 필터**: 전체 트렌드에서 AI영상/웹소설/드라마 관련 토픽만 분리 (키워드 사전 기반). 일반 뉴스 트렌드가 90%라 도메인 신호 대비가 낮음.

### Phase 2 — 소스 확장 (무키)
3. **Tumblr**: explore/trending HTML에 태그 10개 임베드 확인(무인증). API는 키 필요하지만 HTML 파싱으로 daily 추가 가능.
4. **Pinterest trends 웹**: Trial 앱 등록 검토 (검증 결과: Trial로 v5 trends 접근 가능, 당일-온리).

### Phase 3 — 키 발급 필요 (사용자 액션)
5. **Kakao Daum** (REST키만, 국내 최강) — blog/cafe 검색 30k/일
6. **YouTube Data API** (API키, 10k유닛/일) — 숏드 채널 관찰
7. **NAVER API HUB** (NCP 계정) — DataLab 트렌드
8. **Reddit OAuth** (무료, 100QPM) — r/ShortDrama 등 워치리스트, 요약·폐기 원칙

### Phase 4 — 심사/계약 필요 (장기)
9. **Threads** keyword_search (App Review), **X** Post Counts (유료), **Douyin** 핫워드 (해외법인 등록)

## 우선순위 근거
Phase 1은 이미 낸 데이터에서 즉시 가치. Phase 2는 공짜 확장. Phase 3은 키 한 개당 수 분이지만 사용자 발급 필요. 도메인 특화(숏드·AI영상) 트렌드 커버리가 관건 — 일반 트렌드는 경쟁사도 다 보고 있음.

## 사용자 추가 소스 실측 (2026-08-19)

| 소스 | 실측 결과 | 편입 |
|---|---|---|
| **Google Trends** | `trends.google.com/trending/rss` 무키 RSS, 지역별(US/KR/JP) 10개씩 + traffic 근사치 | ✅ **즉시 편입** — `gtrends_collect.py` daily |
| **Instagram** | 태그 페이지가 로그인 벽 (og 메타 없음, 세션 필요) | ❌ 무인증 불가 — API 해시태그 30개/7일 제한 존속. 계정 준비 시 CDP 세션 경로 검토 |
| **TikTok** | Creative Center가 JS 셸 (서버사이드 해시태그 0개) | ❌ 무인증 불가 — 브라우저 세션(기존 CDP 패턴) 또는 계약 필요 |
