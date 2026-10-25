class KnowledgeExtractor:
    def __init__(self, model_name: str = "qwen"):
        self.model_name = model_name

    def extract_assertions(self, text: str) -> list[dict]:
        return []

def main():
    extractor = KnowledgeExtractor()
