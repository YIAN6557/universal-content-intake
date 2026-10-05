import Foundation
import Translation
import Darwin

private struct Cue: Codable {
    let cueID: String
    let start: Double
    let end: Double
    let sourceText: String
    let sourceLanguage: String
    let targetLanguage: String
    var translatedText: String?

    enum CodingKeys: String, CodingKey {
        case cueID = "cue_id"
        case start
        case end
        case sourceText = "source_text"
        case sourceLanguage = "source_language"
        case targetLanguage = "target_language"
        case translatedText = "translated_text"
    }
}

private struct Request: Codable {
    let schemaVersion: Int
    let sourceLanguage: String
    let targetLanguage: String
    let cues: [Cue]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case sourceLanguage = "source_language"
        case targetLanguage = "target_language"
        case cues
    }
}

private struct Outcome: Codable {
    let schemaVersion: Int
    let status: String
    let runtime: String
    let sourceLanguage: String
    let targetLanguage: String
    let languagePairSupported: Bool
    let languageResourcesInstalled: Bool
    let translationEngine: String
    let error: ErrorPayload?
    let cues: [Cue]?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case status
        case runtime
        case sourceLanguage = "source_language"
        case targetLanguage = "target_language"
        case languagePairSupported = "language_pair_supported"
        case languageResourcesInstalled = "language_resources_installed"
        case translationEngine = "translation_engine"
        case error
        case cues
    }
}

private struct ErrorPayload: Codable {
    let code: String
    let message: String
}

private enum HelperError: Error, LocalizedError {
    case usage(String)
    case input(String)

    var errorDescription: String? {
        switch self {
        case .usage(let message), .input(let message): return message
        }
    }
}

@main
private struct UCITranslationCLI {
    static func main() async {
        guard #available(macOS 26.4, *) else {
            writeError("Apple Translation CLI requires macOS 26.4 or newer.")
            exit(2)
        }
        do {
            try await execute(Array(CommandLine.arguments.dropFirst()))
        } catch {
            writeError(error.localizedDescription)
            exit(2)
        }
    }

    @available(macOS 26.4, *)
    private static func execute(_ arguments: [String]) async throws {
        guard let command = arguments.first else {
            throw HelperError.usage("usage: uci-translation preflight|setup|translate|version [options]")
        }
        if command == "version" {
            print("uci-translation 1.0.0")
            return
        }
        switch command {
        case "preflight":
            let options = try parseOptions(Array(arguments.dropFirst()))
            let source = try required(options, "source")
            let target = try required(options, "target")
            guard target == "zh-Hans" else { throw HelperError.input("target language must be zh-Hans") }
            let outcome = await inspect(source: source, target: target)
            try emit(outcome)
            if outcome.status == "unsupported" { exit(3) }
            if outcome.status == "resource_unavailable" { exit(4) }
        case "setup":
            let options = try parseOptions(Array(arguments.dropFirst()))
            let source = try required(options, "source")
            let target = try required(options, "target")
            guard target == "zh-Hans" else { throw HelperError.input("target language must be zh-Hans") }
            let outcome = await prepare(source: source, target: target)
            try emit(outcome)
            if outcome.status == "unsupported" { exit(3) }
            if outcome.status == "resource_unavailable" { exit(4) }
        case "translate":
            let options = try parseOptions(Array(arguments.dropFirst()))
            let input = URL(fileURLWithPath: try required(options, "input"))
            let output = URL(fileURLWithPath: try required(options, "output"))
            let request = try JSONDecoder().decode(Request.self, from: Data(contentsOf: input))
            let outcome = await translate(request)
            let data = try encode(outcome)
            try data.write(to: output, options: .atomic)
            if outcome.status == "unsupported" { exit(3) }
            if outcome.status == "resource_unavailable" { exit(4) }
        default:
            throw HelperError.usage("unknown translation helper command")
        }
    }

    @available(macOS 26.4, *)
    private static func inspect(source: String, target: String) async -> Outcome {
        let sourceLanguage = Locale.Language(identifier: source)
        let targetLanguage = Locale.Language(identifier: target)
        let availability = LanguageAvailability(preferredStrategy: .lowLatency)
        let status = await availability.status(from: sourceLanguage, to: targetLanguage)
        switch status {
        case .installed:
            return outcome("ready", source, target, supported: true, installed: true)
        case .supported:
            return outcome("resource_unavailable", source, target, supported: true, installed: false,
                           error: ErrorPayload(code: "TRANSLATION_RESOURCE_UNAVAILABLE", message: "Language resources are supported but not installed."))
        case .unsupported:
            return outcome("unsupported", source, target, supported: false, installed: false,
                           error: ErrorPayload(code: "TRANSLATION_UNSUPPORTED", message: "Apple Translation does not support this language pair."))
        @unknown default:
            return outcome("unsupported", source, target, supported: false, installed: false,
                           error: ErrorPayload(code: "TRANSLATION_UNSUPPORTED", message: "Apple Translation returned an unknown availability state."))
        }
    }

    @available(macOS 26.4, *)
    private static func prepare(source: String, target: String) async -> Outcome {
        let checked = await inspect(source: source, target: target)
        guard checked.status == "resource_unavailable" else { return checked }
        do {
            let session = TranslationSession(
                installedSource: Locale.Language(identifier: source),
                target: Locale.Language(identifier: target),
                preferredStrategy: .lowLatency
            )
            try await session.prepareTranslation()
            return await inspect(source: source, target: target)
        } catch {
            return outcome("resource_unavailable", source, target, supported: true, installed: false,
                           error: ErrorPayload(code: "TRANSLATION_RESOURCE_UNAVAILABLE", message: "Language resources were not prepared in the attended setup flow."))
        }
    }

    @available(macOS 26.4, *)
    private static func translate(_ request: Request) async -> Outcome {
        guard request.schemaVersion == 1,
              request.targetLanguage == "zh-Hans",
              !request.sourceLanguage.isEmpty,
              request.cues.allSatisfy({
                  $0.targetLanguage == "zh-Hans" &&
                  $0.sourceLanguage == request.sourceLanguage &&
                  $0.end > $0.start && !$0.cueID.isEmpty && !$0.sourceText.isEmpty
              }) else {
            return outcome("failed", request.sourceLanguage, request.targetLanguage, supported: false, installed: false,
                           error: ErrorPayload(code: "TRANSLATION_PROTOCOL_FAILED", message: "Cue request violated the structured translation contract."))
        }
        let checked = await inspect(source: request.sourceLanguage, target: request.targetLanguage)
        guard checked.status == "ready" else { return checked }
        do {
            let session = TranslationSession(
                installedSource: Locale.Language(identifier: request.sourceLanguage),
                target: Locale.Language(identifier: request.targetLanguage),
                preferredStrategy: .lowLatency
            )
            var translated: [Cue] = []
            translated.reserveCapacity(request.cues.count)
            for cue in request.cues {
                let response = try await session.translate(cue.sourceText)
                translated.append(Cue(
                    cueID: cue.cueID,
                    start: cue.start,
                    end: cue.end,
                    sourceText: cue.sourceText,
                    sourceLanguage: cue.sourceLanguage,
                    targetLanguage: cue.targetLanguage,
                    translatedText: response.targetText
                ))
            }
            return outcome("success", request.sourceLanguage, request.targetLanguage, supported: true, installed: true, cues: translated)
        } catch {
            return outcome("failed", request.sourceLanguage, request.targetLanguage, supported: true, installed: true,
                           error: ErrorPayload(code: "TRANSLATION_FAILED", message: "Apple Translation could not translate the cue batch."))
        }
    }

    private static func outcome(
        _ status: String,
        _ source: String,
        _ target: String,
        supported: Bool,
        installed: Bool,
        error: ErrorPayload? = nil,
        cues: [Cue]? = nil
    ) -> Outcome {
        Outcome(
            schemaVersion: 1,
            status: status,
            runtime: "Apple Translation CLI (no UI session)",
            sourceLanguage: source,
            targetLanguage: target,
            languagePairSupported: supported,
            languageResourcesInstalled: installed,
            translationEngine: "Apple Translation / lowLatency",
            error: error,
            cues: cues
        )
    }

    private static func parseOptions(_ arguments: [String]) throws -> [String: String] {
        var options: [String: String] = [:]
        var index = 0
        while index < arguments.count {
            let key = arguments[index]
            guard key.hasPrefix("--"), index + 1 < arguments.count else {
                throw HelperError.usage("translation helper options require --name value pairs")
            }
            options[String(key.dropFirst(2))] = arguments[index + 1]
            index += 2
        }
        return options
    }

    private static func required(_ options: [String: String], _ key: String) throws -> String {
        guard let value = options[key], !value.isEmpty else {
            throw HelperError.usage("missing required --\(key) value")
        }
        return value
    }

    private static func encode(_ value: Outcome) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(value)
    }

    private static func emit(_ value: Outcome) throws {
        FileHandle.standardOutput.write(try encode(value))
        FileHandle.standardOutput.write(Data([0x0a]))
    }

    private static func writeError(_ message: String) {
        let safe = message.replacingOccurrences(of: "\n", with: " ")
        FileHandle.standardError.write(Data((safe + "\n").utf8))
    }
}
