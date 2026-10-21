# ruff: noqa
import json, urllib.request
for d in ["miracl/miracl", "apple/mkqa", "klue/klue", "KorQuAD/squad_kor_v1", "akariasai/xor_tydi_qa",
          "castorini/mr-tydi", "BeIR/scifact", "google-research-datasets/natural_questions", "facebook/belebele"]:
    try:
        j = json.load(urllib.request.urlopen(f"https://huggingface.co/api/datasets/{d}", timeout=30))
        tags = j.get("tags", [])
        print(d, j.get("cardData", {}).get("license"), [t for t in tags if t.startswith("license")][:3], "ko" if "language:ko" in tags else "")
    except Exception as e:
        print(d, "ERR", type(e).__name__, e)
