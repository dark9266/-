# 크림 47k 브랜드 커버리지 갭 분석 — 2026-04-13

배치 5(호카/비이커/더한섬닷컴) 진행 중 시점. 배치 6+ 우선순위 데이터.

## 전제

- 크림 DB 총 47,212 상품
- 차익거래 비대상 제외 (럭셔리·전자: LV/Chanel/Rolex/Apple/Hermes 등): **9,331개 제외**
- **차익거래 유효 풀: 37,881개 (80.2%)**

## 현재 어댑터 13곳 직접 커버 브랜드

| 브랜드 | 어댑터 | 크림 상품수 |
|--------|--------|------------|
| Nike | nike, musinsa, 29cm, kasina, abcmart | 2,660 |
| Adidas | adidas, musinsa, 29cm, kasina | 2,499 |
| New Balance | nbkorea, musinsa, kasina | 2,502 |
| Vans | vans, musinsa, 29cm | 2,492 |
| Arc'teryx | arcteryx, wconcept | 2,470 |
| Salomon | salomon, kasina | (집계 미산정) |
| Jordan | nike(공식), musinsa | 2,487 (Nike 산하) |
| Converse | nike 산하 | 2,190 |

**소계 직접 커버 브랜드 합산**: ~17,300 (37,881 중 **45.7%**)

추가로 musinsa/29cm/wconcept/eql/tune/worksout 가 멀티 브랜드로 기타 다수 브랜드 부분 커버 — 이 부분은 어댑터 dump 결과 봐야 정확.

## 미커버 상위 브랜드 (배치 6 후보)

| 순위 | 브랜드 | 크림 상품 | 추정 소싱처 | 우선순위 근거 |
|-----|--------|----------|-----------|------------|
| 1 | **Asics** | 2,470 | asics.com/kr 공식 | 단일 브랜드 단일 어댑터로 1개 풀 통째 흡수 |
| 2 | **Puma** | 2,469 | kr.puma.com 공식 | 동일 (스니커 카테고리 큼) |
| 3 | **Patagonia** | 2,500 | patagonia.co.kr 공식 | 아웃도어 대형, 단일 어댑터 |
| 4 | **The North Face** | 2,240 | thenorthfacekorea.co.kr | 아웃도어 대형 |
| 5 | **Reebok** | 1,125 | reebok.co.kr 공식 | 단일 브랜드 |
| 6 | **Stussy** | 2,389 | 한국 공식 없음 → 무신사/29CM/카시나 의존 (이미 부분 커버) | 우선순위 낮음 |
| 7 | **Supreme** | 3,137 | 일본/미국 공식 직배 — 한국 소싱처 한정 | 우선순위 낮음 (재고 확보 어려움) |
| 8 | **Carhartt WIP** | 1,984 | carhartt-wip.com 공식 | 워크웨어 |
| 9 | **New Era** | 742 | newerakorea.co.kr | 캡 단일 |
| 10 | **Dr. Martens** | 10 | drmartens.co.kr | 상품수 적음, 보류 |

## 배치 6 권장 (4곳)

크림 풀 흡수 효과 + 단일 브랜드 → 단일 어댑터 ROI 기준:

1. **Asics** — 2,470 상품, 단일 어댑터로 전부 흡수. 스니커 차익거래 핫 카테고리.
2. **Puma** — 2,469 상품, 동일 논리.
3. **Patagonia** — 2,500 상품, 아웃도어. 살로몬/아크테릭스 라인업과 시너지.
4. **The North Face** — 2,240 상품, 아웃도어 대형.

**예상 커버리지 증분**: +9,679 상품 → 누적 ~26,979 / 37,881 = **71.2%**

## 배치 7 (이후)

- Reebok (1,125)
- Carhartt WIP (1,984) — 공식몰 + 무신사 보강
- 한섬 패밀리 추가 (시스템/마인/타임 등 — 더한섬닷컴 어댑터 결과에 따라 자동 흡수 가능)

## 메모

- Sacai/ALD/Kith/BAPE/Balenciaga 등 하이엔드 스트리트는 상품수 적지만 단가 높아 별개 추적 필요 (배치 8+).
- Stussy/Supreme 은 한국 소싱처에서 잡기 어려움 — 일본 직구/병행 채널 분석 후 결정 (Phase 4 이후).
- musinsa/29cm/wconcept 가 멀티 브랜드 흡수 중이라 실제 갭은 위 표보다 작음.
  배치 5 + 6 어댑터 가동 후 매칭율 측정해서 재산정 필요.
