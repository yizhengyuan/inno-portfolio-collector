import Combine
import Foundation
import InnoAppCore

public struct CollectorSummary: Equatable, Sendable {
    public let articleCount: Int
    public let projectCount: Int
    public let failedProjects: Int

    public init(articleCount: Int, projectCount: Int, failedProjects: Int) {
        self.articleCount = articleCount
        self.projectCount = projectCount
        self.failedProjects = failedProjects
    }
}

@MainActor
public final class CollectorViewModel: ObservableObject {
    @Published public private(set) var summary: CollectorSummary?
    @Published public private(set) var isBusy = false
    @Published public private(set) var lastPreflightSucceeded = false
    @Published public private(set) var errorMessage: String?

    private let helper: any HelperCalling
    public let locations: AppLocations

    public init(helper: any HelperCalling, locations: AppLocations) {
        self.helper = helper
        self.locations = locations
    }

    public func refresh() async {
        await perform {
            let result = try await helper.call(
                command: "status",
                arguments: ["vault": .string(locations.vault.path)]
            )
            if case .object(let report) = result["report"] {
                summary = Self.summary(from: report)
            } else if result["vault_exists"] == .boolean(false) {
                summary = CollectorSummary(articleCount: 0, projectCount: 0, failedProjects: 0)
            }
        }
    }

    public func preflight() async {
        lastPreflightSucceeded = false
        await perform {
            let result = try await helper.call(
                command: "collect",
                arguments: collectionArguments(dryRun: true)
            )
            let mapped = Self.summary(from: result)
            summary = mapped
            lastPreflightSucceeded = mapped.failedProjects == 0
            if !lastPreflightSucceeded {
                errorMessage = "采集预检存在失败项目，请先查看详情。"
            }
        }
    }

    public func collect() async {
        guard lastPreflightSucceeded else {
            errorMessage = "请先完成成功的采集预检。"
            return
        }
        await perform {
            let result = try await helper.call(
                command: "collect",
                arguments: collectionArguments(dryRun: false)
            )
            summary = Self.summary(from: result)
            lastPreflightSucceeded = false
        }
    }

    public func buildUpdate(destination: URL, basePackage: URL?) async {
        await perform {
            var arguments: [String: JSONValue] = [
                "vault": .string(locations.vault.path),
                "output": .string(destination.path),
            ]
            if let basePackage {
                arguments["base_package"] = .string(basePackage.path)
            }
            _ = try await helper.call(command: "build_update", arguments: arguments)
        }
    }

    public func receiveDrafts(package: URL) async {
        await perform {
            _ = try await helper.call(
                command: "receive_drafts",
                arguments: [
                    "package": .string(package.path),
                    "inbox": .string(locations.inbox.path),
                ]
            )
        }
    }

    private func collectionArguments(dryRun: Bool) -> [String: JSONValue] {
        var arguments: [String: JSONValue] = [
            "runtime": .string(locations.supportRoot.appendingPathComponent("Runtime").path),
            "exporter_runtime": .string(locations.supportRoot.appendingPathComponent("ExporterRuntime").path),
            "since": .string("2026-01-01"),
            "dry_run": .boolean(dryRun),
        ]
        if let projectsConfig = locations.projectsConfig {
            arguments["projects"] = .string(projectsConfig.path)
        }
        return arguments
    }

    private func perform(_ operation: () async throws -> Void) async {
        guard !isBusy else { return }
        isBusy = true
        errorMessage = nil
        defer { isBusy = false }
        do {
            try await operation()
        } catch is CancellationError {
            errorMessage = nil
        } catch let error as HelperClientError {
            switch error {
            case .helperFailure(let message): errorMessage = message
            case .timedOut: errorMessage = "本地助手响应超时。"
            case .outputTooLarge: errorMessage = "本地助手输出超过安全上限。"
            case .launchFailed: errorMessage = "本地助手不可用。"
            case .invalidResponse, .responseIDMismatch: errorMessage = "本地助手返回了无效响应。"
            }
        } catch {
            errorMessage = "本地操作失败。"
        }
    }

    private static func summary(from values: [String: JSONValue]) -> CollectorSummary {
        func integer(_ key: String) -> Int {
            if case .integer(let value) = values[key] { return value }
            return 0
        }
        return CollectorSummary(
            articleCount: integer("article_count"),
            projectCount: integer("project_count"),
            failedProjects: integer("failed_projects")
        )
    }
}
