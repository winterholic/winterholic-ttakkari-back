"""소유한 제작 자산(G0 CAS)에서 managed Comfy I2V 기준 이미지를 꺼낸다.

자산은 제작 manifest에 있는 (id=이름, sha256) 쌍으로만 지정한다. 임의 경로·URL은 받지 않는다.
CAS 읽기(`read_artifact`)가 sha 형식, 디렉터리 이탈, 심볼릭 링크, 해시 불일치를 거부하고,
이미지 형식·크기·해상도·애니메이션은 `validate_input_image`가 확인한다.
다른 제작의 자산은 이 제작 manifest에 없으므로 같은 메시지로 거부된다(존재 여부를 알려 주지 않는다).
"""

from __future__ import annotations

from ttakkari.creative.director import DirectorStore
from ttakkari.managed_comfy_input import (
    InputImage,
    InputImageError,
    validate_input_image,
)

_NOT_OWNED = "기준 이미지 id와 sha256이 이 제작의 자산 목록과 일치하지 않습니다."


def load_input_image(
    store: DirectorStore, production_id: str, asset_id: str, sha256: str
) -> InputImage:
    state = store.get_production(production_id)  # 제작이 없으면 FileNotFoundError
    record = state["artifacts"].get(asset_id) if isinstance(asset_id, str) else None
    if record is None or record["sha256"] != sha256:
        raise InputImageError(_NOT_OWNED)
    try:
        data = store.read_artifact(production_id, sha256)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise InputImageError(
            f"기준 이미지 자산을 안전하게 읽을 수 없습니다: {exc}"
        ) from exc
    return validate_input_image(asset_id=asset_id, sha256=sha256, data=data)
