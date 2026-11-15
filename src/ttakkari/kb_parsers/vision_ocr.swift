// PDF 쪽을 렌더해 Apple Vision 으로 글자를 읽는다. 입력: PDF 경로, 최대 쪽수. 출력: 쪽마다 JSON 한 줄.
import Foundation
import PDFKit
import Vision

guard CommandLine.arguments.count >= 3, let maxPages = Int(CommandLine.arguments[2]) else {
    FileHandle.standardError.write(Data("usage: vision_ocr <pdf> <max_pages>\n".utf8))
    exit(2)
}
guard let doc = PDFDocument(url: URL(fileURLWithPath: CommandLine.arguments[1])) else { exit(3) }

func emit(_ obj: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: obj), let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

let scale: CGFloat = 2.5  // 72dpi 기준 약 180dpi. 작은 글자 인식과 속도의 절충.
for index in 0..<min(doc.pageCount, maxPages) {
    autoreleasepool {
        guard let page = doc.page(at: index) else { return }
        let bounds = page.bounds(for: .mediaBox)
        let size = CGSize(width: bounds.width * scale, height: bounds.height * scale)
        let image = page.thumbnail(of: size, for: .mediaBox)
        guard let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            emit(["page": index + 1, "error": "render_failed"])
            return
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["ko-KR", "en-US"]
        request.usesLanguageCorrection = true
        do {
            try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
        } catch {
            emit(["page": index + 1, "error": "ocr_failed"])
            return
        }
        // 위에서 아래, 왼쪽에서 오른쪽 순서로 줄을 모은다(Vision 좌표는 아래가 0).
        let lines = (request.results ?? []).compactMap { obs -> (CGRect, String, Float)? in
            guard let top = obs.topCandidates(1).first else { return nil }
            return (obs.boundingBox, top.string, top.confidence)
        }.sorted { a, b in
            abs(a.0.midY - b.0.midY) > 0.01 ? a.0.midY > b.0.midY : a.0.minX < b.0.minX
        }
        let text = lines.map { $0.1 }.joined(separator: "\n")
        let conf = lines.isEmpty ? 0.0 : Double(lines.map { $0.2 }.reduce(0, +)) / Double(lines.count)
        emit(["page": index + 1, "text": text, "confidence": conf, "lines": lines.count])
    }
}
