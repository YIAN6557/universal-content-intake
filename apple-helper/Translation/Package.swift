// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "UCITranslation",
    platforms: [.macOS(.v15)],
    products: [
        .executable(name: "uci-translation", targets: ["UCITranslation"]),
    ],
    targets: [
        .executableTarget(name: "UCITranslation"),
    ]
)
