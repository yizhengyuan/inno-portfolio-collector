// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "InnoNewsSuite",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "InnoAppCore", targets: ["InnoAppCore"]),
    ],
    targets: [
        .target(name: "InnoAppCore"),
        .testTarget(name: "InnoAppCoreTests", dependencies: ["InnoAppCore"]),
    ]
)
