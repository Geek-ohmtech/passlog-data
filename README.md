# info-pipeline — PASSLOG 데이터 팩 (packs/v2) · v1 스냅샷

앱은 공공 API 를 직접 부르지 않는다. GitHub Actions 가 주기마다 데이터를 받아 **정적 데이터 팩**을 만들고 GitHub Pages 에 올린다.
서버는 없고, 인증키는 GitHub Secrets 에만 있다. 앱은 manifest 하나만 ETag 로 확인해 바뀐 데이터셋만 받는다
(`ios/Packages/PasslogKit/Sources/PasslogDataPack`). 설계: hub/PLAN.md §2, auto-update-design.md §3–7.

```
public/                                  (배포 리포 main 에 커밋 → actions/deploy-pages)
├─ packs/v2/
│  ├─ manifest.json                      앱이 확인하는 유일한 파일. 데이터가 바뀔 때만 다시 쓴다 (sequence+1)
│  ├─ status.json                        실행 시각 · 데이터셋별 상태 · 연속 실패 · 알림 (진단용)
│  ├─ history.json                       해시 파일 기록 — 참조가 끊긴 파일은 7일 뒤 삭제
│  └─ datasets/<id>.<sha256 앞 8자>.json  변경 불가 파일
└─ v1/                                   이미 설치된 앱(SnapshotClient) 호환: meta · rates · advisories · countries/{ISO2}
```

## 데이터셋

id 는 앱 `DatasetID` rawValue 와 같다(`tests/test_packlib.py` 가 Swift 파일과 대조). 등록표는 `scripts/packlib.py` `DATASETS`.

| id | 만드는 곳 | 키 | 주기(UTC cron) | 형식 |
|---|---|---|---|---|
| `advisories` | `scripts/advisories.py` (0404 공개 JSON) | 없음 | `17 */6 * * *` | 번들 advisories.json |
| `entry` | `fetch.py` 입국허가요건 | `DATA_GO_KR_KEY` | `17 */6 * * *` | 봉투 · data = {ISO2: {visaFree, maxStayDays, note}} |
| `rates` | `sources/fx.py` 수출입은행 AP01 → 없는 통화 Frankfurter v2 | `KOREAEXIM_KEY`(없으면 Frankfurter 만) | `25 2,5 * * 1-5` | v1 rates.json + `sources` · `dates` |
| `holidays-kr` | `sources/holidays.py` 큐레이션 + python-holidays + 천문연 특일 | 특일만 `DATA_GO_KR_KEY` | `40 18 * * 0` | 번들 kr-holidays.json |
| `holidays-world` | python-holidays (카탈로그 나라, 올해 · 내년) | 없음 | `40 18 * * 0` | 봉투 · data = {years, countries: {ISO2: [{date, name, nameEn?}]}} |
| `local-apps-status` | `sources/apps.py` (iTunes Lookup, `local-apps-check.py` 감쌈) | 없음 | `40 18 * * 0` | 봉투 · data = {apps: {appStoreId: {available, store, inKR, missingCount, hidden, …}}} |
| `kr-restaurants` · `kr-attractions` · `kr-festivals` | `sources/tourapi.py` (`tourapi-snapshot.py` 감쌈) | `DATA_GO_KR_KEY` | `50 21 * * *` + `40 18 * * 0` | 번들 kr-*.json |
| `source-status` | `sources/urlwatch.py` (허브 큐레이션 출처 URL 변경 감지) | 없음 | `50 21 * * *` | 봉투 |
| `destinations` 외 큐레이션 | `curated/*.json` 바이트 그대로 (`destinations` 는 사진 id 게이트) | 없음 | 매 실행 · push | 번들 파일 그대로 |
| `flights-icn` | (3단계, 아직 없음) | | | |

- 큐레이션: destinations · countries · checklist-rules · checklist-template · kr-places · travel-phrases · local-apps · dining-links ·
  airports · dish-wiki · taste-survey · destination-climate · (허브가 생기면) airline-baggage · travel-guide.
- 팩에는 **데이터만** 넣는다(코드 · 기능 스위치 없음, App Review 2.5.2).
- `push`(curated · scripts · data) 는 큐레이션만 다시 싣는다. 외부 호출(0404 · TourAPI …)은 예약 실행과 수동 실행에서만.

## 로컬 시험

```sh
cd ~/Desktop/프로젝트/PASSLOG
# 테스트 (네트워크 없음, python-holidays 가 없으면 그 부분은 건너뜀)
/usr/bin/python3 -I -m unittest discover info-pipeline/tests
# 키 · 네트워크 없이 shared/data 로 전체 팩 → 앱 DEBUG 빌드에서 받아 보기
/usr/bin/python3 -I info-pipeline/scripts/build_pack.py --offline --out /tmp/passlog-pack
python3 -m http.server 8765 --directory /tmp/passlog-pack       # → http://localhost:8765/packs/v2/manifest.json
#   환율까지 시험하려면 --rates-file info-pipeline/public/v1/rates.json (샘플 값) 을 더한다
#   shared/data 를 고친 뒤 같은 --out 으로 다시 돌리면 sequence 가 올라 '업데이트'를 시험할 수 있다
# 키 없는 온라인 소스만 실제로 받아 보기 (0404 경보 · Frankfurter · iTunes)
/usr/bin/python3 -I info-pipeline/scripts/build_pack.py --only advisories,rates,local-apps-status --out /tmp/passlog-pack
```

`--out` 은 꼭 준다(info-pipeline/public/v1 은 앱 번들 샘플이라 덮어쓰면 안 됨). 같은 `--out` 에 이전 팩이 있으면 그것과 견준다.

## 배포 (배포 리포 `passlog-data`)

1. GitHub 에서 **Public** 리포 `passlog-data` 를 만든다(README 없이).
2. 앱 리포에서 클론 폴더를 채운다 — 푸시는 하지 않고 명령만 보여 준다:
   `scripts/publish-data.sh ~/passlog-data --init https://github.com/<github-id>/passlog-data.git`
   (info-pipeline 코드 · `curated/`(shared/data) · `curated/photo-ids.json` · `scripts/vendor/`(tourapi-snapshot.py, local-apps-check.py))
3. 출력된 `git add / commit / push` 를 직접 실행한다.
4. 리포 Settings: Pages → Source **GitHub Actions** · Actions → General → Workflow permissions **Read and write**.
5. (선택) Secrets: `DATA_GO_KR_KEY`(공공데이터포털 Decoding 키 — 입국요건 · TourAPI · 특일을 각각 활용신청), `KOREAEXIM_KEY`.
   키가 없어도 경보 · Frankfurter 환율 · 공휴일 · 현지 앱 · 큐레이션은 켜진다.
6. Actions → datapack → **Run workflow** 로 첫 실행 → `https://<github-id>.github.io/passlog-data/packs/v2/manifest.json` 확인.
7. 앱 `Info.plist` `PasslogDataPackBaseURLs` 에 `https://<github-id>.github.io/passlog-data/packs/v2/` 를 넣는다
   (공개 URL 이라 앱에 들어가도 된다. v1 은 `PasslogSnapshotBaseURL` = `…/passlog-data/v1/`).
8. 큐레이션을 고치면 2–3번을 다시 한다(push 로 팩이 다시 만들어짐). 앱을 출시할 때마다 2번이 사진 id 목록에 새 버전을 더한다.

## 동작 규칙 (`scripts/build_pack.py`)

- 데이터셋마다 따로 성공 · 실패한다. 실패 · 키 없음 · 검증 실패면 **이전 파일을 그대로** 싣는다.
- 이전 파일과 견줄 때 실행 시각 같은 키(volatile: advisories `asOf`, kr-* `generatedAt`, 봉투 `dataAsOf` …)는 뺀다 → 같으면 이전 파일.
  단 heartbeat(경보 12시간, 입국요건 3일, 축제 2일, 맛집 · 볼거리 7일 …)가 지나면 같은 내용이라도 새 기준 시각으로 다시 싣는다
  (앱의 '오래됨'이 멈춘 파이프라인만 가리키게).
- 바뀐 게 하나라도 있을 때만 manifest 를 다시 쓴다: `schemaVersion 2`, `sequence` 단조 증가, `publishedAt`(KST),
  데이터셋마다 `path · sha256(소문자 hex) · size · schema · dataAsOf · ttlHours · staleAfterHours · minAppVersion? · large?`.
  `large` = 256KB 이상(경보 · 환율 · 입국 · 공휴일 · 국내 맛집/볼거리/축제 제외 — 앱이 바로 반영하는 자료라 셀룰러에서도 받는다).
- **sequence 를 되돌리지 말 것**: 앱은 활성 팩보다 작은 sequence 를 거부한다. `history.json` 의 `lastSequence` 가 manifest 가 지워져도
  번호를 잇게 하지만, 배포 리포를 새로 만들거나 `public/` 을 통째로 잃으면 1부터 다시 시작한다. 그때 앱은 활성 팩보다
  **3일 넘게 새로 발행된** 목록만 작은 sequence 라도 받아들인다(리플레이 방지) → 리포를 다시 만들었다면 `history.json` 을 옮겨 오거나,
  옮길 수 없으면 3일 뒤부터 회복된다.
- 이전에 실은 파일은 바이트 sha256 까지 다시 확인한다(줄바꿈 정규화 등으로 바뀌었으면 다시 쓴다). 배포 리포의 `.gitattributes` 가
  데이터 파일의 줄바꿈 변환을 막는다.
- 검증: 크기 5MB · JSON · 봉투 형식 · 경보 단계 0–4 · 나라 수 · 환율(KRW 기준, 전날 대비 ±20% — 몇 통화만 튀면 그 통화만 이전 값) ·
  공휴일(올해 포함) · kr-* 항목(contentId · title · https 사진) · **사진 id 게이트**(`curated/photo-ids.json` 에 사진이 없는 여행지가 있으면
  사진이 들어간 앱 버전으로 `minAppVersion` 을 올리고, 그런 버전이 없으면 거부).
- 연속 실패(경보 · 환율 2회, 나머지 4회)와 거부는 status.json `alerts` → `scripts/report_issues.py` 가 이슈를 열고(20시간에 한 번 댓글),
  회복하면 닫는다. 워크플로 자체가 실패하면 '데이터 팩: 워크플로 실패' 이슈.
- 60일 규칙: 공개 리포의 예약 워크플로는 60일 활동이 없으면 꺼진다 → 실제 산출물을 main 에 커밋한다(경보 heartbeat 로 하루 두 번 이상).
  꺼지면 Actions → datapack → Enable workflow (또는 `gh workflow enable datapack.yml`).

## TourAPI (국내 맛집 · 볼거리 · 축제)

수집 · 형식은 `scripts/tourapi-snapshot.py`(앱 리포, docs/TourAPI-데이터.md)가 정하고 `sources/tourapi.py` 는 그 `run_snapshot()` 을 감싼다.
- 원응답 캐시(키 없음)를 Actions cache 로 실행 사이에 유지한다. 나이 만료: 축제 20시간 · 시·군·구 목록 6.5일 · 상세 28일(±시차).
- 증분: 매일 `areaBasedSyncList2`(modifiedtime=날짜)로 지난 확인 뒤 바뀐 콘텐츠의 상세 · 그 시·군·구 목록 캐시만 지운다.
  하루치가 10쪽(1만 행)을 넘어 다 읽지 못하면 그날을 성공으로 치지 않고 다음 실행에서 다시 묻는다(7일이 넘으면 나이 만료에 맡김).
- 한도: 실행당 600회, 하루(KST) 합계 900회(개발 계정 1,000회/일). 다 못 모은 여행지는 이전 블록을 쓰고 다음 실행이 잇는다.
  첫 배포 때는 캐시가 비어 있어 전체를 채우는 데 이틀쯤 걸린다(그동안은 번들과 같은 내용).
- 축제 기간은 오늘 ~ 12개월 뒤 말일, 끝난 축제는 뺀다. 카탈로그에 없는 여행지 블록은 뺀다. 항목이 30% 넘게 줄면 거부.

## 인증키

| 환경 변수 | 발급처 | 용도 |
|---|---|---|
| `DATA_GO_KR_KEY` | 공공데이터포털 — API 마다 활용신청 | 입국허가요건(15075345) · TourAPI 4.0(KorService2) · 천문연 특일(15012690) · (3단계) 인천공항 운항편(15114085) |
| `KOREAEXIM_KEY` | 한국수출입은행 Open API | 현재환율(AP01) |

- Decoding 키를 넣는다(Encoding 키여도 한 번 풀어서 쓴다). 키 값은 출력 · 로그 · 파일 어디에도 남기지 않는다.
- 로컬에서 키로 돌릴 일은 거의 없다. 돌린다면 환경 변수로만: `DATA_GO_KR_KEY=… python3 -I scripts/build_pack.py --only entry --out /tmp/x`

### 사용 API

| 데이터 | 엔드포인트 | 상태 |
|---|---|---|
| 여행경보 | `POST www.0404.go.kr/travelAlert/apntStatus/getAlertList` (키 없음, 문서화 안 된 내부 JSON → 6시간 주기에서만) | 확인 |
| 입국허가요건 | `apis.data.go.kr/1262000/EntranceVisaService2/getEntranceVisaList2` | 확인 |
| 환율 | `oapi.koreaexim.go.kr/site/program/financial/exchangeJSON?data=AP01` → 없는 통화 `api.frankfurter.dev/v2/rates?base=USD` | 확인 |
| 특일(임시공휴일) | `apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo` | 파라미터 첫 실키 실행 때 확인 |
| TourAPI | `apis.data.go.kr/B551011/KorService2/*` (증분 `areaBasedSyncList2`) | 증분 파라미터 첫 실키 실행 때 확인 |
| 현지 앱 | `itunes.apple.com/lookup?id=…&country=…` (50개씩, 3.5초 간격) | 확인 |

## 여행경보 (advisories.json) — `scripts/advisories.py`

앱의 여행지 추천(경보 3·4단계 · 특별여행주의보 지역 제외, 2단계 감점)과 정보 카드가 쓰는 색인이다. 번들 `shared/data/advisories.json`
과 같은 스키마(schemaVersion 1).
- 단계 필드: `trvlCutn*`=1 · `trvlRfran*`=2 · `dptcnyAdvs*`=3 · `trvlPrhb*`=4 · `spclTrvlCutn*`=특별여행주의보. `level` = 나라 '나머지 전역' 기본 단계.
- `data/`: `mofa-country-codes.json`(이름 → ISO2) · `advisory-geo.json`(지역 문구 → 좌표/범위) · `advisory-overrides.json`(사람이 확인한 보정,
  0404 원문이 같을 때만) · `destination-areas.json`(카탈로그 여행지 행정구역 판정).
- 안전장치: 소스 실패 · 행 수 이상 · 한 번에 30개국 넘게 단계 변경 → 이전 파일 유지.
- 앱 번들 다시 만들기(네트워크 없이): `python3 -I info-pipeline/scripts/advisories.py --offline <getAlertList 응답.json> --catalog shared/data/destinations.json --out shared/data/advisories.json --strict`

## 앱에서 받는 법

- v2(데이터 팩): `manifest.json` 만 `If-None-Match` 로 확인 → 304 면 끝. 200 이면 sha256 이 바뀐 데이터셋만 받아 크기 · sha256 ·
  디코드를 확인하고 원자적으로 교체. 실패하면 이전 팩 → 번들.
- v1(예전 빌드): `meta.json` → `rates.json` · `countries/{ISO2}.json` (파일별 ETag). 내용이 같으면 파일을 다시 쓰지 않는다.
- 같은 내용이면 같은 파일 이름(해시)이라 CDN 캐시가 섞이지 않는다.
- 앱은 내용 하한도 본다(디코드만으로는 빈 파일이 통과하므로: 경보 150개국 · 여행지 50곳 · 나라 150개 · 공휴일 15개 · 환율 20통화 ·
  kr-* 여행지 묶음 · 설문 문항 …). 하한은 이 파이프라인 검증과 같거나 더 느슨하다. 결과가 정해진 실패(sha · 크기 · 형식)를 낸
  같은 파일은 24시간 동안 다시 받지 않는다(수동 '지금 업데이트'는 예외).
- 번들과 견주기: 앱의 `bundled-manifest.json`(scripts/build-bundled-manifest.py) 기준 시각보다 오래된 원격 파일은 쓰지 않는다.
  큐레이션 파일은 그 파일을 만든 시각(generatedAt) 전에 실린 원격 파일이 번들을 이기지 않는다.
  **앱 출시 전**: `python3 scripts/build-bundled-manifest.py --check`(어긋나면 다시 만들기)와
  `PASSLOG_RELEASE_CHECK=1 swift test --filter BundledManifestTests`(kr-* sha 까지 확인), 그리고 `scripts/publish-data.sh` 로
  배포 리포의 큐레이션을 같은 내용으로 맞춘다.

## 출처 표시

공공데이터 이용 조건에 따라 출처(외교부 해외안전여행, 한국수출입은행 · Frankfurter, 한국관광공사 TourAPI — '사진: 한국관광공사' ·
공공누리 유형)와 기준 시각을 표시한다. 내용은 참고용이며 출국 전 외교부 해외안전여행(0404.go.kr)에서 확인하도록 안내한다.

## 파일

| 파일 | 내용 |
|---|---|
| `scripts/build_pack.py` | 팩 빌더 (위 규칙) |
| `scripts/packlib.py` | 데이터셋 등록표 · 정규화 JSON · sha256 · 날짜 · dataAsOf 규칙 (앱 쪽 bundled-manifest 도 이 규칙을 쓰면 비교 기준이 같아진다) |
| `scripts/sources/{fx,holidays,apps,tourapi,urlwatch,climate,net}.py` | 소스 |
| `scripts/make_photo_ids.py` | 번들 사진 id → `curated/photo-ids.json` |
| `scripts/report_issues.py` | status 알림 → GitHub 이슈 |
| `scripts/fetch.py` · `scripts/advisories.py` | 외교부 · 수출입은행 · 0404 (build_pack 이 부른다. 단독 실행도 됨) |
| `.github/workflows/datapack.yml` | 예약 · push · 수동 실행 → 커밋 → Pages 배포 (액션은 SHA 고정) |
| `.github/legacy/snapshot.yml` | 예전 v1 워크플로 (사용 안 함) |
| `requirements.lock` | python-holidays 0.106 + 의존성 (해시 고정, 러너 전용) |
| `tests/` | `python3 -I -m unittest discover info-pipeline/tests` (네트워크 차단, 오프라인 픽스처) |
