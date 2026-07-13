// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "InnoNewsSuite",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "InnoAppCore", targets: ["InnoAppCore"]),
        .library(name: "InnoCollectorFeature", targets: ["InnoCollectorFeature"]),
        .executable(name: "InnoCollectorApp", targets: ["InnoCollectorApp"]),
        .library(name: "InnoReaderFeature", targets: ["InnoReaderFeature"]),
        .executable(name: "InnoReaderApp", targets: ["InnoReaderApp"]),
    ],
    targets: [
        .target(name: "InnoAppCore"),
        .target(name: "InnoCollectorFeature"),
        .executableTarget(
            name: "InnoCollectorApp",
            dependencies: ["InnoCollectorFeature", "InnoAppCore"]
        ),
        .target(name: "InnoReaderFeature", dependencies: ["InnoAppCore"]),
        .executableTarget(
            name: "InnoReaderApp",
            dependencies: ["InnoReaderFeature", "InnoAppCore"]
        ),
        .testTarget(name: "InnoAppCoreTests", dependencies: ["InnoAppCore"]),
        .testTarget(
            name: "InnoCollectorAppTests",
            dependencies: ["InnoCollectorFeature"]
        ),
        .testTarget(
            name: "InnoReaderAppTests",
            dependencies: ["InnoReaderFeature", "InnoAppCore"]
        ),
    ]
)
