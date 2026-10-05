// Minimal pdfinfo-compatible reader built on macOS PDFKit.
// Prints "Pages:", "Title:" and "Encrypted:" lines in poppler's format so the
// DOCUMENT Provider can validate PDFs without an external poppler install.
import Foundation
import PDFKit

let arguments = CommandLine.arguments
guard arguments.count == 2 else {
    FileHandle.standardError.write("usage: uci-pdfinfo <file.pdf>\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: arguments[1])
guard let document = PDFDocument(url: url) else {
    FileHandle.standardError.write("uci-pdfinfo: not a readable PDF\n".data(using: .utf8)!)
    exit(1)
}
let title = (document.documentAttributes?[PDFDocumentAttribute.titleAttribute] as? String) ?? ""
print("Title:          \(title.replacingOccurrences(of: "\n", with: " "))")
print("Encrypted:      \(document.isEncrypted ? "yes" : "no")")
print("Pages:          \(document.pageCount)")
exit(document.pageCount > 0 ? 0 : 1)
