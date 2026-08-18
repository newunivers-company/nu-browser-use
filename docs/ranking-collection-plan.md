# 숏드라마 랭킹 수집 계획 (Ranking Intelligence)

작성: 2026-08-14. 입력: 외부 조사 문서(18개 소스 제안) + 전 소스 HTTP 실증 검증(본문 그대로 신뢰하지 않음).

## 핵심 설계 판단 (조사 문서 동의 부분)

1. **rank_type 분리 필수** — "#1"의 의미가 소스마다 다름 (광고투입 vs 조회수 vs 평점 vs 플랫폼 내부)
2. **스냅샷 저장 → velocity 계산** — 순위 자체보다 주간 변화(82→37→11→3)가 흥행 선행 신호
3. **교차 랭킹 신호** — Paid UA 상위 + organic 하위 = 광고 의존 가설 등
4. 조사 문서가 제안한 `RankingObservation` 스키마는 그대로 채택 (rank_type/scope/period/raw_metric 포함)

## 실증 검증 결과 (2026-08-14 curl/robots 확인)

| 소스 | HTTP | 구조 | robots/비고 | 판정 |
|---|---|---|---|---|
| verticaldrama.tv | 200 | SSR HTML — phone-card(№순위·플랫폼·장르·화수·조회수) | `Allow: /` + AI 크롤러 명시 환영, sitemap/feed 제공 | **P0 즉시** |
| vigloo.com/ko | 200 | `__NEXT_DATA__`에 `rankBundle[10]` (인기 탭, viewCount 포함) | 기존 파이프라인 동일 | **P0 즉시** |
| reelshort.com | 200 | 서명 API 레일(read_count) 이미 확보 | 기존 `reelshort_collect.py` | **P0 즉시** |
| duanju007.com | 200 | SSR + `<table>` 주간 두이훙/훙궈 | 중국어, 표 파싱 | P0 |
| shortdramacast.com | 200 | SSR 91KB | 구조 확인 필요 | P1 |
| insightrackr blog | 200 | 월간 Top15 블로그(5.3MB 인덱스) | 느린 주기, 파싱 용이 | P1 |
| socialpeta.com | 200 | 주간 랭킹 = 블로그 아티클 | 에디토리얼 요약본만 공개(상세는 SaaS) | P1 |
| dataeye.com | 200 | 제품 페이지 | 랭킹 실데이터는 로그인 SaaS | P1(공개분만) |
| shortdramarank.com | 200 | 3.4KB 셸 = JS 렌더 | 브라우저 필요 | P1 |
| xinwanr.com | 200 | 미디어 사이트 | 분석 위주 | P2 |
| netshort /hotseries | 200 | 74KB | `__NEXT_DATA__` 확인 후 결정 | P1 |
| dramabox.com/ko | 200 | 116KB | `__NEXT_DATA__` 확인 후 결정 | P1 |
| microdramaradar | **429** | 평문 curl 차단 | 메모리 확인된 Vercel 벽 — CDP로만 가능 | P1(브라우저) |
| sensor tower | — | 보고서 공개분 | 유료 본체, 블로그 PDF만 | P2(공개분) |
| 短剧工程/Starlight | 200 | — | 교차검증 보조 | P2 |

**조사 문서 대비 정정:** MicroDrama Radar는 "P0" 제안이지만 429 벽 때문에 자동화 최악(메모리와 동일 결론). 반대로 verticaldrama.tv는 robots가 AI 크롤러까지 환영하는 최우호 소스.

## 수집 아키텍처

기존 인프라 재사용 — 새 스키마/플랫폼 불필요:

```
ranking_export/
  snapshots/YYYY-MM-DD/
    verticaldrama_global.json    # CROSS_PLATFORM 주간
    vigloo_trending_<locale>.json# PLATFORM_INTERNAL 일간
    reelshort_rails.json         # PLATFORM_INTERNAL 일일(read_count)
    duanju007_weekly.json        # VIEW_COUNT 주간(중국)
    ...
  observations.jsonl             # RankingObservation 통합 append (스키마 아래)
```

`vigloo_snapshot.py` 패턴 그대로: 날짜별 스냅샷 + 직전 대비 delta 리포트.

### RankingObservation 레코드 (조사 문서 스키마 압축 채택)

```json
{
  "source": "verticaldrama.tv", "rank_type": "CROSS_PLATFORM",
  "entity_type": "work", "entity_id": "<slug-or-title>",
  "scope": {"type": "global"}, "period": {"type": "weekly"},
  "rank": 1, "previous_rank": null, "views": 1500000, "rating": null,
  "platform": "FlareFlow", "genres": ["Revenge Thriller"], "episodes": 50,
  "source_url": "https://...", "observed_at": "2026-08-14T...Z"
}
```

## 순서 (1주차 실행 계획)

1. **D+0: verticaldrama collector** — SSR 파싱(phone-card + table), sitemap으로 하위 페이지(플랫폼별) 발견. 주 1회
2. **D+0: vigloo ranking** — 기존 snapshot에 rankBundle 10개 추가. 일 1회
3. **D+1: reelshort rails** — 기존 collector에 shelf/rail 순서 저장 추가. 일 1회
4. **D+2: duanju007** — 주간 표 파싱. 주 1회
5. **D+3: netshort/dramabox `__NEXT_DATA__` 확인 → 가능하면 일 1회 ordinal**
6. **D+4: shortdramacast/insightrackr/socialpeta 공개분** — 주/월간
7. **크론**: 일일(vigloo/reelshort/netshort/dramabox) + 주간(verticaldrama/duanju007/socialpeta) — 기존 Bluesky/Mastodon 3h 크론 패턴 확장

## 파생 분석 (데이터 쌓이는 대로)

- rank_velocity / new_entry / top10_days — 스냅샷 2주치부터
- Trope Rank — verticaldrama+duanju007 장르 태그 집계
- NU Rank 통합 점수 — 소스 4개 이상 확보 후 가중치 실험(조사 문서의 20/20/20/15/10/10/5 출발점)
- Cross-signal — Paid UA(광고) vs Organic(플랫폼 내부) 괴리 탐지

## 명시적 비수집

- Sensor Tower/DataEye/SocialPeta **SaaS 본체** (유료/로그인) — 공개 블로그 아티클만
- MicroDrama Radar 429 우회 시도 금지 — CDP 브라우저 세션으로 정상 방문만
