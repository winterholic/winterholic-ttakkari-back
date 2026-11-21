"""Producers read repo-root templates/ relative to src/ttakkari/creative/v2.

Deployment runs from the git checkout (editable install, WorkingDirectory=repo, `uv run --no-sync`),
and uv_build packages only src/ttakkari, so a wheel/sdist-only deploy would silently lose templates/.
"""

from pathlib import Path

from ttakkari.creative.v2 import engines, hyperframes, scene_3d

REPO = Path(engines.__file__).resolve().parents[4]
REQUIRED = [
    "templates/__init__.py",
    "templates/frame_animation/presets.py",
    "templates/hyperframes/kafka_message_flow/index.html",
    "templates/hyperframes/kafka_message_flow/input_spec.json",
    "templates/hyperframes/kafka_message_flow/audio.wav",
    "templates/hyperframes/kafka_message_flow/vendor/gsap.min.js",
    "templates/scene_3d/blender_render.py",
    "templates/scene_3d/index.html",
    "templates/scene_3d/main.js",
    "templates/scene_3d/world.js",
]


def test_repo_root_contains_every_template_the_producers_read():
    assert (REPO / "pyproject.toml").is_file() and (REPO / "src" / "ttakkari").is_dir()
    missing = [p for p in REQUIRED if not (REPO / p).is_file()]
    assert not missing, missing


def test_producer_modules_resolve_the_same_template_root():
    assert scene_3d.TEMPLATE_DIR == REPO / "templates" / "scene_3d"
    source = Path(hyperframes.__file__).read_text(encoding="utf-8")
    assert 'parents[4]' in source and '"templates" / "hyperframes"' in source
