# 클라우드 핫딜 수집기 (GitHub Actions)

`cloud_collect.py`는 PC 없이 30분마다 커뮤니티 핫딜을 읽어 `candidates.json`에 후보를 올립니다.
표준 라이브러리만 쓰므로 러너에 패키지를 설치할 필요가 없습니다.

## 동작
```
python3 scripts/cloud_collect.py            # 수집 + 병합
python3 scripts/cloud_collect.py --dry-run  # 파일을 쓰지 않고 결과만 확인
python3 scripts/cloud_collect.py --selftest # 네트워크 없이 파서 단위 검증
```
- 소스(2026-11-22 실제 검증, 7곳):

  | 소스 | 주소 | 모으는 값 | 품질 기준 |
  |---|---|---|---|
  | 뽐뿌 게시판 | `ppomppu.co.kr/rss.php?id=ppomppu` | 제목+가격, 추천·댓글 | 추천 3 또는 댓글 9 |
  | 뽐뿌 쇼핑뽐뿌 | `ppomppu.co.kr/rss.php?id=pmarket` | 제목+가격, 추천·댓글 | 추천 3 또는 댓글 9 |
  | 클리앙 알뜰구매 | `clien.net/service/board/jirum` | 제목+가격, 추천·댓글 | 추천 5 또는 댓글 15 |
  | 이토랜드 핫딜 | `etoland.co.kr/bbs/board.php?bo_table=hotdeal` | 제목·가격·몰·추천·댓글·조회 | 추천 5·댓글 10·조회 200 중 하나 |
  | 아카라이브 핫딜 | `arca.live/b/hotdeal` | 제목·가격·몰·추천·댓글·조회 | 추천 2·댓글 5·조회 900 중 하나 |
  | 딜바다 국내핫딜 | `dealbada.com/bbs/board.php?bo_table=deal_domestic` | 제목(가격 포함)·조회 | 조회 400 이상 |
  | 어미새 | `eomisae.co.kr/rss` | 제목(가격 포함) | 추천 없음 → 가격+상품명 8자 이상, 결제인증·잡담·해외 글 제외 |

- 필터: 화장품·숙박·여행권·중고·해외직구·정보성 글 제외, 가격 표기 필수(1,000~3,000,000원),
  위 표의 소스별 품질 기준(환경변수 `MIN_RECS_<소스>`, `MIN_COMMENTS_<소스>`, `MIN_VIEWS_<소스>`로 변경),
  1회 최대 6건(`MAX_NEW`)을 **소스당 2건 이하**(`PER_SOURCE_MAX`)로 번갈아 골라 한 곳이 독식하지 않게 함,
  기존·게시된 상품과 제목 유사도 0.85 이상이면 중복 제외
- 가격 이력: `data/price-history.json` (상품별 최저가·마지막 가격)
- 새 후보가 없으면 파일을 쓰지 않습니다 → 워크플로도 커밋하지 않습니다.

## 제외한 후보(2026-11-22 같은 환경에서 재확인)
- 타임아웃: 쿨엔조이·플레이웨어즈·11번가·다나와(10~20초 이상 응답 없음)
- 403/430/폐쇄: SSG·롯데온·쿠팡·지마켓·루리웹·에펨코리아·오늘의유머·뽐뿌 웹게시판(HTML)·디시 핫딜갤러리
- 서비스 종료/매물: 위메프(도메인 판매), SLR클럽(지름게시판 폐쇄)
- JS 렌더링 껍데기: 티몬·무신사·오늘의집(SSL 오류), 네이버+ 랭킹(오픈API 키 필요)
- 성격 부적합: 뽐뿌 휴대폰뽐뿌·쿠폰게시판(판매점 광고·포인트 이벤트), 뽐뿌 PC/인터넷(질문 게시판), 더쿠 HOT(쇼핑 글 아님)

## 워크플로 설치
`.github/workflows/collect-hotdeals.yml`이 저장소에 있어야 30분 주기가 동작합니다.
이 연결(Pickle GitHub App)에는 워크플로 파일 쓰기 권한이 없어 자동으로 올릴 수 없습니다.
`scripts/collect-hotdeals.yml.template` 내용을 `.github/workflows/collect-hotdeals.yml`로 넣으면 됩니다.
