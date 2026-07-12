import AppKit
import Darwin
import Foundation

@MainActor
public protocol LocalLoginServing: AnyObject {
    func open() async throws
    func stop()
}

public enum LocalLoginError: Error, Equatable, Sendable {
    case unavailable
    case portInUse
    case launchFailed
    case notReady
    case browserUnavailable
}

@MainActor
protocol LocalLoginProcessControlling: AnyObject, Sendable {
    var isRunning: Bool { get }
    func start(executable: URL, arguments: [String]) throws
    func stop()
    nonisolated func emergencyStop()
}

extension LocalLoginProcessControlling {
    nonisolated func emergencyStop() {}
}

@MainActor
final class FoundationLoginProcess: LocalLoginProcessControlling {
    private nonisolated static let gracefulWait = 0.5
    private nonisolated static let forcedWait = 0.5

    // All regular access is MainActor-isolated. The unsafe annotation is only
    // used by the synchronous deinit fallback, which must work when the main
    // actor can no longer schedule cleanup (for example during app teardown).
    private nonisolated(unsafe) var process: Process?

    var isRunning: Bool {
        process?.isRunning == true
    }

    func start(executable: URL, arguments: [String]) throws {
        stop()

        let next = Process()
        next.executableURL = executable
        next.arguments = arguments
        next.standardInput = FileHandle.nullDevice
        next.standardOutput = FileHandle.nullDevice
        next.standardError = FileHandle.nullDevice
        try next.run()
        process = next
    }

    func stop() {
        guard let process else {
            return
        }
        terminate(process)
        self.process = nil
    }

    deinit {
        emergencyStop()
    }

    nonisolated func emergencyStop() {
        guard let process else {
            return
        }
        terminate(process)
        self.process = nil
    }

    private nonisolated func terminate(_ process: Process) {
        guard process.isRunning else {
            return
        }

        process.terminate()
        if waitForExit(process, timeout: Self.gracefulWait) {
            return
        }

        _ = Darwin.kill(process.processIdentifier, SIGKILL)
        _ = waitForExit(process, timeout: Self.forcedWait)
    }

    private nonisolated func waitForExit(_ process: Process, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning, Date() < deadline {
            Thread.sleep(forTimeInterval: 0.01)
        }
        return !process.isRunning
    }
}

@MainActor
public final class MooreLocalLoginServer: LocalLoginServing {
    public typealias PortOccupancyProbe = @MainActor () -> Bool
    public typealias PageProbe = @MainActor (URL) async -> Bool
    public typealias Sleeper = @MainActor (Duration) async throws -> Void
    public typealias BrowserOpener = @MainActor (URL) -> Bool

    private static let helperName = "MooreExporterHelper"
    private static let runtimeName = "ExporterRuntime"
    // The PyInstaller helper can need roughly ten seconds on its first launch
    // from /Applications. Keep the wait bounded, but leave enough cold-start
    // headroom before treating a healthy helper as failed.
    private static let readinessAttempts = 200
    private static let readinessDelay = Duration.milliseconds(100)
    private static let serverPort: UInt16 = 18_765

    private let executable: URL
    private let pluginsDirectory: URL
    private let runtimeDirectory: URL
    private let supportRoot: URL
    private let serverURL = URL(string: "http://127.0.0.1:18765/")!
    private let process: any LocalLoginProcessControlling
    private let portOccupancyProbe: PortOccupancyProbe
    private let pageProbe: PageProbe
    private let sleeper: Sleeper
    private let browserOpener: BrowserOpener

    // The task covers launch and readiness only. Every caller waits for the same
    // task and then opens the already verified page, so reentrant actor calls
    // cannot start a second helper.
    private var startupTask: Task<Void, any Error>?

    public convenience init(
        executable: URL,
        pluginsDirectory: URL,
        runtimeDirectory: URL,
        supportRoot: URL
    ) {
        self.init(
            executable: executable,
            pluginsDirectory: pluginsDirectory,
            runtimeDirectory: runtimeDirectory,
            supportRoot: supportRoot,
            process: FoundationLoginProcess(),
            portOccupancyProbe: Self.defaultPortOccupancyProbe,
            pageProbe: Self.defaultProbe,
            sleeper: { try await Task.sleep(for: $0) },
            browserOpener: { NSWorkspace.shared.open($0) }
        )
    }

    init(
        executable: URL,
        pluginsDirectory: URL,
        runtimeDirectory: URL,
        supportRoot: URL,
        process: any LocalLoginProcessControlling,
        portOccupancyProbe: @escaping PortOccupancyProbe = { false },
        pageProbe: @escaping PageProbe,
        sleeper: @escaping Sleeper = { _ in },
        browserOpener: @escaping BrowserOpener
    ) {
        self.executable = executable.standardizedFileURL
        self.pluginsDirectory = pluginsDirectory.standardizedFileURL
        self.runtimeDirectory = runtimeDirectory.standardizedFileURL
        self.supportRoot = supportRoot.standardizedFileURL
        self.process = process
        self.portOccupancyProbe = portOccupancyProbe
        self.pageProbe = pageProbe
        self.sleeper = sleeper
        self.browserOpener = browserOpener
    }

    deinit {
        startupTask?.cancel()
        process.emergencyStop()
    }

    public func open() async throws {
        try Task.checkCancellation()
        try validateBoundaries()

        if process.isRunning, startupTask == nil {
            try await openRunningServer()
            return
        }

        let task: Task<Void, any Error>
        if let startupTask {
            task = startupTask
        } else {
            guard !portOccupancyProbe() else {
                throw LocalLoginError.portInUse
            }
            let newTask = Task { @MainActor [self] in
                try await startAndWaitUntilReady()
            }
            startupTask = newTask
            task = newTask
        }

        do {
            try await withTaskCancellationHandler {
                try await task.value
            } onCancel: {
                task.cancel()
            }
            startupTask = nil

            try Task.checkCancellation()
            guard process.isRunning else {
                throw LocalLoginError.launchFailed
            }
            guard browserOpener(serverURL) else {
                throw LocalLoginError.browserUnavailable
            }
        } catch is CancellationError {
            startupTask = nil
            task.cancel()
            if process.isRunning {
                process.stop()
            }
            throw CancellationError()
        } catch {
            startupTask = nil
            throw error
        }
    }

    public func stop() {
        startupTask?.cancel()
        startupTask = nil
        process.stop()
    }

    private func startAndWaitUntilReady() async throws {
        do {
            try Task.checkCancellation()
            do {
                try process.start(
                    executable: executable,
                    arguments: [
                        "--runtime-dir", runtimeDirectory.path,
                        "exporter-server-start",
                        "--host", "127.0.0.1",
                        "--port", "18765",
                        "--no-open",
                    ]
                )
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                throw portOccupancyProbe()
                    ? LocalLoginError.portInUse
                    : LocalLoginError.launchFailed
            }

            guard process.isRunning else {
                process.stop()
                throw portOccupancyProbe()
                    ? LocalLoginError.portInUse
                    : LocalLoginError.launchFailed
            }

            switch try await waitUntilReady() {
            case .ready:
                return
            case .exited:
                process.stop()
                throw portOccupancyProbe()
                    ? LocalLoginError.portInUse
                    : LocalLoginError.launchFailed
            case .timedOut:
                process.stop()
                throw LocalLoginError.notReady
            }
        } catch is CancellationError {
            process.stop()
            throw CancellationError()
        }
    }

    private func openRunningServer() async throws {
        try Task.checkCancellation()
        guard await pageProbe(serverURL) else {
            throw LocalLoginError.notReady
        }
        try Task.checkCancellation()
        guard browserOpener(serverURL) else {
            throw LocalLoginError.browserUnavailable
        }
    }

    private func validateBoundaries() throws {
        let helperValues = try? executable.resourceValues(forKeys: [
            .isRegularFileKey,
            .isSymbolicLinkKey,
        ])
        let canonicalHelper = executable.resolvingSymlinksInPath().standardizedFileURL
        let canonicalPlugins = pluginsDirectory.resolvingSymlinksInPath().standardizedFileURL
        let canonicalRuntime = runtimeDirectory.resolvingSymlinksInPath().standardizedFileURL
        let canonicalSupport = supportRoot.resolvingSymlinksInPath().standardizedFileURL

        guard
            executable.lastPathComponent == Self.helperName,
            executable.deletingLastPathComponent() == pluginsDirectory,
            canonicalHelper.deletingLastPathComponent().path == canonicalPlugins.path,
            helperValues?.isRegularFile == true,
            helperValues?.isSymbolicLink != true,
            FileManager.default.isExecutableFile(atPath: executable.path),
            !isSymbolicLinkIfPresent(pluginsDirectory),
            runtimeDirectory.lastPathComponent == Self.runtimeName,
            runtimeDirectory.deletingLastPathComponent() == supportRoot,
            canonicalRuntime.deletingLastPathComponent().path == canonicalSupport.path,
            !isSymbolicLinkIfPresent(supportRoot),
            !isSymbolicLinkIfPresent(runtimeDirectory)
        else {
            throw LocalLoginError.unavailable
        }
    }

    private func isSymbolicLinkIfPresent(_ url: URL) -> Bool {
        // `fileExists` follows links and therefore returns false for a broken
        // symlink. `readlink` semantics still identify that unsafe path.
        (try? FileManager.default.destinationOfSymbolicLink(atPath: url.path)) != nil
    }

    private enum ReadinessResult {
        case ready
        case exited
        case timedOut
    }

    private func waitUntilReady() async throws -> ReadinessResult {
        for attempt in 0..<Self.readinessAttempts {
            try Task.checkCancellation()
            guard process.isRunning else {
                return .exited
            }
            if await pageProbe(serverURL) {
                try Task.checkCancellation()
                return process.isRunning ? .ready : .exited
            }
            if attempt < Self.readinessAttempts - 1 {
                try await sleeper(Self.readinessDelay)
            }
        }
        try Task.checkCancellation()
        return process.isRunning ? .timedOut : .exited
    }

    static func isMoorePage(data: Data, response: HTTPURLResponse) -> Bool {
        guard
            response.statusCode == 200,
            let server = response.value(forHTTPHeaderField: "Server"),
            server.hasPrefix("MooreExporter/1.0"),
            let body = String(data: data, encoding: .utf8),
            body.contains("<title>Moore Exporter</title>")
        else {
            return false
        }
        return true
    }

    private static func defaultProbe(_ url: URL) async -> Bool {
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        request.timeoutInterval = 0.25

        guard
            let (data, response) = try? await URLSession.shared.data(for: request),
            let httpResponse = response as? HTTPURLResponse
        else {
            return false
        }
        return isMoorePage(data: data, response: httpResponse)
    }

    private static func defaultPortOccupancyProbe() -> Bool {
        let descriptor = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else {
            return true
        }
        defer { Darwin.close(descriptor) }

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = serverPort.bigEndian
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))

        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return result != 0
    }
}
