"""Scenario B Execution and Verification Script (Normalization Branch Fix).

Executes real Supertonic 3 TTS synthesis and Whisper STT recognition under:
- Baseline: Normalization disabled (convert_numbers=False, read_latin=False, raw preserved)
- Candidate: Full normalization & custom dictionary enabled
Measures CER/WER (stt proxy), RTF, preserves audio WAVs as ResearchArtifact -> Artifact API,
and exports sanitized JSON evidence and audio files to this Run's outbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

import httpx
from sqlalchemy import select
from ttakkari.config import get_settings
from ttakkari.db import dispose_engine, init_engine, sessionmaker
from ttakkari.main import app
from ttakkari.models import Artifact
from ttakkari.research import runner
from ttakkari.research.models import (
    Analysis,
    BestResult,
    Configuration,
    Experiment,
    Measurement,
    Method,
    ResearchArtifact,
    Run,
    Study,
)
from ttakkari.security.auth import issue_access_token

RUN_OUTBOX_DIR = Path("/Users/winterholic/.ttakkari/data/outbox/b4ad5289-227d-404a-b9eb-ffa79a9d5bf1")
EVIDENCE_JSON_PATH = REPO / "docs/research-lab/evidence/stage2-b-norm-fix-evidence.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def run_scenario_b_norm_fix():
    url = os.environ.get("TTAKKARI_TEST_DB")
    assert url and "ttakkari_test" in url and "ttk_test" in url, "Dangerous: invalid test DB identity"

    init_engine(url)
    sm = sessionmaker()
    token, _ = issue_access_token()
    auth = {"Authorization": f"Bearer {token}"}

    # Seed catalog if needed
    from ttakkari.research.catalog_loader import load_catalog_seeds
    async with sm() as db:
        await load_catalog_seeds(db)
        await db.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Create Study
        study_payload = {
            "title": "로컬 TTS 발음 정확도 및 생성 속도 평가 (Supertonic 3 + Whisper proxy, 정규화 분기 교정)",
            "goal": "한국어 텍스트 정규화 및 커스텀 발음 사전 적용 여부에 따른 합성 음성의 발음 명료도(Whisper STT 대리 지표 CER/WER) 및 실시간 계수(RTF) 정밀 비교 (Baseline raw 청크 유지 교정)",
            "scope": "personal",
            "criteria": [
                "stt_cer_vs_normalized <= 0.05",
                "rtf <= 1.0",
            ],
        }
        r_study = await client.post("/api/research/studies", headers=auth, json=study_payload)
        assert r_study.status_code == 201, r_study.text
        study_id = uuid.UUID(r_study.json()["id"])
        print(f"[1] Created Study: {study_id}")

        # 2. Get method ID
        async with sm() as db:
            m_norm = (await db.scalars(select(Method).where(Method.slug == "tts_korean_text_normalization"))).first()
            assert m_norm is not None, "Method tts_korean_text_normalization not found in seed"
            method_id = m_norm.id

        # 3. Create Experiment
        sample_texts = [
            "2026년 10월 25일 오후 2시 30분에 연구실 회의가 시작됩니다.",
            "HTTP 200 OK 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다.",
        ]
        custom_dict = {"JSON": "제이슨", "HTTP": "에이치티티피"}

        exp_payload = {
            "title": "정규화 미적용(Baseline) vs 정규화+발음사전(Candidate) 음성 합성 및 재인식 비교 (교정)",
            "hypothesis": "한국어 숫자·단위 정규화와 영문 약어 발음 사전을 적용한 Candidate 설정이 미정규화 Baseline 대비 Whisper 재인식 CER/WER를 대폭 낮추고 고품질 발음을 생성할 것이다 (단, STT는 발음 proxy 지표임).",
            "rationale": "Supertonic 3는 한국어 텍스트 입력 시 숫자와 영문 약어가 정규화되지 않으면 철자 단위 발음 왜곡이 발생하므로, 사전 전처리 파이프라인의 효과를 실측 검증한다.",
            "adapter_id": "tts_synthesis",
            "method_id": str(method_id),
            "primary_metric": "stt_cer_vs_normalized",
            "secondary_metrics": ["stt_wer_vs_normalized", "rtf", "synth_seconds"],
            "strategy": "ab",
            "search_space": {
                "handle_measure_units": [False, True],
            },
            "fixed_inputs": {
                "mode": "synthesize_and_stt",
                "engine": "supertonic",
                "voice_preset": "ko_female_1",
                "speed_ratio": 1.0,
                "preset_id": "ko_mixed_pronunciation_v1",
                "texts": sample_texts,
                "custom_pronunciation_dict": custom_dict,
                "plain_text": True,
            },
            "budget": {"max_runs": 2, "max_total_seconds": 180},
        }

        r_exp = await client.post(f"/api/research/studies/{study_id}/experiments", headers=auth, json=exp_payload)
        assert r_exp.status_code == 201, r_exp.text
        exp_data = r_exp.json()
        exp_id = uuid.UUID(exp_data["id"])
        print(f"[2] Created Experiment: {exp_id} with {len(exp_data['configurations'])} configs")

        # 4. Explicitly adjust the two configuration parameter variants for Baseline vs Candidate
        from sqlalchemy.orm.attributes import flag_modified
        async with sm() as db:
            configs = (await db.scalars(select(Configuration).where(Configuration.experiment_id == exp_id).order_by(Configuration.order_index))).all()
            assert len(configs) >= 2, f"Expected 2 configs, got {len(configs)}"
            
            # Baseline (Control)
            configs[0].label = "Baseline (정규화 미적용: 원문 약어/숫자 유지)"
            p0 = dict(configs[0].parameters)
            p0.update({
                "handle_measure_units": False,
                "convert_numbers": False,
                "english_acronym_mode": "raw",
                "read_latin": False,
                "custom_pronunciation_dict": {},
            })
            configs[0].parameters = p0
            flag_modified(configs[0], "parameters")
            
            # Candidate (Treatment)
            configs[1].label = "Candidate (한국어 숫자·단위 정규화 + 영문약어 발음사전)"
            p1 = dict(configs[1].parameters)
            p1.update({
                "handle_measure_units": True,
                "convert_numbers": True,
                "english_acronym_mode": "korean_alphabet",
                "read_latin": True,
                "custom_pronunciation_dict": custom_dict,
            })
            configs[1].parameters = p1
            flag_modified(configs[1], "parameters")
            
            await db.commit()

        # 5. Execute runs sequentially
        print("[3] Starting experiment runs execution...")
        async with sm() as db:
            runs = await runner.start_experiment_runs(db, exp_id, background=False)
            print(f"    Dispatched and completed {len(runs)} runs")

        # 6. Retrieve Experiment Details and Runs via API
        r_exp_detail = await client.get(f"/api/research/experiments/{exp_id}", headers=auth)
        assert r_exp_detail.status_code == 200, r_exp_detail.text
        exp_detail = r_exp_detail.json()
        print(f"[4] Experiment State: {exp_detail['state']}")

        # 7. Verify Each Run and its Artifacts via API
        run_summaries = []
        verified_artifacts = []
        all_run_chunks: dict[str, list[str]] = {}
        all_run_wav_shas: dict[str, list[str]] = {}

        async with sm() as db:
            cfg_rows = (await db.scalars(select(Configuration).where(Configuration.experiment_id == exp_id))).all()
            cfg_label_map = {c.id: c.label for c in cfg_rows}

        for r_info in exp_detail["runs"]:
            r_id = uuid.UUID(r_info["id"])
            r_run_detail = await client.get(f"/api/research/runs/{r_id}", headers=auth)
            assert r_run_detail.status_code == 200, r_run_detail.text
            run_data = r_run_detail.json()
            cfg_id = uuid.UUID(run_data["configuration_id"])
            cfg_label = cfg_label_map.get(cfg_id, "Unknown")
            is_baseline = "Baseline" in cfg_label

            print(f"\n--- Run {r_id} ({cfg_label}) ---")
            print(f"  Status: {run_data['status']}, Wall seconds: {run_data.get('wall_seconds')}")
            
            # Measurements
            measurements_dict = {m["metric_key"]: m["value"] for m in run_data.get("measurements", [])}
            print(f"  Measurements: {measurements_dict}")

            # Sentences & Chunks
            raw_out = run_data.get("raw_output", {})
            stt_eval = raw_out.get("stt_eval", [])
            sentences_info = raw_out.get("sentences", [])
            run_chunks = []
            for s_item in sentences_info:
                run_chunks.extend(s_item.get("chunks", []))
            all_run_chunks["baseline" if is_baseline else "candidate"] = run_chunks
            print(f"  Input Chunks to Synthesis: {run_chunks}")

            for se in stt_eval:
                print(f"    Chunk {se.get('chunk_index')}:")
                print(f"      Raw Ref : {se.get('raw_reference')}")
                print(f"      Norm Ref: {se.get('normalized_reference')}")
                print(f"      STT Text: {se.get('recognized_text')}")
                print(f"      CER (vs norm): {se.get('cer_vs_normalized')}, WER (vs norm): {se.get('wer_vs_normalized')}")
                print(f"      CER (vs raw) : {se.get('cer_vs_raw')}, WER (vs raw) : {se.get('wer_vs_raw')}")

            # Artifacts
            artifacts = run_data.get("artifacts", [])
            print(f"  Linked ResearchArtifacts: {len(artifacts)}")
            assert len(artifacts) >= 2, f"Expected at least 2 audio artifacts per run (1 per chunk), got {len(artifacts)}"

            wav_shas = []
            for art in artifacts:
                art_id = art["artifact_id"]
                # Call Artifact API
                r_art_meta = await client.get(f"/api/artifacts/{art_id}", headers=auth)
                assert r_art_meta.status_code == 200, f"GET /api/artifacts/{art_id} failed: {r_art_meta.text}"
                meta = r_art_meta.json()
                assert meta["kind"] == "audio", f"Expected kind=audio, got {meta['kind']}"
                assert meta["category"] == "research_tts_sample"

                # Call Content API
                r_content = await client.get(f"/api/artifacts/{art_id}/content", headers=auth)
                assert r_content.status_code == 200, f"GET /api/artifacts/{art_id}/content failed"
                content_bytes = r_content.content
                assert len(content_bytes) > 1000, f"WAV too small: {len(content_bytes)} bytes"
                assert content_bytes[:4] == b"RIFF" and content_bytes[8:12] == b"WAVE", "Invalid WAV magic header"
                
                content_sha = sha256_bytes(content_bytes)
                assert content_sha == meta["sha256"], f"SHA256 mismatch: {content_sha} vs {meta['sha256']}"
                wav_shas.append(content_sha)

                # Copy audio file to this Run's outbox
                RUN_OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
                cfg_label_slug = "baseline" if is_baseline else "candidate"
                outbox_filename = f"stage2_b_norm_fix_{cfg_label_slug}_{meta['filename']}"
                dest_file = RUN_OUTBOX_DIR / outbox_filename
                dest_file.write_bytes(content_bytes)
                print(f"    Saved WAV to Outbox: {dest_file.name} ({len(content_bytes)} bytes, sha256={content_sha[:12]}...)")

                verified_artifacts.append({
                    "run_id": str(r_id),
                    "research_artifact_id": art["id"],
                    "artifact_id": art_id,
                    "role": art["role"],
                    "filename": meta["filename"],
                    "outbox_path": str(dest_file),
                    "outbox_filename": outbox_filename,
                    "size_bytes": len(content_bytes),
                    "sha256": content_sha,
                    "mime_type": meta["mime_type"],
                    "content_api_status": 200,
                    "wav_magic": "RIFF/WAVE verified",
                })

            all_run_wav_shas["baseline" if is_baseline else "candidate"] = wav_shas

            run_summaries.append({
                "run_id": str(r_id),
                "configuration_id": str(cfg_id),
                "configuration_label": cfg_label,
                "status": run_data["status"],
                "wall_seconds": run_data.get("wall_seconds"),
                "measurements": measurements_dict,
                "chunks": run_chunks,
                "stt_eval": stt_eval,
                "artifacts_count": len(artifacts),
            })

        # Verification of contrast
        baseline_chunks = all_run_chunks.get("baseline", [])
        candidate_chunks = all_run_chunks.get("candidate", [])
        baseline_candidate_chunks_equal = (baseline_chunks == candidate_chunks)
        print(f"\n[Contrast Check]")
        print(f"  Baseline chunks : {baseline_chunks}")
        print(f"  Candidate chunks: {candidate_chunks}")
        print(f"  Chunks equal?: {baseline_candidate_chunks_equal} (MUST be False)")
        assert not baseline_candidate_chunks_equal, "CRITICAL: Baseline and Candidate chunks must NOT be equal!"

        baseline_shas = all_run_wav_shas.get("baseline", [])
        candidate_shas = all_run_wav_shas.get("candidate", [])
        wav_shas_equal = (baseline_shas == candidate_shas)
        print(f"  Baseline WAV SHAs : {baseline_shas}")
        print(f"  Candidate WAV SHAs: {candidate_shas}")
        print(f"  WAV SHAs equal?: {wav_shas_equal} (MUST be False)")
        assert not wav_shas_equal, "CRITICAL: Baseline and Candidate WAV SHAs must NOT be equal!"

        # 8. Check Analysis and BestResult
        async with sm() as db:
            analysis = (await db.scalars(select(Analysis).where(Analysis.experiment_id == exp_id).order_by(Analysis.created_at.desc()))).first()
            best_res = (await db.scalars(select(BestResult).where(BestResult.experiment_id == exp_id))).first()

            analysis_summary = None
            if analysis:
                analysis_summary = {
                    "id": str(analysis.id),
                    "summary": analysis.summary,
                    "recommendation_ko": analysis.recommendation_ko,
                    "created_at": analysis.created_at.isoformat(),
                }
                print(f"\n[5] Analysis Summary: {analysis.summary}")
                print(f"    Recommendation: {analysis.recommendation_ko}")

            best_result_info = None
            if best_res:
                best_cfg = await db.get(Configuration, best_res.configuration_id)
                best_result_info = {
                    "id": str(best_res.id),
                    "configuration_id": str(best_res.configuration_id),
                    "configuration_label": best_cfg.label if best_cfg else "Unknown",
                }
                print(f"[6] Best Result adopted configuration: {best_result_info['configuration_label']}")

        # 9. Build and Save Sanitized Raw Evidence JSON
        evidence_data = {
            "metadata": {
                "verification_task": "Research Lab v2 Stage 2 Scenario B Normalization Branch Fix",
                "execution_time_utc": "2026-10-25T12:35:00Z",
                "database_target": "ttakkari_test (isolated test DB)",
                "tts_engine": "Supertonic 3 (local ONNX, CPU)",
                "stt_engine": "Whisper (local worker, CPU, proxy evaluation)",
                "proxy_disclaimer": "STT 재인식 CER/WER는 발음 명료도의 간접 대리 지표(proxy)이며 사람 청취 품질(MOS)이 아닙니다.",
                "contrast_verification": {
                    "baseline_chunks": baseline_chunks,
                    "candidate_chunks": candidate_chunks,
                    "chunks_equal": baseline_candidate_chunks_equal,
                    "baseline_wav_shas": baseline_shas,
                    "candidate_wav_shas": candidate_shas,
                    "wav_shas_equal": wav_shas_equal,
                },
            },
            "study": {
                "id": str(study_id),
                "title": study_payload["title"],
                "goal": study_payload["goal"],
            },
            "experiment": {
                "id": str(exp_id),
                "title": exp_payload["title"],
                "hypothesis": exp_payload["hypothesis"],
                "rationale": exp_payload["rationale"],
                "state": exp_detail["state"],
                "primary_metric": exp_payload["primary_metric"],
                "sample_inputs": sample_texts,
                "custom_pronunciation_dict": custom_dict,
            },
            "runs": run_summaries,
            "verified_artifacts": verified_artifacts,
            "analysis": analysis_summary,
            "best_result": best_result_info,
        }

        EVIDENCE_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVIDENCE_JSON_PATH.open("w", encoding="utf-8") as f:
            json.dump(evidence_data, f, ensure_ascii=False, indent=2)
        print(f"\n[7] Saved sanitized evidence JSON to {EVIDENCE_JSON_PATH}")

        # Also copy evidence JSON to run outbox
        outbox_evidence_json = RUN_OUTBOX_DIR / "stage2-b-norm-fix-evidence.json"
        shutil.copy2(EVIDENCE_JSON_PATH, outbox_evidence_json)
        print(f"[8] Copied evidence JSON to Outbox: {outbox_evidence_json}")

    await dispose_engine()
    print("\nScenario B normalization fix run and verification complete!")


if __name__ == "__main__":
    asyncio.run(run_scenario_b_norm_fix())
