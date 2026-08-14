# 홍보채널 인텔리전스 수집 계획 (Promotion Intelligence)

작성: 2026-08-14. 입력: 외부 조사 문서(플랫폼·제작사 홍보채널 Watch List, 60여 URL).
조사 문서 본문은 그대로 신뢰하지 않고 전 URL HTTP + robots.txt 실증 후 작성.

`ranking-collection-plan.md`의 자매 문서. 저쪽이 "무엇이 몇 위인가"라면
이쪽은 "누가 무엇에 투자하기 시작했는가"를 담당한다.

## 핵심 판단

조사 문서의 우선순위는 **사업적 가치** 축만 있고 **수집 가능성** 축이 없다.
두 축을 교차시키면 제안된 P0 25개 중 20개(80%)가 수집 불가 또는 정책 위반이다.

따라서 레지스트리에 `access_tier`를 도입한다:

| Tier | 정의 | 채널 수 | 처리 |
|---|---|---|---|
| **T0** | 무인증 HTTP + robots 클린 + 구조화 데이터 | 20 | 수집 |
| **T1** | 도달은 되나 JS 렌더/sitemap 없음 | 13 | 브라우저 하네스로 5개 수집 중 |
| **T2** | authwall 또는 robots 금지 | 38 | **레지스트리 등록만, 요청 안 함** |

T2를 삭제하지 않고 남기는 게 설계 요점이다. **존재는 알되 긁지 않는다** —
채널 소유관계는 기록되고, 나중에 정책이나 제휴 상황이 바뀌면 그대로 활성화된다.
`collect: false`는 문서상 약속이 아니라 3중으로 강제된다: pydantic 불변식(로드 시 거부),
`promo_registry_verify.py`(요청 자체를 안 함), `BrowserProfile.prohibited_domains`
(브라우저가 내비게이션 거부).

## 실증 검증 결과 (2026-08-14)

### 조사 문서 사실관계 정정 5건

| 문서 기재 | 실측 | 조치 |
|---|---|---|
| `shortmax.app` | **`www.shorttv.live`로 리다이렉트** (앱 판매자 SHORTTV LIMITED로 확인) | 리브랜딩. URL 교체 |
| `www.megamatrix.io/home` | **404**. 루트는 200 | URL 교체 |
| `www.gammatime.ai/` | **`gammatime.live`로 리다이렉트** | URL 교체 |
| `facebook.com/reelshortapp` | **HTTP 400** | official_status 강등 |
| `medium.com/spoontech` | **403**(봇 차단). `/feed`는 200 | 아래 "판단 보류" 참조 |

`collection-policy.md`의 "dramawave.tech ❌ DNS 실패"도 정정 대상 — 현재 200이다.
다만 2.8KB 셸이라 알맹이는 없다.

### 차단 실측 근거

| 채널군 | 수 | 근거 |
|---|---|---|
| LinkedIn | 17 | robots 첫 줄 *"use of robots or other automated means ... is strictly prohibited"* + authwall(`Join now`×5, `Sign in`×7) |
| TikTok | 4 | robots가 `anthropic-ai`/`ClaudeBot`/`Claude-User`/`GPTBot` 등 12개 AI 크롤러를 한 그룹에 묶어 `Disallow: /`. 별도로 프로필 URL은 1.4KB 로그인 셸 |
| Instagram | 3 | `loginPage` 셸 |
| Facebook | 1 | robots *"Collection of data ... is prohibited"* + 400 |
| Linktree | 5 | robots `User-agent: *` → **`Disallow: /`** (지정 UA에만 예외) |
| col.com | 1 | Alibaba WAF(Tengine, `acw_tc` 챌린지 쿠키)가 모든 요청 형태에 405 |

### Telegram — 조사 문서 주장 반증

`t.me/s/netshort_official` 공개 프리뷰가 **비활성**이다. 메시지 블록 0개
(대조군 `t.me/s/durov` 206개). 조사 문서가 말한 "신작·독점 콘텐츠·이벤트 수집"은
채널 가입(=인증) 없이 불가능하고, 가입은 수집정책 원칙 2 위반.

건지는 것은 랜딩 카드의 **구독자 수**(2026-08-14 기준 423,587). 42만 구독자의
주간 델타는 NetShort 마케팅 투입 강도의 깨끗한 프록시라 버릴 신호는 아니다.

### 판단 보류 2건 (`decision_pending: true`)

`medium.com/spoontech/feed`, `dramashorts.io` — 둘 다 robots의 `User-agent: *`
그룹은 해당 경로를 **허용**하는데, 별도 그룹에서 ClaudeBot/GPTBot/CCBot 등을
`Disallow: /`로 묶는다. 즉 **일반 크롤러에겐 열려 있고 AI 크롤러에게만 닫혀 있다.**

기술적 장벽이 아니라 순수 정책 질문이라 자동 판정하지 않고 사람 판단까지 `collect: false`로
잠갔다. dramashorts.io는 Next.js + sitemap 200(114KB)으로 **남은 타깃 중 구조가 가장 좋아서**
아까운 케이스다. 거절 시 대체: 앱스토어 레코드(itunes:6478970137).

## 차단된 신호 → 합법 대체 경로

크리에이티브 원본만 잃는다. 전략 신호는 대부분 다른 데서 나온다.

| 원하던 신호 | 차단 채널 | 대체 |
|---|---|---|
| 신작 push 강도 | IG/TikTok 게시량 | **앱 버전 릴리즈 케이던스 + 앱 설명 델타 + rating_count 델타** + 기존 카탈로그 콜렉터 + Telegram 구독자 델타 |
| Corporate 전략·파트너십 | LinkedIn 피드 | 기업 사이트 본문 해시 델타 + 기존 `gdelt_collect.py` |
| 조직 투자 방향 | LinkedIn Jobs | 자사 careers + ATS API |
| UA/Affiliate 전략 | LinkedIn/TikTok | **`a.flextv.cc`** 공개 제휴 프로그램 — 경쟁사 UA 경제구조의 1차 진술 |
| 제작사↔플랫폼 관계 | LinkedIn | `amopictures.com/vertical` 자사 납품 크레딧 |
| Creative/Hook 패턴 | TikTok/IG | YouTube Data API v3(공식, 키 필요) — **결정 대기**. 또는 플랫폼 자체 트레일러(기존 파이프라인) |

## 인과 체인 정정

조사 문서의 체인은 관측 불가능한 첫 노드에 의존한다:

```
TikTok 게시량↑ → Paid Creative↑ → Rank↑        ← 앞 두 노드가 T2
```

T0만으로 전 노드 실측 가능한 체인:

```
앱 버전 릴리즈 + 앱 설명 변경         (appstore_watch, 일간)
   ↓
rating_count 급증 (설치량 프록시)      (appstore_watch, 일간)
   ↓
플랫폼 카탈로그 신작 투입              (reelshort/dramabox/vigloo collect, 기존)
   ↓
플랫폼 rail 상위 노출                  (rail_observations.jsonl, 기존)
   ↓
verticaldrama 크로스플랫폼 랭킹 상승   (기존)
```

Paid UA 노드만 결측이고, SocialPeta 공개 아티클(P1, ranking 계획에 이미 있음)로
부분 보간한다.

## 스키마

조사 문서의 `Company / Brand / PromotionChannel / People` 계층 채택. 4가지 수정:

**(a) `official_status`에 증거 필드 필수화.** 조사 문서가 "추정 SNS URL 제외"를
판단한 건 옳았는데 그 근거가 스키마에 없으면 재현이 안 된다.
`official_evidence: appstore_description | appstore_seller_url | site_footer | redirect_target | press_release`

**(b) `collection_method` → `access_tier` / `robots_verdict` / `decision_pending`.**

**(c) `PromotionPost`는 대부분 null이 된다.** views/likes/comments/shares는 전부
T2에서만 나오는 값이다. 실제로 채워지는 관측 레코드는 이 둘 —
`RankingObservation` 형식 재사용:

```
AppObservation        : brand, company, scope{storefront,country}, version,
                        version_bumped, rating, rating_count, rating_count_delta,
                        description_changed, seller, observed_at
ChannelSizeObservation: channel_url, metric(subscribers|followers), value, observed_at
```

**(d) 조인 키.** 플랫폼별 `book_id`는 서로 안 맞는다. `normalize(title)+language`
fuzzy join 레이어 별도 필요 — D+7 과제.

## 구현 현황

```
scripts/site_collectors/registry/promotion_channels.yaml   ✅ 회사 17 / 브랜드 14 / 채널 71
scripts/site_collectors/registry/models.py                 ✅ pydantic 검증 + 정책 불변식
scripts/site_collectors/promo_registry_verify.py           ✅ 주1회
scripts/site_collectors/appstore_watch.py                  ✅ 일1회
scripts/site_collectors/promo_browser_collect.py           ✅ T1 브라우저 하네스, 주1회
tests/ci/test_promotion_registry.py                        ✅ 18 tests
scripts/site_collectors/corpsite_watch.py                  ⬜ D+2
scripts/site_collectors/feed_watch.py                      ⬜ D+1
scripts/site_collectors/telegram_card.py                   ⬜ D+1
scripts/site_collectors/careers_watch.py                   ⬜ D+3
```

### 프로젝트 자산 재사용 (고도화 2026-08-14)

초기 구현은 이 저장소가 이미 가진 것을 쓰지 않고 다시 만들었다. 세 군데를 정정한다.

**(1) `scripts/data_source_catalog.py` 패턴 채택 → `registry/models.py`**

기존에 `tests/data_sources.yaml`용 pydantic 검증 계층이 이미 있었고 `DataSourceAccess`
enum은 사실상 같은 개념이었다. 같은 스타일(StrEnum + `ConfigDict(extra='forbid')` +
`model_validator(mode='after')`)로 레지스트리 검증기를 만들었다.

이게 중요한 이유: 이전까지 수집 정책은 **YAML 주석 안의 산문**이었다. `access_tier` 오타나
`collect: false` 누락이 조용히 통과하면 authwall 채널을 긁게 된다. 이제 정책 문장이
불변식이 된다 —

- T2 ⟹ `collect: false`
- `decision_pending` ⟹ `collect: false`
- `robots_verdict: disallow` ⟹ `collect: false`
- `official_status: verified` ⟹ `official_evidence` 필수
- brand/company 참조 무결성, URL 유일성

도입 즉시 실제 오류 4건을 잡았다(TikTok 행의 `named_block`이 실제 판정값 `named_ai_block`과
불일치 — 손으로 쓴 값이라 아무도 못 봄).

**(2) `BrowserProfile.prohibited_domains` → 정책의 기계적 강제**

`PromotionRegistry.prohibited_domains()`가 T2 호스트 집합을 BrowserProfile 패턴으로
내보낸다. SecurityWatchdog가 이걸 강제하므로 **차단 채널은 "요청하지 않는" 게 아니라
"도달 불가"가 된다.** 페이지가 LinkedIn을 링크하든 리다이렉트가 걸리든 브라우저가 거부한다.
정책이 스크립트가 지키는 약속에서 브라우저의 속성으로 바뀐다.

`tests/ci/test_promotion_registry.py`가 이걸 검증한다 — T2 채널 38개 전 URL이 실제로
거부되는지, 서브도메인(`kr.linkedin.com`)까지 막히는지, 그러면서 T1 대상은 여전히
도달 가능한지.

**(3) 표준 라이브러리 `RobotFileParser`로 교체**

손으로 짠 RFC 9309 파서를 실제 robots.txt 6종(tiktok/medium/linkedin/linktr.ee/
dramashorts/reelshort)에서 stdlib와 교차검증했고 **6건 전부 판정 일치**. `star` 판정은
검증된 stdlib로 넘기고, 직접 짠 그룹 워커는 stdlib가 못 하는 일(어떤 AI UA가 지목됐는지
열거)만 담당하도록 축소했다.

### 소스별 정찰 (promo_recon.py, 2026-08-14)

`promo_browser_collect`가 "페이지가 무엇을 그리는가"를 답한다면, `promo_recon`은
**수집 방식을 결정하는 질문** — "페이지가 데이터를 얻으려 무엇을 호출하는가" — 를 답한다.
`BrowserProfile.record_har_path`(내장 HarRecordingWatchdog)를 쓰므로 CDP Network를
손으로 붙일 필요가 없다. 페이로드는 shape만 남기고 내용은 보관하지 않는다.

세 소스를 하나씩 확인한 결과, **세 소스가 세 가지 다른 결론**으로 갈렸다.

| 소스 | HAR | 페이로드 | 판정 | 수집 |
|---|---|---|---|---|
| **GoodShort** | 73req, 카탈로그 JSON **0** | `__INITIAL_STATE__` **평문** | SSR — 브라우저 불필요 | **T1→T0**, HTTP |
| **ShortMax** | 154req, 카탈로그 JSON 0 | `__NUXT_DATA__` **암호화** | 봉투 해제 금지 | T1, 렌더 DOM |
| **FlexTV** | 277req, 카탈로그 JSON 0 | `__NUXT_DATA__` 평문 | robots 의도상 라이브러리 거부 | T1, 렌더 DOM |

**GoodShort — SPA처럼 보였지만 SSR.** `acf`는 포스터 CDN, `acfs3`는 JS 번들,
`api.xintaicz.cn/sa.gif`는 Sensors Analytics 텔레메트리였다. 카탈로그는 전부 HTML 안에
있어서 브라우저가 아예 필요 없다. **JS 셸이라고 JS 콜렉터가 필요한 건 아니라는 것**,
이게 소스별 정찰을 하는 이유다.

**ShortMax — 페이로드 자체가 암호화.** `__NUXT_DATA__`의 모든 값이 `9MePJ5iXm/LfrSEc7YAS7`
접두사를 공유하는 base64 블록, 즉 고정 IV 대칭 봉투다. JS 번들에서 키를 복구해 푸는 것은
사이트가 의도적으로 가린 데이터에 대한 접근통제 우회이고, 수집정책이 금지한다 —
ReelShort 예외는 그 사이트의 익명 카탈로그 읽기로 **명시적으로 한정**된 것이지 일반 면허가 아니다.
브라우저는 정상 동작 과정에서 이걸 복호화하므로, **렌더된 화면을 읽는다**. 방문자가 보는
것과 정확히 같고 봉투는 건드리지 않는다. 콜렉터가 무거워진 건 그 선을 지키는 비용이다.

**FlexTV — 평문인데도 브라우저로 간다.** robots.txt에 `User-agent: *` 그룹이 **아예 없고**,
Googlebot/Bingbot/Yandex/Yeti를 화이트리스트로 허용한 뒤 `python-requests`, `Scrapy`,
`wget`, `HTTrack`, `crawler4j`, `libwww-perl`을 **이름으로** 차단한다. 형식적으로 우리를
구속하는 규칙은 없고 stdlib `can_fetch('*')`도 True다. 하지만 그 열거는 어떤 접근이
환영받지 못하는지 분명히 말하고, 서버도 같은 말을 한다 — 기본 aiohttp 요청에는
`400 Too many headers received`, 브라우저에는 정상 응답. 그래서 여기서 그은 선은
**허용/금지가 아니라 라이브러리/브라우저**다. HTTP 경로가 훨씬 쉬운데도 쓰지 않는다.

### 수집 결과

| 소스 | 작품 | 확보 지표 |
|---|---|---|
| GoodShort | **461** | viewCount, likeCount, followCount, commentCount, inLibraryNum, ratings, chapterCount + 장르/트로프 461/461 |
| ShortMax | **76** | plays, likes, 화수, 카테고리, 시놉시스 + 추천 엣지 **1,125** |
| FlexTV | **115** | views, likes, 시놉시스, 커버 (**장르 없음** — 아래 참조) |

통합 `observations.jsonl` **652행**, 세 소스 모두 필수 필드 누락 0, `rank_type=VIEW_COUNT`로
기존 `RankingObservation` 스키마와 정합.

**FlexTV 장르는 수집하지 않는다.** 처음엔 "발견된 장르 페이지"를 장르로 귀속했는데
검증해보니 틀렸다 — 장르 페이지 6개가 **전부 동일한 27개 작품**을 반환한다(첫 페이지 +10,
이후 +0×5). 즉 그 앵커들은 장르 필터 결과가 아니라 공용 레일이다. 실제 장르 그리드는
`/dramas/all-dramas`와 같은 앵커 없는 JS 카드고, 재생 페이지엔 breadcrumb도 없다.
그래서 **추측 대신 필드를 비웠고**, 매 실행 말미에 미수집 사유를 출력한다.

**GoodShort의 트로프 어휘**가 특히 값지다 — Counterattack 112, CEO 109, Sweet 106,
Underdog Rise 84, Strong Female Lead 81… 461/461 커버리지에 **viewCount가 붙어 있어서**
랭킹 계획의 Trope Rank를 단순 빈도가 아니라 **조회수 가중**으로 계산할 수 있다.

**ShortMax의 추천 엣지 1,125개**는 플랫폼이 직접 저작한 유사도 그래프다. 경쟁사가 무엇을
무엇의 대체재로 보는지에 대한 1차 진술이라 그 자체로 보관 가치가 있다.

수치 정밀도는 소스의 것이다. ShortMax/FlexTV는 UI가 "447K"로 축약해 노출하므로
`parse_count`가 확장하되 `*_is_approx`로 **정밀도가 우리 것이 아님을 기록**한다.

정확도 버그 1건 정정: ShortMax 화수를 `a.episode` 앵커 수로 셌는데 목록이 24개씩
페이징돼 24로 고정 출력됐다. "N Episodes" 라벨을 읽도록 고쳐 실제값(예: 52)을 얻는다.

### `promo_browser_collect.py` — T1 하네스

계획서의 "D+4 CDP 4개"를 사이트별 스크립트 대신 **레지스트리 구동 단일 하네스**로 대체했다.
이 디렉터리의 기존 CDP 콜렉터들은 전부 생 `cdp_use.CDPClient` 위에 자기 `Runtime.evaluate`
래퍼를 다시 만드는 패턴인데, 이 저장소가 곧 `BrowserSession`이므로 그걸 쓴다.
추가로 얻는 것: 워치독 기반 내비게이션 제한, 이벤트버스 생명주기 관리, 실행별 격리 프로필
(운영자의 로그인 쿠키를 상속하지 않음 — 익명 방문자 읽기가 구조적으로 보장된다).

첫 실행 결과 5/5 성공, **HTTP로는 안 보이던 카탈로그 라우트가 드러났다**:

| 대상 | 렌더 후 | 발견된 카탈로그 라우트 |
|---|---|---|
| goodshort | 18,210자 / 136링크 | `/drama/<id>`×45, `/tag/<id>`×61 |
| shortmax (shorttv.live) | 31,488자 / 160링크 | `/drama/<id>`×67, `/episode/<id>`×67 |
| flextv | 180링크 | `/episodes/<id>`×109, `/genres/<id>`×46 |
| flareflow | 3,032자 / 1링크 | 없음 — 렌더 후에도 셸 |
| dramawave | 74자 / 3링크 | 없음 — 빈 사이트 확인 |

flextv는 aiohttp로는 `ClientResponseError`가 나는데 브라우저로는 정상 수집된다.
flareflow/dramawave가 렌더 후에도 비어 있다는 건 **부정적 결과지만 확정된 사실**이라,
더 이상 파볼 필요가 없다는 판단 근거가 된다.

경로 shape 집계(`catalog_families`)는 다음 콜렉터가 사이트별 추측 없이 페이징할 지점을
알려준다. 그리고 자사 사이트 푸터에서 **ShortMax·FlexTV의 공식 SNS 6개를 1차 출처로
확보**했다 — 조사 문서에 ShortMax 소셜은 아예 없었다. 전부 `official_evidence: site_footer`,
`verified`로 등록(단 T2라 수집은 안 함).

출력은 기존 규약 승계: `~/promo_export/` → NAS `X:\nu-browser-use\promo_export` → G드라이브.

### `promo_registry_verify.py`

레지스트리 채널의 liveness / 리다이렉트 / robots / 본문 해시를 확인하고 직전 스냅샷과 diff.
**경쟁사 리브랜딩·도메인 이전 자체가 신호**라 부가물이 아니라 P0다. 실제로 이 로직의 첫
실행이 조사 문서의 정정 3건(shortmax→shorttv.live, gammatime.ai→.live, megamatrix 404)을 잡았다.

robots 파서는 RFC 9309를 따라 연속 `User-agent` 줄을 한 그룹으로 묶고(TikTok·Medium 모두
AI 크롤러 十수개를 한 그룹에 쌓아둠), 최장일치 + Allow 우선으로 판정한다. 그리고 **두 질문을
분리**한다 — `star`(우리를 구속하는 일반 규칙)와 `ai_named_disallow`(호스트가 AI 크롤러를
지목했는가). 이걸 하나로 합치면 기술적 장벽을 과장하는 동시에 정책 질문을 숨긴다.

현재 상태: 65채널 중 ok=20, skipped_by_policy=45, 오류 0.

### `appstore_watch.py`

`itunes.apple.com/lookup` — Apple 공식·무키·문서화된 JSON. T0 중 회수율 최고.
앱 14개 × 스토어프론트 3개(us/kr/jp) = 40 레코드/일.

한 번의 호출로 달리 얻을 수 없는 4개 신호가 나온다:

1. `version` + `currentVersionReleaseDate` → 릴리즈 케이던스 = 브랜드별 엔지니어링 투자 프록시
2. `userRatingCount` → 단조 증가 카운터. **일간 델타가 설치량 프록시**이자 UA 지출과 함께 움직이는
   유일한 공개 숫자. TikTok 게시량 신호를 대체하는 자리
3. `sellerName` → 1차 법적 진술. 브랜드→회사 해소가 여기서 나온다
4. `description` → 퍼블리셔가 **자기 공식 SNS URL을 직접 적어두는 곳**. 채널 discovery가 같은 호출에서 떨어진다

스토어프론트별 분리 저장이라 시장 진출/철수가 드러난다 — 실행 결과 **My Drama와 Sereal+는
한국 스토어에 없다**(us/jp만).

부수 성과로 앱스토어 조회가 **법인 실체 12건**을 확정했고(My Drama=Holy Water Limited,
Sereal+=Col Web Pte Ltd / UniReel=COL JAPAN K.K.로 COL Group 브랜드 2개 추가 발견),
discovery가 **조사 문서에 없던 실제 카탈로그 사이트 3개**를 찾아냈다:
`my-drama.com`(1.7MB — 조사 문서는 My Drama의 SNS만 훑고 웹 자산을 통째로 놓쳤다),
`dramaboxdb.com`, `sereal.com`. 전부 검증 후 레지스트리 등록.

discovery는 URL을 자동 등록하지 않는다. 발견은 주장(claim)이지 검증된 채널이 아니므로
`discovered_channels.jsonl`로 보고만 하고 사람이 승격시킨다. 약관/개인정보 URL은
`classify()`로 걸러낸다(매 실행 26건이 실제 발견 6건을 묻어버림).

## 남은 순서

| 시점 | 작업 |
|---|---|
| D+1 | `telegram_card.py`(구독자 시계열) + `feed_watch.py`(RSS/Atom) — 둘 다 소품, 시계열은 빨리 시작할수록 이득 |
| D+2 | `corpsite_watch.py` — T0 기업/제작사 사이트 본문 해시 델타 |
| D+3 | `careers_watch.py` — ATS 슬러그 **discovery부터**(greenhouse/lever 추측은 404 확인) |
| D+4 | ~~T1 CDP 4개~~ → `promo_browser_collect.py`로 완료. 다음 단계는 발견된 카탈로그 라우트(`/drama/<id>` 등) 페이징 |
| D+5 | `a.flextv.cc` 제휴 조건 파서 (단발, UA 전략 원문) |
| D+7 | title fuzzy-join → 기존 `RankingObservation`과 결합 |

## 결정 필요 2건

1. **YouTube Data API v3** — 공식 API·ToS 준수 경로지만 키 발급이 필요해 기존 keyless 기조를 깬다.
   대상 채널 4개(ReelShort, Vigloo, My Drama, FlexTV English). 크리에이티브 신호를 얼마나 원하는지의 문제.
2. **AI 크롤러 지목 소스 2건** — 위 "판단 보류" 참조.

## 명시적 비수집

- LinkedIn 17 / TikTok 4 / Instagram 3 / Facebook 1 / Linktree 5 — 레지스트리 `access_tier: T2` 등록만
- Telegram 채널 가입 / 메시지 아카이브
- col.com WAF 우회
- 크리에이티브 영상 원본 (수집정책 원칙 1)
