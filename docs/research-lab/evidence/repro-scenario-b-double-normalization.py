"""DB-free negative control: disabled normalization must reach synthesis unchanged."""

from ttakkari.research.adapters.tts_synthesis import _normalize_with_options
from ttakkari.tts.normalize import chunk_text


def main() -> None:
    text = "HTTP 200 OK 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다."
    synth_inputs = []
    for enabled in (False, True):
        normalized = _normalize_with_options(
            text,
            use_convert_numbers=enabled,
            use_read_latin=enabled,
            custom_dict={"HTTP": "에이치티티피", "JSON": "제이슨"} if enabled else {},
        )
        chunks = chunk_text(normalized, max_chars=220)
        synth_inputs.append(chunks)
        print({"enabled": enabled, "normalized": normalized, "synthesis_chunks": chunks})
    print("baseline_candidate_synthesis_equal=", synth_inputs[0] == synth_inputs[1])
    assert synth_inputs[0] != synth_inputs[1], "normalization-disabled baseline became the candidate"


if __name__ == "__main__":
    main()
