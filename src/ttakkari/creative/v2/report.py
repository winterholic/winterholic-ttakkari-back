"""레지스트리 상태에서 능력 행렬 문서를 생성한다. 손으로 쓰지 않으므로 코드·스냅샷과 어긋나지 않는다.

실행: python -m ttakkari.creative.v2.report > docs/design/creative-studio-v2-capability-matrix.md
"""

from __future__ import annotations

from collections import Counter

from ttakkari.creative.v2.capabilities import CAPABILITIES, LEVEL_RANK
from ttakkari.creative.v2.registry import Registry

VERDICT_LABEL = {
    "studio_executable": "스튜디오 실행 가능",
    "recipe_demonstrated": "레시피 사례 실행됨(lab)",
    "capabilities_executed": "능력별 실행됨, 조합 미실행",
    "partially_demonstrated": "부분 실행",
    "unverified": "설치만, 실행 미검증",
    "blocked": "차단(필수 능력 누락)",
}


def _esc(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def matrix_markdown(registry: Registry | None = None) -> str:
    registry = registry or Registry()
    meta = registry.snapshot_meta()
    matrix = registry.recipe_matrix()
    counts = Counter(m.verdict for m in matrix)
    out: list[str] = ["# Creative Studio v2 능력·레시피 행렬", ""]
    out.append("이 문서는 `python -m ttakkari.creative.v2.report`로 생성한다. 손으로 고치지 않는다.")
    out.append("")
    if meta["available"]:
        out += [
            f"- 호스트 스냅샷: {meta['generated_at']} / {meta['host']['platform']}",
            f"- 탐침 {meta['probe_count']}개, lab 기록 {meta['record_count']}건",
            f"- 스냅샷 명령: `{meta['command']}`",
        ]
    else:
        out.append("- 호스트 스냅샷 없음: 모든 능력이 declared로 처리된다.")
    out += [
        "",
        "## 수준 정의",
        "",
        "| 수준 | 뜻 |",
        "|---|---|",
        "| missing | 설치 탐침 실패 또는 등록·실행된 적 없음 |",
        "| declared | 선언만 있음(스냅샷 없음, 또는 스킬에 실행 코드 없음) |",
        "| installed | 이 호스트에 있음. 해당 능력을 실행한 기록은 없음 |",
        "| executed_partial | lab 기록이 있으나 부분 성공·결과물 없음 |",
        "| executed | lab 기록이 completed이고 결과물이 있음. 독립 검수는 아님 |",
        "| verified | 독립 검수 pass 기록이 있음 |",
        "",
        "연동 범위: creative_runtime은 스튜디오 제작 코드가 호출한다. app_service는 앱 서비스로 있으나 제작에 연결되지 않았다. "
        "lab_only는 lab에서 수동 실행만 됐다. none은 연결 없음.",
        "",
        "## 요약",
        "",
    ]
    out += [f"- {VERDICT_LABEL[v]}: {counts.get(v, 0)}개" for v in VERDICT_LABEL]
    out += [
        "",
        "## R01~R20 레시피 판정",
        "",
        "| ID | 레시피 | 판정 | 가장 약한 필수 능력 | 누락 | 설치만(미실행) | 레시피 사례 |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in matrix:
        weakest = ", ".join("/".join(w) for w in m.weakest[:3]) + (" 외" if len(m.weakest) > 3 else "")
        missing = "; ".join("/".join(g.options) for g in m.groups if LEVEL_RANK[g.level] <= LEVEL_RANK["declared"]) or "-"
        unverified = "; ".join("/".join(g.options) for g in m.groups if g.level == "installed") or "-"
        cases = ", ".join(m.cases) or "없음"
        out.append(
            f"| {m.recipe_id} | {_esc(m.name)} | {VERDICT_LABEL[m.verdict]} | {_esc(weakest)} | {_esc(missing)} | {_esc(unverified)} | {_esc(cases)} |"
        )
    out += ["", "### 레시피별 필수 능력 제공자", ""]
    for m in matrix:
        out += [
            f"#### {m.recipe_id} {m.name}",
            "",
            f"{m.summary}",
            "",
            "| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |",
            "|---|---|---|---|---|",
        ]
        for g in m.groups:
            best = g.best
            evidence = "; ".join(best.evidence[:2]) if best else "-"
            out.append(
                f"| {_esc('/'.join(g.options))} | {g.level} | {best.entry_id if best else '없음'} | {best.integration if best else '-'} | {_esc(evidence)} |"
            )
        if m.constraints:
            out += ["", "제약: " + "; ".join(m.constraints)]
        out.append("")
    out += ["## 능력별 최고 수준", "", "| 능력 | 이름 | 최고 수준 | 제공자 |", "|---|---|---|---|"]
    for cap, (name, _) in CAPABILITIES.items():
        providers = registry.providers(cap)
        best = providers[0] if providers else None
        out.append(f"| {cap} | {_esc(name)} | {best.level if best else 'missing'} | {best.entry_id if best else '없음'} |")
    for title, kind in (("도구", "tool"), ("모델", "model"), ("스킬", "skill")):
        out += [
            "",
            f"## {title} 레지스트리",
            "",
            "| ID | 이름 | 설치 | 버전 | 연동 | 능력 수준 | 차단 사유 |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in registry.states(kind):
            installed = {True: "예", False: "아니오", None: "미확인"}[s.installed]
            levels = ", ".join(f"{c}={lv}" for c, lv in s.levels.items()) or "-"
            out.append(
                f"| {s.id} | {_esc(s.name)} | {installed} | {s.version or '-'} | {s.integration} | {_esc(levels)} | {_esc('; '.join(s.blockers)) or '-'} |"
            )
    out.append("")
    return "\n".join(out)


def main() -> None:
    print(matrix_markdown(), end="")


if __name__ == "__main__":
    main()
