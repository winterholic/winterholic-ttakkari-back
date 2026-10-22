# Creative Studio v2 능력·레시피 행렬

이 문서는 `python -m ttakkari.creative.v2.report`로 생성한다. 손으로 고치지 않는다.

- 호스트 스냅샷: 2026-10-22T14:45:31.192278+00:00 / macOS-27.0-arm64-arm-64bit-Mach-O
- 탐침 28개, lab 기록 21건
- 스냅샷 명령: `~/development/projects/ttakkari-studio-v2-back/src/ttakkari/creative/v2/probe.py --out src/ttakkari/creative/v2/data/capability_snapshot.json`

## 수준 정의

| 수준 | 뜻 |
|---|---|
| missing | 설치 탐침 실패 또는 등록·실행된 적 없음 |
| declared | 선언만 있음(스냅샷 없음, 또는 스킬에 실행 코드 없음) |
| installed | 이 호스트에 있음. 해당 능력을 실행한 기록은 없음 |
| executed_partial | lab 기록이 있으나 부분 성공·결과물 없음 |
| executed | lab 기록이 completed이고 결과물이 있음. 독립 검수는 아님 |
| verified | 독립 검수 pass 기록이 있음 |

연동 범위: creative_runtime은 스튜디오 제작 코드가 호출한다. app_service는 앱 서비스로 있으나 제작에 연결되지 않았다. lab_only는 lab에서 수동 실행만 됐다. none은 연결 없음.

## 요약

- 스튜디오 실행 가능: 0개
- 레시피 사례 실행됨(lab): 1개
- 능력별 실행됨, 조합 미실행: 1개
- 부분 실행: 0개
- 설치만, 실행 미검증: 8개
- 차단(필수 능력 누락): 10개

## R01~R20 레시피 판정

| ID | 레시피 | 판정 | 가장 약한 필수 능력 | 누락 | 설치만(미실행) | 레시피 사례 |
|---|---|---|---|---|---|---|
| R01 | 게임틱한 3D | 설치만, 실행 미검증 | asset.model_3d, anim.rig_3d, anim.character_3d 외 | - | asset.model_3d; anim.rig_3d; anim.character_3d; asset.environment | 없음 |
| R02 | SF 공상영화 | 설치만, 실행 미검증 | asset.environment, asset.model_3d, vfx.composite 외 | - | asset.environment; asset.model_3d; vfx.composite; audio.sfx | three-cinematic-v1-v2 |
| R03 | React 컴포넌트 조립 | 설치만, 실행 미검증 | render.kinetic_typo, render.dataviz | - | render.kinetic_typo; render.dataviz | remotion-intro-v1-v2, remotion-beat-v1-v2 |
| R04 | 일본 애니메이션 | 차단(필수 능력 누락) | anim.expression | anim.expression | anim.rig_2d/gen.video_i2v | 없음 |
| R05 | 2D 만화·모션코믹 | 차단(필수 능력 누락) | gen.layer_separation | gen.layer_separation | - | 없음 |
| R06 | 한국형 웹툰 | 능력별 실행됨, 조합 미실행 | qa.machine_video, edit.timeline, gen.image 외 | - | - | 없음 |
| R07 | 서양식 카툰 | 차단(필수 능력 누락) | anim.rig_2d, anim.squash_stretch | anim.rig_2d; anim.squash_stretch | render.vector_2d | 없음 |
| R08 | 시뮬레이션 게임 | 차단(필수 능력 누락) | sim.game_state | sim.game_state | asset.model_3d/asset.sprites | 없음 |
| R09 | 귀여운 생활형 3D | 설치만, 실행 미검증 | asset.model_3d, anim.character_3d, light.studio 외 | - | asset.model_3d; anim.character_3d; light.studio; asset.environment | 없음 |
| R10 | 사극·역사 재현 | 차단(필수 능력 누락) | research.fact_check | research.fact_check | asset.environment | 없음 |
| R11 | 옛날 전쟁영화 | 설치만, 실행 미검증 | anim.crowd, vfx.composite, asset.environment | - | anim.crowd; vfx.composite; asset.environment | 없음 |
| R12 | 판타지 영화 | 설치만, 실행 미검증 | asset.environment, vfx.composite | - | asset.environment; vfx.composite | 없음 |
| R13 | 픽셀아트 게임 | 차단(필수 능력 누락) | asset.sprites, render.pixel_art | asset.sprites; render.pixel_art | - | 없음 |
| R14 | 스톱모션·클레이 | 설치만, 실행 미검증 | render.stop_motion, light.studio, vfx.composite | - | render.stop_motion; light.studio; vfx.composite | 없음 |
| R15 | 수묵화·회화 | 차단(필수 능력 누락) | render.painterly_shader | render.painterly_shader | vfx.composite | 없음 |
| R16 | 다큐멘터리 | 차단(필수 능력 누락) | research.fact_check, research.map_data | research.fact_check; research.map_data | render.dataviz | legacy-catalog-longform-narration-story |
| R17 | 과학·기술 교육 | 차단(필수 능력 누락) | research.fact_check | research.fact_check | render.dataviz; sim.physics_viz | legacy-catalog-sample-tech-explainer |
| R18 | 제품 광고 | 설치만, 실행 미검증 | asset.model_3d, light.studio | - | asset.model_3d; light.studio | 없음 |
| R19 | 뮤직비디오 | 레시피 사례 실행됨(lab) | qa.machine_video, edit.timeline, audio.music_analysis 외 | - | - | remotion-beat-v1-v2, acestep-ko-ballad-run1, legacy-catalog-sample-music-video |
| R20 | 공포·스릴러 | 차단(필수 능력 누락) | audio.sfx_spatial | audio.sfx_spatial | - | 없음 |

### 레시피별 필수 능력 제공자

#### R01 게임틱한 3D

필수 능력은 설치돼 있으나 4개는 실행 기록이 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| asset.model_3d | installed | tool.blender | lab_only |  |
| anim.rig_3d | installed | tool.blender | lab_only |  |
| anim.character_3d | installed | tool.blender | lab_only |  |
| anim.camera_virtual | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| asset.environment | installed | tool.blender | lab_only |  |
| light.cinematic | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| render.blender_offline/render.three_realtime/render.game_engine | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional); record:three-previz-roundtable(completed, 검수 unreviewed) |

#### R02 SF 공상영화

필수 능력은 설치돼 있으나 4개는 실행 기록이 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| asset.environment | installed | tool.blender | lab_only |  |
| asset.model_3d | installed | tool.blender | lab_only |  |
| anim.camera_virtual | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| vfx.volumetric | executed | tool.three | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| vfx.composite | installed | tool.artcraft | none |  |
| audio.sfx | installed | skill.ttakkari-video-audio | lab_only |  |
| render.blender_offline/gen.video_i2v/render.three_realtime | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional); record:three-previz-roundtable(completed, 검수 unreviewed) |

#### R03 React 컴포넌트 조립

필수 능력은 설치돼 있으나 2개는 실행 기록이 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| render.react_motion | executed | skill.ttakkari-remotion-setup | lab_only | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| render.kinetic_typo | installed | tool.remotion | lab_only |  |
| render.dataviz | installed | tool.remotion | lab_only |  |

#### R04 일본 애니메이션

필수 능력 1개가 없거나 선언뿐이라 지금은 실행할 수 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| asset.character_sheet | executed | model.flux2_klein_4b | lab_only | record:comfy-flux2-klein-character-edit(completed, 검수 conditional) |
| gen.image_edit | executed | model.flux2_klein_4b | lab_only | record:comfy-flux2-klein-character-edit(completed, 검수 conditional) |
| anim.expression | missing | 없음 | - | - |
| anim.lipsync | executed | skill.studio-character-lipsync | lab_only | record:three-character-talk-v1-v2(completed, 검수 conditional) |
| anim.rig_2d/gen.video_i2v | installed | model.wan22_i2v_14b | lab_only |  |
| qa.continuity | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |

#### R05 2D 만화·모션코믹

필수 능력 1개가 없거나 선언뿐이라 지금은 실행할 수 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| gen.image | executed | model.flux2_klein_4b | lab_only | record:comfy-flux2-klein-character-edit(completed, 검수 conditional) |
| gen.layer_separation | missing | 없음 | - | - |
| render.react_motion | executed | skill.ttakkari-remotion-setup | lab_only | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| anim.camera_virtual | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| edit.captions | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |

#### R06 한국형 웹툰

필수 능력은 각각 실행 기록이 있으나 이 레시피 조합을 끝까지 만든 기록은 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| gen.image | executed | model.flux2_klein_4b | lab_only | record:comfy-flux2-klein-character-edit(completed, 검수 conditional) |
| gen.image_edit | executed | model.flux2_klein_4b | lab_only | record:comfy-flux2-klein-character-edit(completed, 검수 conditional) |
| render.react_motion | executed | skill.ttakkari-remotion-setup | lab_only | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.captions | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |

#### R07 서양식 카툰

필수 능력 2개가 없거나 선언뿐이라 지금은 실행할 수 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| render.vector_2d | installed | tool.svg_canvas | lab_only |  |
| anim.rig_2d | missing | 없음 | - | - |
| anim.squash_stretch | missing | 없음 | - | - |
| render.react_motion | executed | skill.ttakkari-remotion-setup | lab_only | record:remotion-intro-v1-v2(completed, 검수 conditional) |

#### R08 시뮬레이션 게임

필수 능력 1개가 없거나 선언뿐이라 지금은 실행할 수 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| sim.game_state | missing | tool.godot | none |  |
| render.game_engine/render.three_realtime | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional); record:three-previz-roundtable(completed, 검수 unreviewed) |
| asset.model_3d/asset.sprites | installed | tool.blender | lab_only |  |
| render.react_motion | executed | skill.ttakkari-remotion-setup | lab_only | record:remotion-intro-v1-v2(completed, 검수 conditional) |

#### R09 귀여운 생활형 3D

필수 능력은 설치돼 있으나 4개는 실행 기록이 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| asset.model_3d | installed | tool.blender | lab_only |  |
| anim.character_3d | installed | tool.blender | lab_only |  |
| light.studio | installed | tool.blender | lab_only |  |
| asset.environment | installed | tool.blender | lab_only |  |
| render.blender_offline/render.three_realtime/render.game_engine | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional); record:three-previz-roundtable(completed, 검수 unreviewed) |

제약: 특정 상용 게임 자산·캐릭터를 복제하지 않는다

#### R10 사극·역사 재현

필수 능력 1개가 없거나 선언뿐이라 지금은 실행할 수 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| research.fact_check | missing | 없음 | - | - |
| asset.character_sheet | executed | model.flux2_klein_4b | lab_only | record:comfy-flux2-klein-character-edit(completed, 검수 conditional) |
| asset.environment | installed | tool.blender | lab_only |  |
| gen.image | executed | model.flux2_klein_4b | lab_only | record:comfy-flux2-klein-character-edit(completed, 검수 conditional) |
| render.blender_offline/gen.video_i2v | executed | tool.blender | lab_only | record:blender-eevee-cycles-probe(completed, 검수 unreviewed) |

#### R11 옛날 전쟁영화

필수 능력은 설치돼 있으나 3개는 실행 기록이 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| anim.crowd | installed | tool.blender | lab_only |  |
| vfx.particles | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| vfx.composite | installed | tool.artcraft | none |  |
| asset.environment | installed | tool.blender | lab_only |  |
| qa.continuity | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| render.blender_offline/gen.video_i2v | executed | tool.blender | lab_only | record:blender-eevee-cycles-probe(completed, 검수 unreviewed) |

#### R12 판타지 영화

필수 능력은 설치돼 있으나 2개는 실행 기록이 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| asset.environment | installed | tool.blender | lab_only |  |
| asset.model_3d/gen.image | executed | model.flux2_klein_4b | lab_only | record:comfy-flux2-klein-character-edit(completed, 검수 conditional) |
| vfx.particles | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| vfx.composite | installed | tool.artcraft | none |  |
| anim.camera_virtual | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| render.blender_offline/gen.video_i2v | executed | tool.blender | lab_only | record:blender-eevee-cycles-probe(completed, 검수 unreviewed) |

#### R13 픽셀아트 게임

필수 능력 2개가 없거나 선언뿐이라 지금은 실행할 수 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| asset.sprites | missing | tool.godot | none |  |
| render.pixel_art | missing | tool.godot | none |  |
| render.game_engine/render.react_motion | executed | skill.ttakkari-remotion-setup | lab_only | record:remotion-intro-v1-v2(completed, 검수 conditional) |

#### R14 스톱모션·클레이

필수 능력은 설치돼 있으나 3개는 실행 기록이 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| render.stop_motion | installed | tool.blender | lab_only |  |
| asset.model_3d/gen.image | executed | model.flux2_klein_4b | lab_only | record:comfy-flux2-klein-character-edit(completed, 검수 conditional) |
| light.studio | installed | tool.blender | lab_only |  |
| vfx.composite | installed | tool.artcraft | none |  |

#### R15 수묵화·회화

필수 능력 1개가 없거나 선언뿐이라 지금은 실행할 수 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| gen.image | executed | model.flux2_klein_4b | lab_only | record:comfy-flux2-klein-character-edit(completed, 검수 conditional) |
| render.painterly_shader | missing | 없음 | - | - |
| vfx.composite | installed | tool.artcraft | none |  |

#### R16 다큐멘터리

필수 능력 2개가 없거나 선언뿐이라 지금은 실행할 수 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| research.fact_check | missing | 없음 | - | - |
| audio.tts | executed | model.supertonic3 | app_service | record:three-character-talk-v1-v2(completed, 검수 conditional) |
| render.react_motion | executed | skill.ttakkari-remotion-setup | lab_only | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| render.dataviz | installed | tool.remotion | lab_only |  |
| research.map_data | missing | 없음 | - | - |
| edit.captions | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |

#### R17 과학·기술 교육

필수 능력 1개가 없거나 선언뿐이라 지금은 실행할 수 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| render.react_motion | executed | skill.ttakkari-remotion-setup | lab_only | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| render.dataviz | installed | tool.remotion | lab_only |  |
| render.three_realtime | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional); record:three-previz-roundtable(completed, 검수 unreviewed) |
| sim.physics_viz | installed | tool.three | lab_only |  |
| research.fact_check | missing | 없음 | - | - |

#### R18 제품 광고

필수 능력은 설치돼 있으나 2개는 실행 기록이 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| asset.model_3d | installed | tool.blender | lab_only |  |
| light.studio | installed | tool.blender | lab_only |  |
| render.blender_offline | executed | tool.blender | lab_only | record:blender-eevee-cycles-probe(completed, 검수 unreviewed) |
| anim.camera_virtual | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| render.react_motion | executed | skill.ttakkari-remotion-setup | lab_only | record:remotion-intro-v1-v2(completed, 검수 conditional) |

#### R19 뮤직비디오

레시피 사례 실행 기록이 있다. 단 스튜디오 제작 런타임에는 연결되지 않았다(lab 수동 실행).

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| audio.music_analysis | executed | model.acestep15 | lab_only | record:acestep-ko-ballad-run1(completed, 검수 conditional) |
| edit.beat_cut | executed | skill.ttakkari-beat-sync | lab_only | record:remotion-beat-v1-v2(completed, 검수 conditional) |
| gen.video_i2v/render.react_motion | executed | skill.ttakkari-remotion-setup | lab_only | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.captions | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| qa.audio_sync | executed | skill.studio-music-generate-sync | lab_only | record:acestep-ko-ballad-run1(completed, 검수 conditional) |

#### R20 공포·스릴러

필수 능력 1개가 없거나 선언뿐이라 지금은 실행할 수 없다.

| 필수 능력 | 수준 | 제공자 | 연동 | 근거 |
|---|---|---|---|---|
| qa.machine_video | executed | tool.ffmpeg | creative_runtime | record:remotion-intro-v1-v2(completed, 검수 conditional) |
| edit.timeline | executed | skill.v1_ffmpeg_slideshow | creative_runtime | record:legacy-catalog-sample-music-video(completed, 검수 unreviewed); record:legacy-catalog-sample-tech-explainer(completed, 검수 unreviewed) |
| light.cinematic | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| anim.camera_virtual | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| audio.sfx_spatial | missing | 없음 | - | - |
| vfx.volumetric | executed | tool.three | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional) |
| render.blender_offline/gen.video_i2v/render.three_realtime | executed | skill.studio-3d-cinematic | lab_only | record:three-cinematic-v1-v2(completed, 검수 conditional); record:three-previz-roundtable(completed, 검수 unreviewed) |

## 능력별 최고 수준

| 능력 | 이름 | 최고 수준 | 제공자 |
|---|---|---|---|
| render.react_motion | React/Remotion 모션 렌더 | executed | skill.ttakkari-remotion-setup |
| render.kinetic_typo | 키네틱 타이포그래피 | installed | tool.remotion |
| render.dataviz | 데이터 시각화 렌더 | installed | tool.remotion |
| render.three_realtime | Three.js 3D 렌더 | executed | skill.studio-3d-cinematic |
| render.blender_offline | Blender 오프라인 렌더 | executed | tool.blender |
| render.game_engine | 게임 엔진 렌더 | missing | tool.godot |
| render.vector_2d | 벡터 2D 애니메이션 | installed | tool.svg_canvas |
| render.pixel_art | 픽셀아트 렌더 | missing | tool.godot |
| render.painterly_shader | 회화·수묵 셰이더 | missing | 없음 |
| render.stop_motion | 스톱모션 프레임 합성 | installed | tool.blender |
| asset.model_3d | 3D 모델링 | installed | tool.blender |
| asset.character_sheet | 캐릭터 시트 | executed | model.flux2_klein_4b |
| asset.environment | 환경 에셋 | installed | tool.blender |
| asset.sprites | 스프라이트·타일맵 | missing | tool.godot |
| gen.image | 이미지 생성 | executed | model.flux2_klein_4b |
| gen.image_edit | 참조 이미지 편집 | executed | model.flux2_klein_4b |
| gen.video_i2v | 이미지→영상 생성 | installed | model.wan22_i2v_14b |
| gen.video_t2v | 텍스트→영상 생성 | executed | model.wan22_ti2v_5b |
| gen.upscale | 영상 업스케일 | installed | model.seedvr2 |
| gen.interpolate | 프레임 보간 | installed | model.film_net |
| gen.layer_separation | 레이어 분리 | missing | 없음 |
| anim.rig_3d | 3D 리깅 | installed | tool.blender |
| anim.character_3d | 3D 캐릭터 애니메이션 | installed | tool.blender |
| anim.rig_2d | 2D 리깅 | missing | 없음 |
| anim.lipsync | 립싱크 | executed | skill.studio-character-lipsync |
| anim.expression | 표정 연기 | missing | 없음 |
| anim.squash_stretch | 스쿼시·스트레치 | missing | 없음 |
| anim.crowd | 군중 시뮬레이션 | installed | tool.blender |
| anim.camera_virtual | 가상 카메라 연출 | executed | skill.studio-3d-cinematic |
| sim.physics_viz | 물리·수학 시뮬레이션 | installed | tool.three |
| sim.game_state | 게임 상태·이벤트 | missing | tool.godot |
| vfx.composite | VFX 합성 | installed | tool.artcraft |
| vfx.volumetric | 볼류메트릭 효과 | executed | tool.three |
| vfx.particles | 파티클·연기·먼지 | executed | skill.studio-3d-cinematic |
| light.cinematic | 시네마틱 조명 | executed | skill.studio-3d-cinematic |
| light.studio | 스튜디오 조명 | installed | tool.blender |
| audio.tts | 음성 합성 | executed | model.supertonic3 |
| audio.stt | 음성 인식 | executed | model.faster_whisper |
| audio.music_generate | 음악 생성 | executed | model.acestep15 |
| audio.music_analysis | 음악 분석 | executed | model.acestep15 |
| audio.mix | 오디오 믹싱 | installed | tool.ffmpeg |
| audio.sfx | 효과음 | installed | skill.ttakkari-video-audio |
| audio.sfx_spatial | 공간 음향 | missing | 없음 |
| edit.timeline | 타임라인 조립 | executed | skill.v1_ffmpeg_slideshow |
| edit.captions | 자막·말풍선 | executed | skill.v1_ffmpeg_slideshow |
| edit.beat_cut | 박자 컷 편집 | executed | skill.ttakkari-beat-sync |
| edit.color_grade | 색보정 | installed | tool.ffmpeg |
| research.fact_check | 사실·고증 검증 | missing | 없음 |
| research.map_data | 지도·통계 자료 | missing | 없음 |
| qa.machine_video | 기계 영상 검사 | executed | tool.ffmpeg |
| qa.visual_semantic | 시각·의미 검수 | missing | model.multimodal_judge |
| qa.audio_sync | 음성·음악 동기 검사 | executed | skill.studio-music-generate-sync |
| qa.continuity | 연속성 검사 | executed | skill.studio-3d-cinematic |
| orch.checkpoint_resume | 체크포인트·재개 | executed | skill.studio-pipeline-checkpoints |
| plan.brief_storyboard | 브리프·스토리보드 | declared | skill.ttakkari-video-brief |

## 도구 레지스트리

| ID | 이름 | 설치 | 버전 | 연동 | 능력 수준 | 차단 사유 |
|---|---|---|---|---|---|---|
| tool.remotion | Remotion + React | 예 | 4.0.537 | lab_only | render.react_motion=executed, edit.beat_cut=executed, render.kinetic_typo=installed, render.dataviz=installed | - |
| tool.svg_canvas | SVG/Canvas (브라우저 렌더) | 예 | 1.64.1 | lab_only | render.dataviz=installed, render.vector_2d=installed | - |
| tool.three | Three.js | 예 | 0.185.0 | lab_only | render.three_realtime=executed, anim.camera_virtual=executed, light.cinematic=executed, vfx.particles=executed, vfx.volumetric=executed, anim.lipsync=executed, sim.physics_viz=installed | - |
| tool.r3f | React Three Fiber | 아니오 | - | none | render.three_realtime=missing | 탐침 r3f 미설치 |
| tool.blender | Blender (bpy 헤드리스) | 예 | 5.0.1 | lab_only | render.blender_offline=executed, asset.model_3d=installed, anim.rig_3d=installed, anim.character_3d=installed, light.studio=installed, asset.environment=installed, render.stop_motion=installed, vfx.particles=installed, anim.crowd=installed | - |
| tool.godot | Godot | 아니오 | - | none | render.game_engine=missing, sim.game_state=missing, render.pixel_art=missing, asset.sprites=missing | 탐침 godot 미설치 |
| tool.comfyui | ComfyUI (관리 런타임) | 예 | 0.42.0 | lab_only | - | - |
| tool.artcraft | ArtCraft 앱 13종 | 예 | - | none | render.vector_2d=installed, edit.timeline=installed, audio.mix=installed, vfx.composite=installed, asset.model_3d=installed | - |
| tool.ffmpeg | FFmpeg (imageio 정적 빌드) | 예 | - | creative_runtime | edit.timeline=executed, edit.captions=executed, qa.machine_video=executed, audio.mix=installed, edit.color_grade=installed | - |
| tool.rhubarb | Rhubarb Lip Sync 1.14 | 예 | 1.14.0 | lab_only | anim.lipsync=installed | - |

## 모델 레지스트리

| ID | 이름 | 설치 | 버전 | 연동 | 능력 수준 | 차단 사유 |
|---|---|---|---|---|---|---|
| model.flux2_klein_4b | FLUX.2 klein 4B | 예 | - | lab_only | gen.image=executed, gen.image_edit=executed, asset.character_sheet=executed | - |
| model.qwen_image_edit_2511 | Qwen-Image-Edit-2511 | 예 | - | lab_only | gen.image_edit=executed_partial | - |
| model.wan22_ti2v_5b | Wan2.2 TI2V 5B | 예 | - | lab_only | gen.video_t2v=executed, gen.video_i2v=installed | - |
| model.wan22_i2v_14b | Wan2.2 I2V 14B + LightX2V | 예 | - | lab_only | gen.video_i2v=installed | - |
| model.ltx23 | LTX-2.3 22B | 예 | - | none | gen.video_i2v=installed, gen.video_t2v=installed | - |
| model.hunyuanvideo15 | HunyuanVideo 1.5 720p | 예 | - | none | gen.video_t2v=installed | - |
| model.seedvr2 | SeedVR2 3B | 예 | - | lab_only | gen.upscale=installed | - |
| model.film_net | FILM 프레임 보간 | 예 | - | lab_only | gen.interpolate=installed | - |
| model.acestep15 | ACE-Step 1.5 | 예 | - | lab_only | audio.music_generate=executed, audio.music_analysis=executed | - |
| model.supertonic3 | Supertonic 3 (ONNX CPU TTS) | 예 | - | app_service | audio.tts=executed | - |
| model.faster_whisper | faster-whisper large-v3 | 예 | - | app_service | audio.stt=executed | - |
| model.multimodal_judge | 멀티모달 평가 모델 | 아니오 | - | none | qa.visual_semantic=missing | 등록·실행된 적이 없다 |

## 스킬 레지스트리

| ID | 이름 | 설치 | 버전 | 연동 | 능력 수준 | 차단 사유 |
|---|---|---|---|---|---|---|
| skill.v1_ffmpeg_slideshow | v1 FFmpeg 슬라이드쇼 렌더러(켄번즈·자막) | 예 | - | creative_runtime | edit.timeline=executed, edit.captions=executed | - |
| skill.ttakkari-remotion-setup | Remotion 세팅·렌더 규칙 | 예 | - | lab_only | render.react_motion=executed | - |
| skill.ttakkari-motion-rules | 모션·타이포 규칙 | 예 | - | lab_only | render.kinetic_typo=declared | 실행 코드 없음(지침 문서만 있음) |
| skill.ttakkari-video-brief | 브리프·룩 바이블·스토리보드 | 예 | - | lab_only | plan.brief_storyboard=declared | 실행 코드 없음(지침 문서만 있음) |
| skill.ttakkari-beat-sync | 박자 그리드 분석·박 단위 타임라인 | 예 | - | lab_only | edit.beat_cut=executed, audio.music_analysis=executed, qa.audio_sync=executed | - |
| skill.ttakkari-video-audio | 로컬 음악·효과음 제작과 믹스·피크 검사 | 예 | - | lab_only | audio.mix=installed, audio.sfx=installed | - |
| skill.ttakkari-video-qa | 렌더 후 프레임 검수 루프 | 예 | - | lab_only | qa.machine_video=executed | - |
| skill.studio-3d-cinematic | Three.js 시네마틱 3D 렌더 + 감사 게이트 | 예 | - | lab_only | render.three_realtime=executed, anim.camera_virtual=executed, light.cinematic=executed, vfx.particles=executed, qa.continuity=executed | - |
| skill.studio-character-lipsync | 한국어 TTS + 음절 립싱크 3D 캐릭터 | 예 | - | lab_only | anim.lipsync=executed, audio.tts=executed | - |
| skill.t2i-flux2-klein | FLUX.2 klein 텍스트→이미지 | 예 | - | lab_only | gen.image=executed | - |
| skill.edit1-flux2-klein | FLUX.2 klein 참조 편집(1) | 예 | - | lab_only | gen.image_edit=executed | - |
| skill.edit2-flux2-klein | FLUX.2 klein 참조 편집(2) | 예 | - | lab_only | gen.image_edit=executed, asset.character_sheet=executed | - |
| skill.edit1-qwen-image-edit-2511 | Qwen 참조 편집(1) | 예 | - | lab_only | gen.image_edit=executed_partial | - |
| skill.edit2-qwen-image-edit-2511 | Qwen 참조 편집(2) | 예 | - | lab_only | gen.image_edit=executed_partial | - |
| skill.t2v-wan22-5b | Wan2.2 5B 텍스트→영상 | 예 | - | lab_only | gen.video_t2v=executed | - |
| skill.i2v-wan22-5b | Wan2.2 5B 이미지→영상 | 예 | - | lab_only | gen.video_i2v=installed | - |
| skill.i2v-wan22-14b-lightx2v | Wan2.2 14B 이미지→영상 | 예 | - | lab_only | gen.video_i2v=installed | - |
| skill.interpolate-film-video | FILM 프레임 보간 | 예 | - | lab_only | gen.interpolate=installed | - |
| skill.upscale-seedvr2-video | SeedVR2 영상 업스케일 | 예 | - | lab_only | gen.upscale=installed | - |
| skill.studio-audio-mix | ffmpeg 오디오 믹싱(덕킹·라우드니스) | 예 | - | lab_only | audio.mix=installed | - |
| skill.studio-music-generate-sync | ACE-Step 음악 생성 + BPM·LRC 동기 | 예 | - | lab_only | audio.music_generate=executed, audio.music_analysis=executed, qa.audio_sync=executed | - |
| skill.studio-pipeline-checkpoints | 파이프라인 체크포인트·부분 재실행 | 예 | - | lab_only | orch.checkpoint_resume=executed | - |
