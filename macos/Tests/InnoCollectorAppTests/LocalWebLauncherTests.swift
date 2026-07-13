import Foundation
import Testing
@testable import InnoCollectorFeature

@MainActor
private final class RecordingWebProcess: LocalWebProcessControlling {
    enum ReadBehavior {
        case line(Data)
        case failure(LocalWebProcessReadError)
        case waitForResume
        case waitForCancellation
    }

    private(set) var starts: [(URL, [String])] = []
    private(set) var reads: [(maximumBytes: Int, timeout: TimeInterval)] = []
    private(set) var stopCount = 0
    nonisolated(unsafe) private(set) var emergencyStopCount = 0
    var isRunning = false
    var processIdentifier: Int32?
    var launchedPID: Int32 = 4_321
    var startError: (any Error)?
    var remainsRunningAfterStart = true
    var exitsWhileReading = false
    var readBehavior: ReadBehavior
    private var readyContinuation: CheckedContinuation<Data, any Error>?

    init(readBehavior: ReadBehavior) {
        self.readBehavior = readBehavior
    }

    func start(executable: URL, arguments: [String]) throws {
        if let startError {
            throw startError
        }
        starts.append((executable, arguments))
        processIdentifier = launchedPID
        isRunning = remainsRunningAfterStart
    }

    func readReadyLine(maximumBytes: Int, timeout: TimeInterval) async throws -> Data {
        reads.append((maximumBytes, timeout))
        switch readBehavior {
        case .line(let data):
            if exitsWhileReading {
                isRunning = false
            }
            return data
        case .failure(let error):
            throw error
        case .waitForResume:
            return try await withCheckedThrowingContinuation { continuation in
                readyContinuation = continuation
            }
        case .waitForCancellation:
            try await Task.sleep(for: .seconds(60))
            throw LocalWebProcessReadError.closed
        }
    }

    func resumeReady(with data: Data) {
        let continuation = readyContinuation
        readyContinuation = nil
        continuation?.resume(returning: data)
    }

    func stop() {
        stopCount += 1
        isRunning = false
        processIdentifier = nil
    }

    nonisolated func emergencyStop() {
        emergencyStopCount += 1
    }
}

private enum SyntheticLaunchError: Error {
    case failed
}

@Suite("Local Web launcher")
@MainActor
struct LocalWebLauncherTests {
    private struct Fixture {
        let root: URL
        let plugins: URL
        let executable: URL
        let projects: URL
        let support: URL

        func remove() {
            try? FileManager.default.removeItem(at: root)
        }
    }

    private func fixture() throws -> Fixture {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let contents = root.appendingPathComponent(
            "Collector.app/Contents",
            isDirectory: true
        )
        let plugins = contents.appendingPathComponent("PlugIns", isDirectory: true)
        let config = contents.appendingPathComponent(
            "Resources/config",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: plugins,
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: config,
            withIntermediateDirectories: true
        )
        let executable = plugins.appendingPathComponent("InnoCollectorWebServer")
        #expect(FileManager.default.createFile(atPath: executable.path, contents: Data()))
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755],
            ofItemAtPath: executable.path
        )
        let projects = config.appendingPathComponent("projects.json")
        try Data("{}".utf8).write(to: projects)
        return Fixture(
            root: root,
            plugins: plugins,
            executable: executable,
            projects: projects,
            support: root.appendingPathComponent(
                "Library/Application Support/com.inno.news.collector",
                isDirectory: true
            )
        )
    }

    private func readyLine(
        protocolVersion: Int = 1,
        pid: Int32 = 4_321,
        host: String = "127.0.0.1",
        port: Int = 49_123,
        extra: [String: Any] = [:]
    ) throws -> Data {
        var object: [String: Any] = [
            "protocol": protocolVersion,
            "pid": Int(pid),
            "host": host,
            "port": port,
        ]
        object.merge(extra) { _, new in new }
        var data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        data.append(0x0a)
        return data
    }

    private func launcher(
        fixture: Fixture,
        process: RecordingWebProcess,
        maximumReadyBytes: Int = 1_024,
        readyTimeout: TimeInterval = 15,
        browserOpener: @escaping LocalWebLauncher.BrowserOpener = { _ in true }
    ) -> LocalWebLauncher {
        LocalWebLauncher(
            executable: fixture.executable,
            pluginsDirectory: fixture.plugins,
            supportRoot: fixture.support,
            projectsConfig: fixture.projects,
            process: process,
            maximumReadyBytes: maximumReadyBytes,
            readyTimeout: readyTimeout,
            browserOpener: browserOpener
        )
    }

    @Test("packaged cold start has a bounded slow-Mac allowance")
    func packagedColdStartAllowance() {
        #expect(LocalWebLauncher.defaultReadyTimeout >= 60)
        #expect(LocalWebLauncher.defaultReadyTimeout <= 120)
    }

    @Test("production child environment overwrites an inherited launcher PID")
    func controlledLauncherEnvironment() {
        let environment = FoundationWebProcess.controlledEnvironment(
            inheriting: [
                "INNO_COLLECTOR_LAUNCHER_PID": "9999",
                "KEEP_ME": "yes",
            ],
            launcherPID: 4_321
        )

        #expect(environment["INNO_COLLECTOR_LAUNCHER_PID"] == "4321")
        #expect(environment["KEEP_ME"] == "yes")
    }

    @Test("starts once with fixed loopback arguments and reuses its verified endpoint")
    func startsAndReuses() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingWebProcess(readBehavior: .line(try readyLine()))
        var opened: [URL] = []
        let launcher = launcher(
            fixture: fixture,
            process: process,
            maximumReadyBytes: 777,
            readyTimeout: 9,
            browserOpener: { opened.append($0); return true }
        )

        try await launcher.open()
        try await launcher.open()

        #expect(process.starts.count == 1)
        #expect(process.starts[0].0 == fixture.executable)
        #expect(process.starts[0].1 == [
            "--support-root", fixture.support.path,
            "--projects", fixture.projects.path,
            "--host", "127.0.0.1",
            "--port", "0",
        ])
        #expect(process.reads.count == 1)
        #expect(process.reads[0].maximumBytes == 777)
        #expect(process.reads[0].timeout == 9)
        #expect(opened == [
            URL(string: "http://127.0.0.1:49123/")!,
            URL(string: "http://127.0.0.1:49123/")!,
        ])
    }

    @Test("concurrent opens share one launch and one ready read")
    func concurrentOpen() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingWebProcess(readBehavior: .waitForResume)
        var opened: [URL] = []
        let subject = launcher(
            fixture: fixture,
            process: process,
            browserOpener: { opened.append($0); return true }
        )

        let first = Task { @MainActor in try await subject.open() }
        while process.reads.isEmpty {
            await Task.yield()
        }
        let second = Task { @MainActor in try await subject.open() }
        for _ in 0..<10 {
            await Task.yield()
        }

        #expect(process.starts.count == 1)
        #expect(process.reads.count == 1)
        process.resumeReady(with: try readyLine())
        try await first.value
        try await second.value

        #expect(process.starts.count == 1)
        #expect(process.reads.count == 1)
        #expect(opened == [
            URL(string: "http://127.0.0.1:49123/")!,
            URL(string: "http://127.0.0.1:49123/")!,
        ])
    }

    @Test("rejects missing, non-executable, symlinked, and indirect Web Servers")
    func rejectsUnsafeExecutables() async throws {
        let missing = try fixture()
        defer { missing.remove() }
        try FileManager.default.removeItem(at: missing.executable)
        let missingProcess = RecordingWebProcess(readBehavior: .line(try readyLine()))
        await #expect(throws: LocalWebLauncherError.unavailable) {
            try await launcher(fixture: missing, process: missingProcess).open()
        }

        let plain = try fixture()
        defer { plain.remove() }
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o644],
            ofItemAtPath: plain.executable.path
        )
        let plainProcess = RecordingWebProcess(readBehavior: .line(try readyLine()))
        await #expect(throws: LocalWebLauncherError.unavailable) {
            try await launcher(fixture: plain, process: plainProcess).open()
        }

        let linked = try fixture()
        defer { linked.remove() }
        let target = linked.root.appendingPathComponent("real-server")
        try FileManager.default.moveItem(at: linked.executable, to: target)
        try FileManager.default.createSymbolicLink(
            at: linked.executable,
            withDestinationURL: target
        )
        let linkedProcess = RecordingWebProcess(readBehavior: .line(try readyLine()))
        await #expect(throws: LocalWebLauncherError.unavailable) {
            try await launcher(fixture: linked, process: linkedProcess).open()
        }

        let indirect = try fixture()
        defer { indirect.remove() }
        let nested = indirect.plugins.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        let nestedServer = nested.appendingPathComponent("InnoCollectorWebServer")
        try FileManager.default.moveItem(at: indirect.executable, to: nestedServer)
        let indirectProcess = RecordingWebProcess(readBehavior: .line(try readyLine()))
        let indirectLauncher = LocalWebLauncher(
            executable: nestedServer,
            pluginsDirectory: indirect.plugins,
            supportRoot: indirect.support,
            projectsConfig: indirect.projects,
            process: indirectProcess,
            browserOpener: { _ in true }
        )
        await #expect(throws: LocalWebLauncherError.unavailable) {
            try await indirectLauncher.open()
        }

        #expect(missingProcess.starts.isEmpty)
        #expect(plainProcess.starts.isEmpty)
        #expect(linkedProcess.starts.isEmpty)
        #expect(indirectProcess.starts.isEmpty)
    }

    @Test("rejects symlinked bundle, support, and packaged project boundaries")
    func rejectsSymlinkedBoundaries() async throws {
        let linkedPlugins = try fixture()
        defer { linkedPlugins.remove() }
        let realPlugins = linkedPlugins.root.appendingPathComponent(
            "real-plugins",
            isDirectory: true
        )
        try FileManager.default.moveItem(at: linkedPlugins.plugins, to: realPlugins)
        try FileManager.default.createSymbolicLink(
            at: linkedPlugins.plugins,
            withDestinationURL: realPlugins
        )
        let pluginsProcess = RecordingWebProcess(readBehavior: .line(try readyLine()))
        await #expect(throws: LocalWebLauncherError.unavailable) {
            try await launcher(fixture: linkedPlugins, process: pluginsProcess).open()
        }

        let linkedSupport = try fixture()
        defer { linkedSupport.remove() }
        try FileManager.default.createDirectory(
            at: linkedSupport.support.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let realSupport = linkedSupport.root.appendingPathComponent(
            "real-support",
            isDirectory: true
        )
        try FileManager.default.createDirectory(at: realSupport, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(
            at: linkedSupport.support,
            withDestinationURL: realSupport
        )
        let supportProcess = RecordingWebProcess(readBehavior: .line(try readyLine()))
        await #expect(throws: LocalWebLauncherError.unavailable) {
            try await launcher(fixture: linkedSupport, process: supportProcess).open()
        }

        let linkedApplicationSupport = try fixture()
        defer { linkedApplicationSupport.remove() }
        let applicationSupport = linkedApplicationSupport.support.deletingLastPathComponent()
        try FileManager.default.createDirectory(
            at: applicationSupport.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let realApplicationSupport = linkedApplicationSupport.root.appendingPathComponent(
            "real-application-support",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: realApplicationSupport,
            withIntermediateDirectories: true
        )
        try FileManager.default.createSymbolicLink(
            at: applicationSupport,
            withDestinationURL: realApplicationSupport
        )
        let applicationSupportProcess = RecordingWebProcess(
            readBehavior: .line(try readyLine())
        )
        await #expect(throws: LocalWebLauncherError.unavailable) {
            try await launcher(
                fixture: linkedApplicationSupport,
                process: applicationSupportProcess
            ).open()
        }

        let linkedProjects = try fixture()
        defer { linkedProjects.remove() }
        let realProjects = linkedProjects.root.appendingPathComponent("real-projects.json")
        try FileManager.default.moveItem(at: linkedProjects.projects, to: realProjects)
        try FileManager.default.createSymbolicLink(
            at: linkedProjects.projects,
            withDestinationURL: realProjects
        )
        let projectsProcess = RecordingWebProcess(readBehavior: .line(try readyLine()))
        await #expect(throws: LocalWebLauncherError.unavailable) {
            try await launcher(fixture: linkedProjects, process: projectsProcess).open()
        }

        let outsideProjects = try fixture()
        defer { outsideProjects.remove() }
        let movedProjects = outsideProjects.root.appendingPathComponent("projects.json")
        try FileManager.default.moveItem(at: outsideProjects.projects, to: movedProjects)
        let outsideProcess = RecordingWebProcess(readBehavior: .line(try readyLine()))
        let outsideLauncher = LocalWebLauncher(
            executable: outsideProjects.executable,
            pluginsDirectory: outsideProjects.plugins,
            supportRoot: outsideProjects.support,
            projectsConfig: movedProjects,
            process: outsideProcess,
            browserOpener: { _ in true }
        )
        await #expect(throws: LocalWebLauncherError.unavailable) {
            try await outsideLauncher.open()
        }

        let outsideSupport = try fixture()
        defer { outsideSupport.remove() }
        let outsideSupportProcess = RecordingWebProcess(readBehavior: .line(try readyLine()))
        let outsideSupportLauncher = LocalWebLauncher(
            executable: outsideSupport.executable,
            pluginsDirectory: outsideSupport.plugins,
            supportRoot: outsideSupport.root.appendingPathComponent("outside-support"),
            projectsConfig: outsideSupport.projects,
            process: outsideSupportProcess,
            browserOpener: { _ in true }
        )
        await #expect(throws: LocalWebLauncherError.unavailable) {
            try await outsideSupportLauncher.open()
        }

        #expect(pluginsProcess.starts.isEmpty)
        #expect(supportProcess.starts.isEmpty)
        #expect(applicationSupportProcess.starts.isEmpty)
        #expect(projectsProcess.starts.isEmpty)
        #expect(outsideProcess.starts.isEmpty)
        #expect(outsideSupportProcess.starts.isEmpty)
    }

    @Test("accepts only the exact trusted ready schema")
    func rejectsUntrustedReadyPayloads() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let invalidLines = try [
            readyLine(protocolVersion: 2),
            readyLine(pid: 9_999),
            readyLine(host: "localhost"),
            readyLine(host: "0.0.0.0"),
            readyLine(port: 0),
            readyLine(port: 65_536),
            readyLine(extra: ["url": "https://attacker.invalid/"]),
            readyLine(extra: ["token": "secret"]),
            readyLine(extra: ["browser_target": "file:///tmp/private"]),
            Data("not-json\n".utf8),
        ]

        for line in invalidLines {
            let process = RecordingWebProcess(readBehavior: .line(line))
            var opened = false
            let subject = launcher(
                fixture: fixture,
                process: process,
                browserOpener: { _ in opened = true; return true }
            )

            await #expect(throws: LocalWebLauncherError.invalidReady) {
                try await subject.open()
            }
            #expect(process.stopCount == 1)
            #expect(!opened)
        }
    }

    @Test("timeouts, oversized output, launch failure, and early exit all clean up")
    func startupFailuresCleanUp() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }

        let timedOut = RecordingWebProcess(readBehavior: .failure(.timedOut))
        await #expect(throws: LocalWebLauncherError.notReady) {
            try await launcher(fixture: fixture, process: timedOut).open()
        }
        #expect(timedOut.stopCount == 1)

        let oversized = RecordingWebProcess(readBehavior: .failure(.tooLong))
        await #expect(throws: LocalWebLauncherError.invalidReady) {
            try await launcher(fixture: fixture, process: oversized).open()
        }
        #expect(oversized.stopCount == 1)

        let launchFailed = RecordingWebProcess(readBehavior: .line(try readyLine()))
        launchFailed.startError = SyntheticLaunchError.failed
        await #expect(throws: LocalWebLauncherError.launchFailed) {
            try await launcher(fixture: fixture, process: launchFailed).open()
        }
        #expect(launchFailed.stopCount == 0)

        let exited = RecordingWebProcess(readBehavior: .line(try readyLine()))
        exited.exitsWhileReading = true
        await #expect(throws: LocalWebLauncherError.launchFailed) {
            try await launcher(fixture: fixture, process: exited).open()
        }
        #expect(exited.stopCount == 1)
    }

    @Test("cancellation and browser failure stop only the owned child")
    func cancellationAndBrowserFailure() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }

        let waiting = RecordingWebProcess(readBehavior: .waitForCancellation)
        let waitingLauncher = launcher(fixture: fixture, process: waiting)
        let openTask = Task { @MainActor in
            try await waitingLauncher.open()
        }
        while waiting.reads.isEmpty {
            await Task.yield()
        }
        openTask.cancel()
        await #expect(throws: CancellationError.self) {
            try await openTask.value
        }
        #expect(waiting.stopCount == 1)

        let rejectedBrowser = RecordingWebProcess(readBehavior: .line(try readyLine()))
        let browserLauncher = launcher(
            fixture: fixture,
            process: rejectedBrowser,
            browserOpener: { _ in false }
        )
        await #expect(throws: LocalWebLauncherError.browserUnavailable) {
            try await browserLauncher.open()
        }
        #expect(rejectedBrowser.stopCount == 1)
    }

    @Test("explicit stop is idempotent and clears the verified endpoint")
    func explicitStop() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingWebProcess(readBehavior: .line(try readyLine()))
        let subject = launcher(fixture: fixture, process: process)

        try await subject.open()
        subject.stop()
        subject.stop()

        #expect(process.starts.count == 1)
        #expect(process.stopCount == 1)
        #expect(!process.isRunning)
    }

    @Test("the production reader returns one line and enforces byte and time bounds")
    func boundedReadyLineReader() async throws {
        let twoLines = Pipe()
        try twoLines.fileHandleForWriting.write(contentsOf: Data("first\nsecond\n".utf8))
        try twoLines.fileHandleForWriting.close()

        let first = try await BoundedReadyLineReader.read(
            from: twoLines.fileHandleForReading,
            maximumBytes: 16,
            timeout: 1
        )
        #expect(first == Data("first".utf8))
        #expect(
            try twoLines.fileHandleForReading.readToEnd()
                == Data("second\n".utf8)
        )

        let oversized = Pipe()
        try oversized.fileHandleForWriting.write(contentsOf: Data("12345".utf8))
        try oversized.fileHandleForWriting.close()
        await #expect(throws: LocalWebProcessReadError.tooLong) {
            _ = try await BoundedReadyLineReader.read(
                from: oversized.fileHandleForReading,
                maximumBytes: 4,
                timeout: 1
            )
        }

        let silent = Pipe()
        await #expect(throws: LocalWebProcessReadError.timedOut) {
            _ = try await BoundedReadyLineReader.read(
                from: silent.fileHandleForReading,
                maximumBytes: 16,
                timeout: 0.02
            )
        }
        try silent.fileHandleForWriting.close()
    }
}
