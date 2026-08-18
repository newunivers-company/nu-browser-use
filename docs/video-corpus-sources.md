# 영상 코퍼스 수집 방법 매트릭스

검증일: 2026-08-14. 전 항목 HTTP 실증(HF API + resolve URL 확인) 완료. 상업 플랫폼(숏드라마 등) 영상은 수집 정책상 제외 — `docs/collection-policy.md` 참조.

## 즉시 수집 가능 (gated 아님, 상업 허용 라이선스)

| 코퍼스 | 라이선스 | 규모 | 용량 | 다운로드 | 비고 |
|---|---|---|---|---|---|
| **VideoFeedback** (TIGER-Lab) | Apache-2.0 | 37,662 mp4 + 5차원 품질 주석 | 8.8GB | `hexuan21/VideoFeedback-videos-mp4` + `TIGER-Lab/VideoFeedback` (HF datasets) | 수집 진행 중. 품질 평가 모델 학습용 최적 |
| **MultiCamVideo** (KlingTeam, 구 KwaiVGI) | Apache-2.0 | 13,600신×10캠 = 136k mp4 + extrinsics JSON | 333GB | `KlingTeam/MultiCamVideo-Dataset` git clone → 16파트 tar 병합 | 카메라 무브먼트 인텔리전스. NAS 직접 다운로드 |
| **CameraClone** (KlingTeam) | Apache-2.0 | 391k 영상 / 1.15M 트리플 세트 (576×1008, 77f) | ~TB급 | `KlingTeam/CameraClone-Dataset` (HF) | UE5 합성 카메라-클론. 참조 기반 카메라 컨트롤 학습. 합성이라 저작권 클린 |
| **T2V-CompBench 영상** (Kaiyue) | OpenRAIL | 22모델 × 1,400 프롬프트, ~25.2k mp4 | 48.5GB | `Kaiyue/T2V-CompBench-Videos` 모델별 zip (익명 curl 200 확인) | 모델별 생성물 비교. ⚠️ 상업 모델(Kling/Gen-2/3/Pika) 생성물 포함 — openrail은 배포 라이선스일 뿐 원 모델 ToS와 별개 |
| **RekaDaily-10k-raw** (RekaAI) | Apache-2.0 | 397,171 영상 / 7,834시간 | 70TB (9,836 샤드) | `RekaAI/RekaDaily-10k-raw` (HF) | 1인칭 실촬영 일상. 카드에 상업 사용 명시. 증분 릴리즈 |
| **VideoUFO** (WenhaoWang) | CC-BY-4.0 | 1.09M 클립 (1,291 토픽) | ~800GB 압축 | `WenhaoWang/VideoUFO` (HF, CSV+tar) | YouTube CC 영상 기반 실영상+캡션. T2V 학습용 최대급. ⚠️ 일부 CC-BY-NC 혼입 가능성(논문 주장 기준) |
| **Pexels-400k** (jovianzm) | MIT | 400,476 영상 **메타데이터만** | 18MB | `jovianzm/Pexels-400k` (parquet) | 영상 본체는 Pexels 직접 수집 필요(본체 라이선스도 상업 허용). i2v 페어: `img2vid-pexels-350k` |
| **Disney-VideoGeneration** (Wild-Heart) | Apache-2.0 | 69 클립 (흑백 30fps) | 소량 | `Wild-Heart/Disney-VideoGeneration-Dataset` (HF) | Steamboat Willie(2024 PD). 저작권 클린, 소규모 |

## 제외 (라이선스 확인 결과)

| 코퍼스 | 사유 |
|---|---|
| LTX-Video 샘플 | **커스텀 라이선스** (OpenRAIL-M 아님). 연매출 $10M+ 기업은 유료 계약 필수, 위반 시 2배 배상. 회사 규모상 리스크 |
| Wan2.2 샘플 | HF 리포에 mp4 없음(README 헤더 1개뿐). Apache-2.0이지만 샘플 물량이 수집 가치 미달 |
| OpenVid-1M | CC-BY-4.0 태그지만 README에 "research and non-commercial" 명시. 모순 — 상업 부적절 |
| UltraVideo (April Lab) | `license: other` + Wan 라이선스 이중. 상업 여부 미확정 |
| Vera-Layered (Netflix) | Apache-2.0이나 업스트림 DAVIS(CC-BY-NC) 혼입 가능성. 파일 레벨 검증 전 보류 |

## 우선순위 (NU 폐쇄루프 기준)

1. **VideoFeedback** (진행 중) — 품질 주석 → 생성물 선별/폐기 모델(nu-result-arena 연계)
2. **MultiCamVideo** — 카메라 무브먼트 → 연출 인텔리전스(nu-video-direction-skills 연계)
3. **CameraClone** — 참조 기반 카메라 컨트롤 → 리캠/리프레임 파이프라인(nu-reframe-lab 연계)
4. **T2V-CompBench** — 모델별 생성물 비교 → 벤치마크 레퍼런스(nu-benchmark-lab 연계)
5. VideoUFO / RekaDaily — 대규모 실영상. 스토리지 계획 후(70TB는 별도 협의)

## 다운로드 실행 패턴

모두 HF 비게이트 리포이므로 동일 패턴: `scripts/site_collectors/videofeedback_collect.py` 참조
- 파일 목록: `GET https://huggingface.co/api/datasets/<repo>` → siblings
- 개별 파일: `GET https://huggingface.co/datasets/<repo>/resolve/main/<path>` (302 → CDN)
- 동시성 12, `.part` 임시파일→rename, 재실행 시 캐시 스킵
- Windows venv에서 돌리므로 OUT_DIR은 Windows 경로(`C:\...`)로 지정, 완료 후 NAS/WSL로 cp
