import Foundation
import Testing
@testable import InnoAppCore

@Suite("Packaged role isolation")
struct RoleIsolationTests {
    @Test(
        "packaged apps contain only their final role-specific executables",
        .enabled(
            if: ProcessInfo.processInfo.environment["INNO_COLLECTOR_APP"] != nil
                && ProcessInfo.processInfo.environment["INNO_READER_APP"] != nil,
            "requires INNO_COLLECTOR_APP and INNO_READER_APP"
        )
    )
    func packagedApps() throws {
        let environment = ProcessInfo.processInfo.environment
        let collectorPath = try #require(environment["INNO_COLLECTOR_APP"])
        let readerPath = try #require(environment["INNO_READER_APP"])
        let collector = URL(fileURLWithPath: collectorPath, isDirectory: true)
            .standardizedFileURL
        let reader = URL(fileURLWithPath: readerPath, isDirectory: true)
            .standardizedFileURL

        #expect(try entryNames(in: collector, at: "Contents/MacOS") == [
            "InnoCollectorApp",
        ])
        #expect(try entryNames(in: collector, at: "Contents/PlugIns") == [
            "InnoCollectorWebServer",
        ])
        #expect(try regularFileExists(in: collector, at: "Contents/Resources/config/projects.json"))

        #expect(try entryNames(in: reader, at: "Contents/MacOS") == [
            "InnoReaderApp",
        ])
        #expect(try entryNames(in: reader, at: "Contents/PlugIns") == [
            "InnoReaderHelper",
        ])
        #expect(!FileManager.default.fileExists(
            atPath: reader.appendingPathComponent(
                "Contents/Resources/config/projects.json",
                isDirectory: false
            ).path
        ))

        let forbidden = Set([
            "innocollectorhelper",
            "mooreexporterhelper",
            "collectorcontentview",
            "collectorviewmodel",
            "moorelocalloginserver",
        ])
        #expect(try forbiddenFiles(in: collector, names: forbidden).isEmpty)
        #expect(try forbiddenFiles(in: reader, names: forbidden).isEmpty)
    }

    private func entryNames(in bundle: URL, at relativePath: String) throws -> Set<String> {
        let directory = bundle.appendingPathComponent(relativePath, isDirectory: true)
        let values = try directory.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard values.isDirectory == true, values.isSymbolicLink != true else {
            return []
        }
        return Set(try FileManager.default.contentsOfDirectory(atPath: directory.path))
    }

    private func regularFileExists(in bundle: URL, at relativePath: String) throws -> Bool {
        let file = bundle.appendingPathComponent(relativePath, isDirectory: false)
        let values = try file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
        return values.isRegularFile == true && values.isSymbolicLink != true
    }

    private func forbiddenFiles(in root: URL, names: Set<String>) throws -> [String] {
        let enumerator = try #require(FileManager.default.enumerator(
            at: root,
            includingPropertiesForKeys: [.isRegularFileKey],
            options: [.skipsHiddenFiles]
        ))
        var found: [String] = []
        for case let url as URL in enumerator {
            let stem = url.deletingPathExtension().lastPathComponent.lowercased()
            if names.contains(stem) {
                found.append(url.lastPathComponent)
            }
        }
        return found.sorted()
    }
}
