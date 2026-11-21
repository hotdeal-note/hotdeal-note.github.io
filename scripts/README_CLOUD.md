# 클라우드 핫딜 수집기 (GitHub Actions)

`cloud_collect.py`는 PC 없이 30분마다 커뮤니티 핫딜을 읽어 `candidates.json`에 후보를 올립니다.
표준 라이브러리만 쓰므로 러너에 패키지를 설치할 필요가 없습니다.

## 동작
```
python3 scripts/cloud_collect.py            # 수집 + 병합
python3 scripts/cloud_collect.py --dry-run  # 파일을 쓰지 않고 결과만 확인
python3 scripts/cloud_collect.py --selftest # 네트워크 없이 파서 단위 검증
```
- 소스: 뽐뿌 게시판 RSS, 클리앙 알뜰구매 (둘 다 2026-11-22 실제 검증)
- 필터: 화장품·숙박·여행권·중고·해외직구·정보성 글 제외, 가격 표기 필수,
  추천 기준(뽐뿌 3 / 클리앙 5, `MIN_RECS_PPOMPPU`·`MIN_RECS_CLIEN`로 변경) 이상,
  1회 최대 6건(`MAX_NEW`), 기존·게시된 상품과 제목 유사도 0.85 이상이면 중복 제외
- 가격 이력: `data/price-history.json` (상품별 최저가·마지막 가격)
- 새 후보가 없으면 파일을 쓰지 않습니다 → 워크플로도 커밋하지 않습니다.

## 워크플로 설치
`.github/workflows/collect-hotdeals.yml`이 저장소에 있어야 30분 주기가 동작합니다.
이 연결(Pickle GitHub App)에는 워크플로 파일 쓰기 권한이 없어 자동으로 올릴 수 없습니다.
`scripts/collect-hotdeals.yml.template` 내용을 `.github/workflows/collect-hotdeals.yml`로 넣으면 됩니다.
