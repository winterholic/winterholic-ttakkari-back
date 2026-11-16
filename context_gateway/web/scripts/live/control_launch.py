# 실제 Gateway 대조용 하네스. 코어를 수정하지 않는다.
# 2026-11-16 시점 코어는 publish_snapshot에서 stage 디렉터리를 0o555로 만든 뒤 다른 부모 디렉터리로 os.replace해서
# macOS에서 PermissionError(500)가 난다. 코어가 고쳐지면 이 파일 없이 control_app을 직접 띄워도 된다.
import os

from context_gateway import publishing

_orig = os.replace


def _replace(src, dst, *args, **kwargs):
    if os.path.isdir(src):
        os.chmod(src, 0o755)
    return _orig(src, dst, *args, **kwargs)


publishing.os.replace = _replace

from context_gateway.apps import create_control_app

app = create_control_app()
