"""허용된 games_root의 바로 아래만 읽어 기존 프로젝트 연결을 진단한다."""
from __future__ import annotations

import json
import stat
from pathlib import Path

from ttakkari.games.adapter import get_adapter
from ttakkari.games.contract import SLUG_RE, GameContractError, game_dir, read_manifest


def _package(root: Path) -> dict:
    path = root / "package.json"
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_size > 64 * 1024:
        raise ValueError("package.json은 64KB 이하의 일반 파일이어야 합니다.")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise GameContractError("package.json 형식을 확인해 주세요.", "bad_manifest")
    return data


def discover_projects(registered: dict[str, str | None], limit: int = 100) -> dict:
    # game_dir가 games_root의 allowed_roots 경계를 먼저 검사한다.
    base = game_dir("discovery-root-check").parent
    if not base.exists():
        return {"candidates": [], "has_more": False}
    entries = sorted((p for p in base.iterdir() if not p.name.startswith(".") and SLUG_RE.fullmatch(p.name)
                      and (p.is_symlink() or p.is_dir())), key=lambda p: p.name)
    candidates = []
    for root in entries[:limit]:
        item = {"slug": root.name, "name": root.name, "engine_id": None,
                "game_id": registered.get(root.name), "can_register": False, "issues": []}
        issues = item["issues"]
        try:
            if root.is_symlink():
                raise ValueError("다른 위치를 가리키는 폴더는 연결하지 않아요.")
            manifest, _ = read_manifest(root)
            if manifest.id != root.name:
                raise ValueError("게임 설정의 id와 폴더 이름이 달라요.")
            item.update(name=manifest.name, engine_id=manifest.engine)
            get_adapter(manifest.engine)  # 지원 여부만 확인. 설치·빌드·스크립트 실행 없음.
            if root.name in registered:
                if not item["game_id"]:
                    issues.append("보관된 게임이에요. 기존 프로젝트를 먼저 확인해 주세요.")
            else:
                package = _package(root)
                scripts = package.get("scripts")
                if not isinstance(scripts, dict) or not isinstance(scripts.get("build"), str) or not scripts["build"].strip():
                    raise ValueError("package.json에 build 명령이 필요해요.")
                if not (root / ".git").exists():
                    raise ValueError("소스 이력을 보존할 Git 저장소가 필요해요.")
                item["can_register"] = True
                issues.append("연결 준비가 됐어요. 실제 플레이·캡처·저장 호환성은 연결 후 확인해 주세요.")
                if any("server" in key or "server" in str(value) for key, value in scripts.items()):
                    issues.append("서버 관련 명령이 있어요. 게임 미리보기는 정적 웹 빌드만 제공해요.")
        except (GameContractError, OSError, ValueError, UnicodeError) as e:
            if isinstance(e, GameContractError) and e.code == "no_manifest":
                issues.append("ttakkari-game.json 게임 설정이 필요해요. 원본 파일은 변경하지 않았어요.")
            elif isinstance(e, OSError):
                issues.append("게임 설정이나 package.json 파일을 읽지 못했어요.")
            else:
                issues.append(str(e))
        candidates.append(item)
    return {"candidates": candidates, "has_more": len(entries) > limit}
