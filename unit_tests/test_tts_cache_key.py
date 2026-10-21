from ttakkari.tts.cache import KEY_VERSION, chunk_key


def test_chunk_key_distinguishes_speeds_used_by_synthesis():
    assert KEY_VERSION == "tts2"
    shared = {
        "engine": "supertonic",
        "model_revision": "supertonic3-v1.7.3-s8",
        "voice": "M1",
        "lang": "ko",
        "text": "안녕하세요.",
    }

    assert chunk_key(**shared, speed=1.0001) != chunk_key(**shared, speed=1.0004)
