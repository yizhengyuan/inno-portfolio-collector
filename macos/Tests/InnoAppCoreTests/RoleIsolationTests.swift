import Foundation
import Testing
@testable import InnoAppCore

@Suite("Packaged role isolation")
struct RoleIsolationTests {
    @Test(
        "real helper roles are distinct and reader bundle excludes collector material",
        .enabled(
            if: ProcessInfo.processInfo.environment["INNO_COLLECTOR_HELPER"] != nil
                && ProcessInfo.processInfo.environment["INNO_READER_HELPER"] != nil,
            "requires INNO_COLLECTOR_HELPER and INNO_READER_HELPER"
        )
    )
    func packagedHelpers() async throws {
        let environment = ProcessInfo.processInfo.environment
        let collectorPath = try #require(environment["INNO_COLLECTOR_HELPER"])
        let readerPath = try #require(environment["INNO_READER_HELPER"])
        let collectorURL = URL(fileURLWithPath: collectorPath).standardizedFileURL
        let readerURL = URL(fileURLWithPath: readerPath).standardizedFileURL
        let collector = HelperClient(executable: collectorURL, timeout: 60)
        let reader = HelperClient(executable: readerURL, timeout: 60)

        async let collectorStatus = collector.call(command: "status", arguments: [:])
        async let readerStatus = reader.call(command: "status", arguments: [:])
        let (collectorResult, readerResult) = try await (collectorStatus, readerStatus)
        #expect(collectorResult["role"] == .string("collector"))
        #expect(readerResult["role"] == .string("reader"))

        let root = bundleRoot(for: readerURL)
        #expect(try forbiddenFiles(in: root).isEmpty)
    }

    private func forbiddenFiles(in root: URL) throws -> [String] {
        let forbidden = Set([
            "wechat_exporter.py", "collector_helper", "innocollectorhelper",
            "cookies.sqlite", "projects.json",
        ])
        let enumerator = try #require(FileManager.default.enumerator(
            at: root,
            includingPropertiesForKeys: [.isRegularFileKey],
            options: [.skipsHiddenFiles]
        ))
        var found: [String] = []
        for case let url as URL in enumerator {
            if forbidden.contains(url.lastPathComponent.lowercased()) {
                found.append(url.lastPathComponent)
            }
        }
        return found
    }

    private func bundleRoot(for helper: URL) -> URL {
        let plugins = helper.deletingLastPathComponent()
        if plugins.lastPathComponent == "PlugIns",
           plugins.deletingLastPathComponent().lastPathComponent == "Contents" {
            return plugins.deletingLastPathComponent().deletingLastPathComponent()
        }
        return plugins
    }
}
