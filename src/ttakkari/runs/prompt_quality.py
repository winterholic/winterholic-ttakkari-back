"""오케스트레이터가 작업자에게 보내는 지시의 가독성 검사(도구 단계의 미들웨어).

기획 세션(특히 Codex Sol)이 띄어쓰기를 지운 전보문("원3무효셀만재실행은앞지시와같으며...")으로 지시를 써서 사람이 읽을 수 없고
모델도 형태소를 잘못 나눠 읽게 된다. 2026-10-19 이후 작업자 지시의 약 절반이 이랬다(Sol 48%, Opus 10%, Muse·Copilot 0%).
문장으로 부탁하는 것보다 spawn_worker·message_worker 에서 걸러 다시 쓰게 하는 쪽이 확실하다.

판정: 한글이 150자 이상인 지시에서 '띄어쓰기 없이 9자 이상 이어진 한글 덩어리'가 한글 전체의 8% 이상이면 압축체다.
실제 지시 2,400여 건으로 맞췄다(정상 문장은 최대 2.2%, 압축체는 하위 10%가 18%). 영어·경로·코드가 섞인 지시와 짧은 지시는 걸리지 않는다."""

from __future__ import annotations

import re

_HANGUL_RUN = re.compile(r"[가-힣]+")
MIN_HANGUL = 150
LONG_RUN = 9
THRESHOLD = 0.08

REWRITE_GUIDE = (
    "이 지시는 띄어쓰기 없이 붙여 쓴 압축 문장이다(붙은 한글 덩어리 {pct}%). 사람이 읽기 어렵고 모델도 낱말을 잘못 끊어 읽는다. "
    "띄어쓰기를 지키는 일반 문장으로 항목을 나눠 다시 써라.\n"
    "목표: 한 문장\n작업 위치: 저장소·디렉터리\n먼저 읽을 것: 경로 목록\n범위와 금지: 쓰기 범위, 건드리지 말 것\n"
    "완료 기준: 확인 가능한 항목\n검증 방법: 실행할 명령\n보고: 첫 줄 `상태:`와 항목별 결과\n"
    "'앞 지시와 같다' 같은 표현은 쓰지 말고 필요한 내용을 그대로 적는다(작업자는 이전 지시를 못 볼 수 있다)."
)


def compressed_ratio(text: str) -> tuple[int, float]:
    """(한글 글자 수, 9자 이상 붙은 한글 덩어리가 차지하는 비율)."""
    runs = [len(m.group()) for m in _HANGUL_RUN.finditer(text)]
    total = sum(runs)
    if total == 0:
        return 0, 0.0
    return total, sum(n for n in runs if n >= LONG_RUN) / total


def rewrite_request(text: str) -> str | None:
    """압축체면 다시 쓰라는 안내문, 아니면 None."""
    total, ratio = compressed_ratio(text)
    if total < MIN_HANGUL or ratio < THRESHOLD:
        return None
    return REWRITE_GUIDE.format(pct=round(ratio * 100))
