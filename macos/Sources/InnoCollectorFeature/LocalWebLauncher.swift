import AppKit
import Darwin
import Foundation

@MainActor
public protocol LocalWebLaunching: AnyObject {
    func open() async throws
    func stop()
}

public enum LocalWebLauncherError: Error, Equatable, Sendable {
    case unavailable
    case launchFailed
    case notReady
    case invalidReady
    case browserUnavailable
}

public enum LocalWebPreview {
    public static func isEnabled(environment: [String: String]) -> Bool {
        environment["INNO_COLLECTOR_WEB_PREVIEW"] == "1"
    }
}

enum LocalWebProcessReadError: Error, Equatable, Sendable {
    case timedOut
    case tooLong
    case closed
}

@MainActor
protocol LocalWebProcessControlling: AnyObject, Sendable {
    var isRunning: Bool { get }
    var processIdentifier: Int32? { get }
    func start(executable: URL, arguments: [String]) throws
    func readReadyLine(maximumBytes: Int, timeout: TimeInterval) async throws -> Data
    func stop()
    nonisolated func emergencyStop()
}

extension LocalWebProcessControlling {
    nonisolated func emergencyStop() {}
}

enum BoundedReadyLineReader {
    static func read(
        from handle: FileHandle,
        maximumBytes: Int,
        timeout: TimeInterval
    ) async throws -> Data {
        guard maximumBytes > 0, timeout > 0, timeout.isFinite else {
            throw LocalWebProcessReadError.closed
        }
        let descriptor = Darwin.dup(handle.fileDescriptor)
        guard descriptor >= 0 else {
            throw LocalWebProcessReadError.closed
        }
        let operation = ReadyLineReadOperation(
            descriptor: descriptor,
            maximumBytes: maximumBytes,
            timeout: timeout
        )
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                operation.begin(continuation)
            }
        } onCancel: {
            operation.cancel()
        }
    }
}

private final class ReadyLineReadOperation: @unchecked Sendable {
    private let queue = DispatchQueue(label: "com.inno.news.collector.ready-line")
    private let maximumBytes: Int
    private let timeout: TimeInterval
    private var descriptor: Int32
    private var buffer = Data()
    private var continuation: CheckedContinuation<Data, any Error>?
    private var terminalResult: Result<Data, any Error>?
    private var readSource: (any DispatchSourceRead)?
    private var timer: (any DispatchSourceTimer)?

    init(descriptor: Int32, maximumBytes: Int, timeout: TimeInterval) {
        self.descriptor = descriptor
        self.maximumBytes = maximumBytes
        self.timeout = timeout
    }

    func begin(_ continuation: CheckedContinuation<Data, any Error>) {
        queue.async { [self] in
            if let terminalResult {
                continuation.resume(with: terminalResult)
                return
            }
            self.continuation = continuation
            startSources()
        }
    }

    func cancel() {
        queue.async { [self] in
            finish(.failure(CancellationError()))
        }
    }

    private func startSources() {
        guard terminalResult == nil, readSource == nil else {
            return
        }

        let flags = Darwin.fcntl(descriptor, F_GETFL)
        if flags >= 0 {
            _ = Darwin.fcntl(descriptor, F_SETFL, flags | O_NONBLOCK)
        }

        let source = DispatchSource.makeReadSource(
            fileDescriptor: descriptor,
            queue: queue
        )
        source.setEventHandler { [weak self] in
            self?.readAvailableBytes()
        }
        readSource = source

        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + timeout)
        timer.setEventHandler { [weak self] in
            self?.finish(.failure(LocalWebProcessReadError.timedOut))
        }
        self.timer = timer

        source.resume()
        timer.resume()
    }

    private func readAvailableBytes() {
        guard terminalResult == nil else {
            return
        }
        let remainingWithSentinel = maximumBytes - buffer.count + 1
        guard remainingWithSentinel > 0 else {
            finish(.failure(LocalWebProcessReadError.tooLong))
            return
        }
        let signalled = max(Int(readSource?.data ?? 0), 1)
        var bytesLeft = min(signalled, remainingWithSentinel, 4_096)
        while bytesLeft > 0, terminalResult == nil {
            var byte: UInt8 = 0
            let count = withUnsafeMutablePointer(to: &byte) { pointer in
                Darwin.read(descriptor, pointer, 1)
            }
            if count == 0 {
                finish(.failure(LocalWebProcessReadError.closed))
                return
            }
            if count < 0 {
                if errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR {
                    return
                }
                finish(.failure(LocalWebProcessReadError.closed))
                return
            }
            if byte == 0x0a {
                finish(.success(buffer))
                return
            }
            buffer.append(byte)
            if buffer.count > maximumBytes {
                finish(.failure(LocalWebProcessReadError.tooLong))
                return
            }
            bytesLeft -= 1
        }
    }

    private func finish(_ result: Result<Data, any Error>) {
        guard terminalResult == nil else {
            return
        }
        terminalResult = result
        readSource?.setEventHandler {}
        readSource?.cancel()
        readSource = nil
        timer?.setEventHandler {}
        timer?.cancel()
        timer = nil
        if descriptor >= 0 {
            Darwin.close(descriptor)
            descriptor = -1
        }
        if let continuation {
            self.continuation = nil
            continuation.resume(with: result)
        }
    }
}

@MainActor
final class FoundationWebProcess: LocalWebProcessControlling {
    private static let launcherPIDEnvironmentKey = "INNO_COLLECTOR_LAUNCHER_PID"
    private nonisolated static let gracefulWait = 0.5
    private nonisolated static let forcedWait = 0.5

    // Normal access is MainActor-isolated. These unsafe references exist only
    // so deinit can synchronously stop the child during application teardown.
    private nonisolated(unsafe) var process: Process?
    private nonisolated(unsafe) var outputPipe: Pipe?

    var isRunning: Bool {
        process?.isRunning == true
    }

    var processIdentifier: Int32? {
        process?.processIdentifier
    }

    func start(executable: URL, arguments: [String]) throws {
        stop()

        let pipe = Pipe()
        let next = Process()
        next.executableURL = executable
        next.arguments = arguments
        next.environment = Self.controlledEnvironment(
            inheriting: ProcessInfo.processInfo.environment,
            launcherPID: Darwin.getpid()
        )
        next.standardInput = FileHandle.nullDevice
        next.standardOutput = pipe
        next.standardError = FileHandle.nullDevice
        do {
            try next.run()
            try? pipe.fileHandleForWriting.close()
            process = next
            outputPipe = pipe
        } catch {
            try? pipe.fileHandleForReading.close()
            try? pipe.fileHandleForWriting.close()
            throw error
        }
    }

    static func controlledEnvironment(
        inheriting environment: [String: String],
        launcherPID: Int32
    ) -> [String: String] {
        precondition(launcherPID > 1)
        var controlled = environment
        controlled[launcherPIDEnvironmentKey] = String(launcherPID)
        return controlled
    }

    func readReadyLine(maximumBytes: Int, timeout: TimeInterval) async throws -> Data {
        guard let outputPipe else {
            throw LocalWebProcessReadError.closed
        }
        return try await BoundedReadyLineReader.read(
            from: outputPipe.fileHandleForReading,
            maximumBytes: maximumBytes,
            timeout: timeout
        )
    }

    func stop() {
        if let process {
            terminate(process)
        }
        closeOutput()
        process = nil
    }

    deinit {
        emergencyStop()
    }

    nonisolated func emergencyStop() {
        if let process {
            terminate(process)
        }
        closeOutput()
        process = nil
    }

    private nonisolated func closeOutput() {
        if let outputPipe {
            try? outputPipe.fileHandleForReading.close()
            try? outputPipe.fileHandleForWriting.close()
        }
        outputPipe = nil
    }

    private nonisolated func terminate(_ process: Process) {
        guard process.isRunning else {
            return
        }
        process.terminate()
        if waitForExit(process, timeout: Self.gracefulWait) {
            return
        }
        // This PID always comes from the Process instance created above. The
        // untrusted ready payload is never used as a termination target.
        _ = Darwin.kill(process.processIdentifier, SIGKILL)
        _ = waitForExit(process, timeout: Self.forcedWait)
    }

    private nonisolated func waitForExit(
        _ process: Process,
        timeout: TimeInterval
    ) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning, Date() < deadline {
            Thread.sleep(forTimeInterval: 0.01)
        }
        return !process.isRunning
    }
}

@MainActor
public final class LocalWebLauncher: LocalWebLaunching {
    public typealias BrowserOpener = @MainActor (URL) -> Bool

    private struct ReadyEndpoint: Sendable {
        let pid: Int32
        let port: UInt16

        var url: URL {
            URL(string: "http://127.0.0.1:\(port)/")!
        }
    }

    private struct ReadyPayload: Decodable {
        let `protocol`: Int
        let pid: Int32
        let host: String
        let port: Int
    }

    private static let executableName = "InnoCollectorWebServer"
    private static let trustedHost = "127.0.0.1"
    private static let readyProtocol = 1
    private static let defaultMaximumReadyBytes = 1_024
    // A first PyInstaller launch may need to unpack the embedded Python
    // runtime on a slower Mac.  Keep the wait bounded, but comfortably above
    // the 20–30 second cold starts observed during packaged-app verification.
    static let defaultReadyTimeout: TimeInterval = 90

    private let executable: URL
    private let pluginsDirectory: URL
    private let supportRoot: URL
    private let projectsConfig: URL
    private let process: any LocalWebProcessControlling
    private let maximumReadyBytes: Int
    private let readyTimeout: TimeInterval
    private let browserOpener: BrowserOpener

    private var startupTask: Task<ReadyEndpoint, any Error>?
    private var verifiedEndpoint: ReadyEndpoint?
    private var ownsProcess = false

    public convenience init(
        executable: URL,
        pluginsDirectory: URL,
        supportRoot: URL,
        projectsConfig: URL
    ) {
        self.init(
            executable: executable,
            pluginsDirectory: pluginsDirectory,
            supportRoot: supportRoot,
            projectsConfig: projectsConfig,
            process: FoundationWebProcess(),
            maximumReadyBytes: Self.defaultMaximumReadyBytes,
            readyTimeout: Self.defaultReadyTimeout,
            browserOpener: { NSWorkspace.shared.open($0) }
        )
    }

    init(
        executable: URL,
        pluginsDirectory: URL,
        supportRoot: URL,
        projectsConfig: URL,
        process: any LocalWebProcessControlling,
        maximumReadyBytes: Int = LocalWebLauncher.defaultMaximumReadyBytes,
        readyTimeout: TimeInterval = LocalWebLauncher.defaultReadyTimeout,
        browserOpener: @escaping BrowserOpener
    ) {
        self.executable = executable.standardizedFileURL
        self.pluginsDirectory = pluginsDirectory.standardizedFileURL
        self.supportRoot = supportRoot.standardizedFileURL
        self.projectsConfig = projectsConfig.standardizedFileURL
        self.process = process
        self.maximumReadyBytes = maximumReadyBytes
        self.readyTimeout = readyTimeout
        self.browserOpener = browserOpener
    }

    deinit {
        startupTask?.cancel()
        if ownsProcess {
            process.emergencyStop()
        }
    }

    public func open() async throws {
        try Task.checkCancellation()
        try validateBoundaries()

        if startupTask == nil, let verifiedEndpoint, process.isRunning {
            try openBrowser(for: verifiedEndpoint)
            return
        }

        let task: Task<ReadyEndpoint, any Error>
        if let startupTask {
            task = startupTask
        } else {
            stopOwnedProcess()
            verifiedEndpoint = nil
            let next = Task { @MainActor [self] in
                try await startAndReadReady()
            }
            startupTask = next
            task = next
        }

        do {
            let endpoint = try await withTaskCancellationHandler {
                try await task.value
            } onCancel: {
                task.cancel()
            }
            startupTask = nil
            try Task.checkCancellation()
            verifiedEndpoint = endpoint
            try openBrowser(for: endpoint)
        } catch is CancellationError {
            startupTask = nil
            verifiedEndpoint = nil
            task.cancel()
            stopOwnedProcess()
            throw CancellationError()
        } catch {
            startupTask = nil
            verifiedEndpoint = nil
            throw error
        }
    }

    public func stop() {
        startupTask?.cancel()
        startupTask = nil
        verifiedEndpoint = nil
        stopOwnedProcess()
    }

    private func startAndReadReady() async throws -> ReadyEndpoint {
        do {
            try Task.checkCancellation()
            do {
                try process.start(
                    executable: executable,
                    arguments: [
                        "--support-root", supportRoot.path,
                        "--projects", projectsConfig.path,
                        "--host", Self.trustedHost,
                        "--port", "0",
                    ]
                )
                ownsProcess = true
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                throw LocalWebLauncherError.launchFailed
            }

            guard
                process.isRunning,
                let childPID = process.processIdentifier,
                childPID > 0
            else {
                throw LocalWebLauncherError.launchFailed
            }

            let line: Data
            do {
                line = try await process.readReadyLine(
                    maximumBytes: maximumReadyBytes,
                    timeout: readyTimeout
                )
            } catch is CancellationError {
                throw CancellationError()
            } catch let error as LocalWebProcessReadError {
                switch error {
                case .timedOut, .closed:
                    throw LocalWebLauncherError.notReady
                case .tooLong:
                    throw LocalWebLauncherError.invalidReady
                }
            } catch {
                throw LocalWebLauncherError.notReady
            }

            let endpoint = try parseReady(line, expectedPID: childPID)
            guard
                process.isRunning,
                process.processIdentifier == childPID
            else {
                throw LocalWebLauncherError.launchFailed
            }
            return endpoint
        } catch {
            stopOwnedProcess()
            throw error
        }
    }

    private func openBrowser(for endpoint: ReadyEndpoint) throws {
        guard
            process.isRunning,
            process.processIdentifier == endpoint.pid
        else {
            verifiedEndpoint = nil
            stopOwnedProcess()
            throw LocalWebLauncherError.launchFailed
        }
        guard browserOpener(endpoint.url) else {
            verifiedEndpoint = nil
            stopOwnedProcess()
            throw LocalWebLauncherError.browserUnavailable
        }
    }

    private func parseReady(_ data: Data, expectedPID: Int32) throws -> ReadyEndpoint {
        guard !data.isEmpty, data.count <= maximumReadyBytes else {
            throw LocalWebLauncherError.invalidReady
        }
        guard
            let object = try? JSONSerialization.jsonObject(with: data),
            let dictionary = object as? [String: Any],
            Set(dictionary.keys) == Set(["protocol", "pid", "host", "port"]),
            let payload = try? JSONDecoder().decode(ReadyPayload.self, from: data),
            payload.protocol == Self.readyProtocol,
            payload.pid == expectedPID,
            payload.host == Self.trustedHost,
            (1...65_535).contains(payload.port),
            let port = UInt16(exactly: payload.port)
        else {
            throw LocalWebLauncherError.invalidReady
        }
        return ReadyEndpoint(pid: expectedPID, port: port)
    }

    private func validateBoundaries() throws {
        let executableValues = try? executable.resourceValues(forKeys: [
            .isRegularFileKey,
            .isSymbolicLinkKey,
        ])
        let pluginsValues = try? pluginsDirectory.resourceValues(forKeys: [
            .isDirectoryKey,
            .isSymbolicLinkKey,
        ])
        let projectsValues = try? projectsConfig.resourceValues(forKeys: [
            .isRegularFileKey,
            .isSymbolicLinkKey,
        ])
        let canonicalExecutable = executable.resolvingSymlinksInPath().standardizedFileURL
        let canonicalPlugins = pluginsDirectory.resolvingSymlinksInPath().standardizedFileURL
        let contentsDirectory = pluginsDirectory.deletingLastPathComponent()
        let configDirectory = projectsConfig.deletingLastPathComponent()
        let resourcesDirectory = configDirectory.deletingLastPathComponent()
        let applicationSupportDirectory = supportRoot.deletingLastPathComponent()
        let canonicalProjects = projectsConfig.resolvingSymlinksInPath().standardizedFileURL
        let canonicalContents = contentsDirectory.resolvingSymlinksInPath().standardizedFileURL

        guard
            maximumReadyBytes > 0,
            readyTimeout > 0,
            readyTimeout.isFinite,
            executable.lastPathComponent == Self.executableName,
            executable.deletingLastPathComponent() == pluginsDirectory,
            pluginsDirectory.lastPathComponent == "PlugIns",
            contentsDirectory.lastPathComponent == "Contents",
            contentsDirectory
                .deletingLastPathComponent().pathExtension == "app",
            canonicalExecutable.deletingLastPathComponent() == canonicalPlugins,
            executableValues?.isRegularFile == true,
            executableValues?.isSymbolicLink != true,
            pluginsValues?.isDirectory == true,
            pluginsValues?.isSymbolicLink != true,
            FileManager.default.isExecutableFile(atPath: executable.path),
            projectsConfig.lastPathComponent == "projects.json",
            configDirectory.lastPathComponent == "config",
            resourcesDirectory.lastPathComponent == "Resources",
            resourcesDirectory.deletingLastPathComponent() == contentsDirectory,
            canonicalProjects.deletingLastPathComponent()
                .deletingLastPathComponent()
                .deletingLastPathComponent() == canonicalContents,
            projectsValues?.isRegularFile == true,
            projectsValues?.isSymbolicLink != true,
            supportRoot.lastPathComponent == "com.inno.news.collector",
            applicationSupportDirectory.lastPathComponent == "Application Support",
            !isSymbolicLinkIfPresent(contentsDirectory),
            !isSymbolicLinkIfPresent(resourcesDirectory),
            !isSymbolicLinkIfPresent(configDirectory),
            !isSymbolicLinkIfPresent(applicationSupportDirectory),
            !isSymbolicLinkIfPresent(supportRoot)
        else {
            throw LocalWebLauncherError.unavailable
        }
    }

    private func isSymbolicLinkIfPresent(_ url: URL) -> Bool {
        (try? FileManager.default.destinationOfSymbolicLink(atPath: url.path)) != nil
    }

    private func stopOwnedProcess() {
        guard ownsProcess else {
            return
        }
        ownsProcess = false
        process.stop()
    }
}
