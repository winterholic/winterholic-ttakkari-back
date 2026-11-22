// Apple Vision OCR over PNG files. Usage: ocr_lines <png>...  Output: one JSON object per file.
// Boxes are normalized with the origin at the bottom-left (Vision convention); the caller flips y.
import Foundation
import Vision
import ImageIO

func lines(_ path: String) -> [String: Any] {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
        return ["path": path, "error": "cannot decode image"]
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["ko-KR", "en-US"]
    request.usesLanguageCorrection = false
    do { try VNImageRequestHandler(cgImage: image).perform([request]) }
    catch { return ["path": path, "error": "\(error)"] }
    let rows: [[String: Any]] = (request.results ?? []).compactMap { obs in
        guard let top = obs.topCandidates(1).first else { return nil }
        let b = obs.boundingBox
        return ["text": top.string, "confidence": Double(top.confidence),
                "x": Double(b.minX), "y": Double(b.minY), "w": Double(b.width), "h": Double(b.height)]
    }
    return ["path": path, "width": image.width, "height": image.height, "lines": rows]
}

for path in CommandLine.arguments.dropFirst() {
    let data = try JSONSerialization.data(withJSONObject: lines(path), options: [])
    print(String(data: data, encoding: .utf8)!)
}
