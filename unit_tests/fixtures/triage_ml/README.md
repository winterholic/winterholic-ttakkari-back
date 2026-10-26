# 외부 라벨 평가 산출물 (C4 stage 2)

| 파일 | 내용 |
|---|---|
| `eval_report.json` | `scripts/eval_triage_ml.py` 실제 실행 결과(헤드 평가, 관련성 평가, 문서당 비용) |
| `fineweb_c_kor_manifest.json` | 학습/평가에 쓴 FineWeb-C 한국어 639건의 id, 라벨, train/test 분할, 본문 sha256. 본문은 저장하지 않는다(ODC-By, CommonCrawl 약관) |

## 라벨 출처
- 교육적 가치, 문제 콘텐츠: `data-is-better-together/fineweb-c` 의 `kor_Hang`. 커뮤니티 **사람** 주석자 12명, 문서당 1명, ODC-By 1.0.
  라벨 값은 None/Minimal/Basic/Good/Excellent 와 Problematic Content 표시다. LLM 라벨이 아니다.
  문서당 주석자가 1명이라 주석자 간 일치도는 알 수 없다.
- 관련성: `castorini/mr-tydi` 한국어 train(Apache-2.0). TyDi QA 사람 판정의 정답 문단이 양성, BM25 상위 비정답 문단이 음성이다.
  음성에는 주석되지 않은 정답이 섞일 수 있다. 질문-위키 문단 쌍이어서 임무 목표-웹 문서와는 분포가 다르다. 대리 평가다.

## 재현
```
PYTHONPATH=src ../back/.venv/bin/python scripts/eval_triage_ml.py --cache-dir /tmp/triage-ml-eval \
  --models-dir <fastembed 캐시: multilingual-e5-large, paraphrase-multilingual-MiniLM-L12-v2> --write-artifact --measure-cost
```
분할 seed는 20261025. 시험 분할은 lambda 선택, 임계값, 학습에 쓰지 않는다.
