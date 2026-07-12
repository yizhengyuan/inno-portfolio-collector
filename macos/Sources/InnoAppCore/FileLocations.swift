import Foundation

public enum AppRole: String, Sendable {
    case collector
    case reader

    var bundleIdentifier: String {
        switch self {
        case .collector: "com.inno.news.collector"
        case .reader: "com.inno.news.reader"
        }
    }

    var helperName: String {
        switch self {
        case .collector: "InnoCollectorHelper"
        case .reader: "InnoReaderHelper"
        }
    }
}

public enum AppLocationsError: Error, Equatable, Sendable {
    case applicationSupportUnavailable
    case unsafeBundlePath
}

public struct AppLocations: Equatable, Sendable {
    public let supportRoot: URL
    public let vault: URL
    public let inbox: URL
    public let helper: URL
    public let projectsConfig: URL?
    public let mooreHelper: URL?
    public let collectorWebServer: URL?
    public let exporterRuntime: URL?

    public static func resolve(
        role: AppRole,
        applicationSupport: URL,
        bundleURL: URL
    ) throws -> Self {
        let supportRoot = applicationSupport
            .appendingPathComponent(role.bundleIdentifier, isDirectory: true)
            .standardizedFileURL
        let plugins = bundleURL
            .appendingPathComponent("Contents/PlugIns", isDirectory: true)
            .standardizedFileURL
        let helper = plugins
            .appendingPathComponent(role.helperName, isDirectory: false)
            .standardizedFileURL
        let mooreHelper = plugins
            .appendingPathComponent("MooreExporterHelper", isDirectory: false)
            .standardizedFileURL
        let collectorWebServer = plugins
            .appendingPathComponent("InnoCollectorWebServer", isDirectory: false)
            .standardizedFileURL
        guard helper.deletingLastPathComponent() == plugins,
              collectorWebServer.deletingLastPathComponent() == plugins else {
            throw AppLocationsError.unsafeBundlePath
        }
        let resources = bundleURL
            .appendingPathComponent("Contents/Resources", isDirectory: true)
            .standardizedFileURL
        let vault = role == .collector
            ? supportRoot.appendingPathComponent(
                "Runtime/vault/英诺被投项目资讯库",
                isDirectory: true
            )
            : supportRoot.appendingPathComponent("英诺被投项目资讯库", isDirectory: true)
        return Self(
            supportRoot: supportRoot,
            vault: vault,
            inbox: supportRoot.appendingPathComponent("DraftInbox", isDirectory: true),
            helper: helper,
            projectsConfig: role == .collector
                ? resources.appendingPathComponent("config/projects.json", isDirectory: false)
                : nil,
            mooreHelper: role == .collector ? mooreHelper : nil,
            collectorWebServer: role == .collector ? collectorWebServer : nil,
            exporterRuntime: role == .collector
                ? supportRoot.appendingPathComponent("ExporterRuntime", isDirectory: true)
                : nil
        )
    }

    public static func collector(
        fileManager: FileManager = .default,
        bundle: Bundle = .main
    ) throws -> Self {
        try live(role: .collector, fileManager: fileManager, bundle: bundle)
    }

    public static func reader(
        fileManager: FileManager = .default,
        bundle: Bundle = .main
    ) throws -> Self {
        try live(role: .reader, fileManager: fileManager, bundle: bundle)
    }

    private static func live(
        role: AppRole,
        fileManager: FileManager,
        bundle: Bundle
    ) throws -> Self {
        guard let applicationSupport = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first else {
            throw AppLocationsError.applicationSupportUnavailable
        }
        return try resolve(
            role: role,
            applicationSupport: applicationSupport,
            bundleURL: bundle.bundleURL
        )
    }
}
