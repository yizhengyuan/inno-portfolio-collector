import Foundation
import Testing
@testable import InnoAppCore

@Suite("Application locations")
struct FileLocationsTests {
    private let applicationSupport = URL(fileURLWithPath: "/Users/test/Library/Application Support", isDirectory: true)

    @Test("collector and reader use separate product roots")
    func rolesAreSeparated() throws {
        let collector = try AppLocations.resolve(
            role: .collector,
            applicationSupport: applicationSupport,
            bundleURL: URL(fileURLWithPath: "/Applications/InnoCollector.app", isDirectory: true)
        )
        let reader = try AppLocations.resolve(
            role: .reader,
            applicationSupport: applicationSupport,
            bundleURL: URL(fileURLWithPath: "/Applications/InnoReader.app", isDirectory: true)
        )

        #expect(collector.supportRoot != reader.supportRoot)
        #expect(collector.supportRoot.path.hasSuffix("com.inno.news.collector"))
        #expect(reader.supportRoot.path.hasSuffix("com.inno.news.reader"))
        #expect(collector.helper.lastPathComponent == "InnoCollectorHelper")
        #expect(reader.helper.lastPathComponent == "InnoReaderHelper")
        #expect(collector.projectsConfig?.path.hasSuffix("Contents/Resources/config/projects.json") == true)
        #expect(reader.projectsConfig == nil)
        #expect(
            collector.mooreHelper?.path.hasSuffix(
                "Contents/PlugIns/MooreExporterHelper"
            ) == true
        )
        #expect(
            collector.exporterRuntime
                == collector.supportRoot.appendingPathComponent(
                    "ExporterRuntime",
                    isDirectory: true
                )
        )
        #expect(reader.mooreHelper == nil)
        #expect(reader.exporterRuntime == nil)
        #expect(
            collector.vault
                == collector.supportRoot
                    .appendingPathComponent("Runtime/vault/英诺被投项目资讯库", isDirectory: true)
        )
        #expect(
            reader.vault
                == reader.supportRoot.appendingPathComponent("英诺被投项目资讯库", isDirectory: true)
        )
    }

    @Test("all writable paths stay inside Application Support")
    func writablePathsAreConfined() throws {
        for role in [AppRole.collector, .reader] {
            let locations = try AppLocations.resolve(
                role: role,
                applicationSupport: applicationSupport,
                bundleURL: URL(fileURLWithPath: "/Applications/Test.app", isDirectory: true)
            )
            for url in [locations.supportRoot, locations.vault, locations.inbox] {
                #expect(url.standardizedFileURL.path.hasPrefix(applicationSupport.path + "/"))
                #expect(!url.path.contains("/.moore/"))
                #expect(!url.path.contains("/Desktop/"))
                #expect(!url.path.contains("/Downloads/"))
            }
        }
    }

    @Test("helpers stay inside bundle PlugIns")
    func helpersStayInsideBundle() throws {
        let bundle = URL(fileURLWithPath: "/Applications/Test.app", isDirectory: true)
        let locations = try AppLocations.resolve(
            role: .reader,
            applicationSupport: applicationSupport,
            bundleURL: bundle
        )
        let plugins = bundle.appendingPathComponent("Contents/PlugIns", isDirectory: true).standardizedFileURL
        let collector = try AppLocations.resolve(
            role: .collector,
            applicationSupport: applicationSupport,
            bundleURL: bundle
        )

        #expect(locations.helper.deletingLastPathComponent() == plugins)
        #expect(collector.mooreHelper?.deletingLastPathComponent() == plugins)
    }
}
