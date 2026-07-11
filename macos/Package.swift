// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "InnoNewsSuite",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "InnoAppCore", targets: ["InnoAppCore"]),
        .library(name: "InnoCollectorFeature", targets: ["InnoCollectorFeature"]),
        .executable(name: "InnoCollectorApp", targets: ["InnoCollectorApp"]),
    ],
    targets: [
        .target(name: "InnoAppCore"),
        .target(name: "InnoCollectorFeature", dependencies: ["InnoAppCore"]),
        .executableTarget(
            name: "InnoCollectorApp",
            dependencies: ["InnoCollectorFeature", "InnoAppCore"]
        ),
        .testTarget(name: "InnoAppCoreTests", dependencies: ["InnoAppCore"]),
        .testTarget(
            name: "InnoCollectorAppTests",
            dependencies: ["InnoCollectorFeature", "InnoAppCore"]
        ),
    ]
)
