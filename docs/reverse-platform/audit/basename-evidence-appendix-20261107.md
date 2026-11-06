# 별도 부록 — basename 샘플 교정 근거 (2026-11-07)

이 부록은 독립 감사 보고서의 고정 스냅샷 판정을 바꾸지 않는다. 하네스 worker가 `90bfefea1eb78dd805b938ae538defa72d541736` (`Repair basename CLI sample parity`)에 추가한 별도 basename CLI 실행 증거를 연결한다. 원래 감사의 probe 결과나 결함 판정에 이 증거를 소급 적용하지 않는다.

## 근거 범위와 관찰값

저장소의 [실행 근거 README](../evidence/basename-repaired-20261107/README.md)는 macOS 27 arm64의 실제 `/usr/bin/basename`와 복제 CLI에 동일 argv를 전달하고 stdout/stderr 바이트 및 종료 코드를 비교했다고 기록한다. 실행 기록과 원시 캡처는 `before-capture-20261107T105000/` 및 `after-capture-20261107T105500/` 아래에 있다.

각 캡처의 `comparisons.json`와 독립 `server-verify.json`을 읽어 집계한 값:

| 캡처 | 캡처 행 | 서버 재산출 행 | match | mismatch | unverified |
|---|---:|---:|---:|---:|---:|
| before (`basename-repaired-before-20261107T105000`) | 16 | 16 | 12 | 4 | 0 |
| after (`basename-repaired-after-20261107T105500`) | 16 | 16 | 16 | 0 | 0 |

두 재산출 모두 해당 캡처의 고유 Run ID만 포함한다. 이 부록에서 실행한 검사는 JSON 파일의 행 수·판정 분포·Run ID와 SHA-256을 읽어 대조한 것이다. 원본 CLI 샘플 pytest와 `reverse_compare_recheck.py`는 이 감사자가 다시 실행하지 않았다. 명령 및 환경 설명은 README와 저장된 `run.log`를 출처로 한다.

## 원시 파일 무결성 식별자

| 파일 | SHA-256 |
|---|---|
| before `comparisons.json` | `d09861a2d0c5b69bf59d68b8770cc4ae644c559c2463d0585d1bf77045f14d56` |
| before `server-verify.json` | `32f9e672a055a45248c021a266f8244c4aa1c4f8fafbef9f7ef4dcded78910c9` |
| after `comparisons.json` | `622816e71f8b148a493b131dcc6dab240034a4f30e770614964fe6dfefa24aa2` |
| after `server-verify.json` | `c5ad98d9736f1494e957d0f880d07b65dca63e46d835ac8a1ac1cb5f3e9c87ff` |

## 한계와 후속 감사 조건

- 이 결과는 **직접 CLI 표본 실행**이다. 플랫폼 에이전트가 sample을 실행했다는 증거, 사용자 요청부터 완료 판정까지의 실제 E2E, 운영 배포 검증은 아니다.
- 16개 관찰 벡터는 이 입력들의 전후 차이만 증명한다. 전체 basename 인자 공간, 다른 구현체·운영체제 또는 전체 정확도를 증명하지 않는다.
- 최종 실제 E2E 후속 감사에서는 이 16개 벡터를 전체 정확도 근거로 일반화하지 않았는지 따로 확인한다.

## 이 부록의 재현 가능한 집계 명령과 출력

저장된 JSON만 집계했으며 CLI 재실행은 하지 않았다.

```text
$ python3 - <<'PY'
import json, pathlib, collections
root=pathlib.Path('docs/reverse-platform/evidence/basename-repaired-20261107')
for dirname in ('before-capture-20261107T105000','after-capture-20261107T105500'):
    d=root/dirname
    comp=json.loads((d/'comparisons.json').read_text())
    verify=json.loads((d/'server-verify.json').read_text())
    print(dirname, 'capture=',len(comp['cases']),dict(collections.Counter(x['result'] for x in comp['cases'])), 'server_checked=',verify['count'],dict(collections.Counter(x['result'] for x in verify['checked'])))
PY
before-capture-20261107T105000 capture= 16 {'match': 12, 'mismatch': 4} server_checked= 16 {'match': 12, 'mismatch': 4}
after-capture-20261107T105500 capture= 16 {'match': 16} server_checked= 16 {'match': 16}
```
