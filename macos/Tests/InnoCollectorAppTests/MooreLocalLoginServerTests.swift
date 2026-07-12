import Foundation
import Testing
@testable import InnoCollectorFeature

@MainActor
private final class RecordingLoginProcess: LocalLoginProcessControlling {
    private(set) var starts: [(URL, [String])] = []
    private(set) var stopCount = 0
    nonisolated(unsafe) private(set) var emergencyStopCount = 0
    var isRunning = false
    var remainsRunningAfterStart = true
    var startError: (any Error)?

    func start(executable: URL, arguments: [String]) throws {
        if let startError {
            throw startError
        }
        starts.append((executable, arguments))
        isRunning = remainsRunningAfterStart
    }

    func stop() {
        stopCount += 1
        isRunning = false
    }

    nonisolated func emergencyStop() {
        emergencyStopCount += 1
    }
}

private enum SyntheticStartError: Error {
    case failed
}

@MainActor
private final class ConcurrentOpenState {
    var portChecks = 0
    var isReady = false
    var sleepContinuation: CheckedContinuation<Void, any Error>?
    var opened: [URL] = []
}

@Suite("Moore local login server")
@MainActor
struct MooreLocalLoginServerTests {
    private struct Fixture {
        let root: URL
        let helper: URL
        let plugins: URL
        let support: URL

        var runtime: URL {
            support.appendingPathComponent("ExporterRuntime", isDirectory: true)
        }

        func remove() {
            try? FileManager.default.removeItem(at: root)
        }
    }

    private func fixture() throws -> Fixture {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let plugins = root.appendingPathComponent(
            "Collector.app/Contents/PlugIns",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: plugins,
            withIntermediateDirectories: true
        )
        let helper = plugins.appendingPathComponent("MooreExporterHelper")
        #expect(FileManager.default.createFile(atPath: helper.path, contents: Data()))
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755],
            ofItemAtPath: helper.path
        )
        let support = root.appendingPathComponent(
            "Library/Application Support/com.inno.news.collector",
            isDirectory: true
        )
        return Fixture(root: root, helper: helper, plugins: plugins, support: support)
    }

    private func server(
        fixture: Fixture,
        process: RecordingLoginProcess,
        portOccupancyProbe: @escaping MooreLocalLoginServer.PortOccupancyProbe = { false },
        pageProbe: @escaping MooreLocalLoginServer.PageProbe,
        sleeper: @escaping MooreLocalLoginServer.Sleeper = { _ in },
        browserOpener: @escaping MooreLocalLoginServer.BrowserOpener = { _ in true }
    ) -> MooreLocalLoginServer {
        MooreLocalLoginServer(
            executable: fixture.helper,
            pluginsDirectory: fixture.plugins,
            runtimeDirectory: fixture.runtime,
            supportRoot: fixture.support,
            process: process,
            portOccupancyProbe: portOccupancyProbe,
            pageProbe: pageProbe,
            sleeper: sleeper,
            browserOpener: browserOpener
        )
    }

    @Test("starts the bundled helper once and reopens the fixed local URL")
    func startsAndReuses() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingLoginProcess()
        var probes = [true, true]
        var opened: [URL] = []
        let server = server(
            fixture: fixture,
            process: process,
            pageProbe: { _ in probes.removeFirst() },
            browserOpener: { opened.append($0); return true }
        )

        try await server.open()
        try await server.open()

        #expect(process.starts.count == 1)
        #expect(process.starts[0].0 == fixture.helper)
        #expect(process.starts[0].1 == [
            "--runtime-dir", fixture.runtime.path,
            "exporter-server-start",
            "--host", "127.0.0.1",
            "--port", "18765",
            "--no-open",
        ])
        #expect(opened == [
            URL(string: "http://127.0.0.1:18765/")!,
            URL(string: "http://127.0.0.1:18765/")!,
        ])
        #expect(probes.isEmpty)
    }

    @Test("refuses an occupied port without starting or opening")
    func occupiedPort() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingLoginProcess()
        var opened = false
        let server = server(
            fixture: fixture,
            process: process,
            portOccupancyProbe: { true },
            pageProbe: { _ in false },
            browserOpener: { _ in opened = true; return true }
        )

        await #expect(throws: LocalLoginError.portInUse) {
            try await server.open()
        }
        #expect(process.starts.isEmpty)
        #expect(!opened)
    }

    @Test("retries readiness and stops its process after the bounded timeout")
    func readinessFailure() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingLoginProcess()
        var probeCount = 0
        var sleepCount = 0
        let server = server(
            fixture: fixture,
            process: process,
            pageProbe: { _ in probeCount += 1; return false },
            sleeper: { duration in
                #expect(duration == .milliseconds(100))
                sleepCount += 1
            }
        )

        await #expect(throws: LocalLoginError.notReady) {
            try await server.open()
        }
        #expect(probeCount == 30)
        #expect(sleepCount == 29)
        #expect(process.stopCount == 1)
    }

    @Test("rejects missing and non-executable helpers before probing")
    func rejectsInvalidFiles() async throws {
        let missing = try fixture()
        defer { missing.remove() }
        try FileManager.default.removeItem(at: missing.helper)
        let missingProcess = RecordingLoginProcess()
        var missingProbes = 0
        let missingServer = server(
            fixture: missing,
            process: missingProcess,
            pageProbe: { _ in missingProbes += 1; return false }
        )

        await #expect(throws: LocalLoginError.unavailable) {
            try await missingServer.open()
        }

        let plain = try fixture()
        defer { plain.remove() }
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o644],
            ofItemAtPath: plain.helper.path
        )
        let plainProcess = RecordingLoginProcess()
        let plainServer = server(
            fixture: plain,
            process: plainProcess,
            pageProbe: { _ in false }
        )

        await #expect(throws: LocalLoginError.unavailable) {
            try await plainServer.open()
        }
        #expect(missingProbes == 0)
        #expect(missingProcess.starts.isEmpty)
        #expect(plainProcess.starts.isEmpty)
    }

    @Test("rejects a helper symlink")
    func rejectsHelperSymlink() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let target = fixture.plugins.deletingLastPathComponent()
            .appendingPathComponent("real-helper")
        try FileManager.default.moveItem(at: fixture.helper, to: target)
        try FileManager.default.createSymbolicLink(
            at: fixture.helper,
            withDestinationURL: target
        )
        let process = RecordingLoginProcess()
        let server = server(
            fixture: fixture,
            process: process,
            pageProbe: { _ in false }
        )

        await #expect(throws: LocalLoginError.unavailable) {
            try await server.open()
        }
        #expect(process.starts.isEmpty)
    }

    @Test("rejects helper and runtime paths outside their direct boundaries")
    func rejectsOutsidePaths() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingLoginProcess()
        let outsideHelper = MooreLocalLoginServer(
            executable: fixture.helper,
            pluginsDirectory: fixture.plugins.deletingLastPathComponent(),
            runtimeDirectory: fixture.runtime,
            supportRoot: fixture.support,
            process: process,
            pageProbe: { _ in false },
            browserOpener: { _ in true }
        )
        await #expect(throws: LocalLoginError.unavailable) {
            try await outsideHelper.open()
        }

        let outsideRuntime = MooreLocalLoginServer(
            executable: fixture.helper,
            pluginsDirectory: fixture.plugins,
            runtimeDirectory: fixture.support.deletingLastPathComponent()
                .appendingPathComponent("ExporterRuntime", isDirectory: true),
            supportRoot: fixture.support,
            process: process,
            pageProbe: { _ in false },
            browserOpener: { _ in true }
        )
        await #expect(throws: LocalLoginError.unavailable) {
            try await outsideRuntime.open()
        }
        #expect(process.starts.isEmpty)
    }

    @Test("rejects a symlinked support root")
    func rejectsSymlinkedSupportRoot() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        try FileManager.default.createDirectory(
            at: fixture.support.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let outsideSupport = fixture.root.appendingPathComponent("outside-support")
        try FileManager.default.createDirectory(
            at: outsideSupport,
            withIntermediateDirectories: true
        )
        try FileManager.default.createSymbolicLink(
            at: fixture.support,
            withDestinationURL: outsideSupport
        )
        let process = RecordingLoginProcess()
        let server = server(
            fixture: fixture,
            process: process,
            pageProbe: { _ in false }
        )

        await #expect(throws: LocalLoginError.unavailable) {
            try await server.open()
        }
        #expect(process.starts.isEmpty)
    }

    @Test("rejects symlinked plugin and runtime directories")
    func rejectsOtherSymlinkedBoundaries() async throws {
        let pluginFixture = try fixture()
        defer { pluginFixture.remove() }
        let realPlugins = pluginFixture.root.appendingPathComponent("real-plugins")
        try FileManager.default.moveItem(at: pluginFixture.plugins, to: realPlugins)
        try FileManager.default.createSymbolicLink(
            at: pluginFixture.plugins,
            withDestinationURL: realPlugins
        )
        let pluginProcess = RecordingLoginProcess()
        let pluginServer = server(
            fixture: pluginFixture,
            process: pluginProcess,
            pageProbe: { _ in false }
        )
        await #expect(throws: LocalLoginError.unavailable) {
            try await pluginServer.open()
        }

        let runtimeFixture = try fixture()
        defer { runtimeFixture.remove() }
        try FileManager.default.createDirectory(
            at: runtimeFixture.support,
            withIntermediateDirectories: true
        )
        let outsideRuntime = runtimeFixture.root.appendingPathComponent("outside-runtime")
        try FileManager.default.createDirectory(
            at: outsideRuntime,
            withIntermediateDirectories: true
        )
        try FileManager.default.createSymbolicLink(
            at: runtimeFixture.runtime,
            withDestinationURL: outsideRuntime
        )
        let runtimeProcess = RecordingLoginProcess()
        let runtimeServer = server(
            fixture: runtimeFixture,
            process: runtimeProcess,
            pageProbe: { _ in false }
        )
        await #expect(throws: LocalLoginError.unavailable) {
            try await runtimeServer.open()
        }

        #expect(pluginProcess.starts.isEmpty)
        #expect(runtimeProcess.starts.isEmpty)
    }

    @Test("maps launch failures and immediate exits")
    func launchFailures() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let failedProcess = RecordingLoginProcess()
        failedProcess.startError = SyntheticStartError.failed
        let launchFailure = server(
            fixture: fixture,
            process: failedProcess,
            pageProbe: { _ in false }
        )
        await #expect(throws: LocalLoginError.launchFailed) {
            try await launchFailure.open()
        }
        #expect(failedProcess.stopCount == 0)

        let exitedProcess = RecordingLoginProcess()
        exitedProcess.remainsRunningAfterStart = false
        let immediateExit = server(
            fixture: fixture,
            process: exitedProcess,
            pageProbe: { _ in false }
        )
        await #expect(throws: LocalLoginError.launchFailed) {
            try await immediateExit.open()
        }
        #expect(exitedProcess.stopCount == 1)
    }

    @Test("maps a port race followed by helper exit to port in use")
    func portRaceAfterLaunch() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingLoginProcess()
        var portChecks = [false, true]
        let server = server(
            fixture: fixture,
            process: process,
            portOccupancyProbe: { portChecks.removeFirst() },
            pageProbe: { _ in process.isRunning = false; return false }
        )

        await #expect(throws: LocalLoginError.portInUse) {
            try await server.open()
        }
        #expect(process.stopCount == 1)
        #expect(portChecks.isEmpty)
    }

    @Test("maps an exit during readiness to launch failure")
    func exitsDuringReadiness() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingLoginProcess()
        var probes = 0
        let server = server(
            fixture: fixture,
            process: process,
            pageProbe: { _ in
                probes += 1
                if probes == 1 {
                    process.isRunning = false
                }
                return false
            }
        )

        await #expect(throws: LocalLoginError.launchFailed) {
            try await server.open()
        }
        #expect(process.stopCount == 1)
    }

    @Test("maps browser failures, keeps the server reusable, and supports stop")
    func browserFailureAndStop() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingLoginProcess()
        var probes = [true, true]
        var browserAttempts = 0
        let server = server(
            fixture: fixture,
            process: process,
            pageProbe: { _ in probes.removeFirst() },
            browserOpener: { _ in
                browserAttempts += 1
                return browserAttempts > 1
            }
        )

        await #expect(throws: LocalLoginError.browserUnavailable) {
            try await server.open()
        }
        #expect(process.isRunning)
        try await server.open()
        #expect(process.starts.count == 1)
        server.stop()
        #expect(process.stopCount == 1)
        #expect(!process.isRunning)
    }

    @Test("does not open a running process until it is ready")
    func runningProcessNotReady() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingLoginProcess()
        process.isRunning = true
        var opened = false
        let server = server(
            fixture: fixture,
            process: process,
            pageProbe: { _ in false },
            browserOpener: { _ in opened = true; return true }
        )

        await #expect(throws: LocalLoginError.notReady) {
            try await server.open()
        }
        #expect(process.starts.isEmpty)
        #expect(!opened)
    }

    @Test("serializes concurrent opens through one startup task")
    func concurrentOpensStartOnce() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingLoginProcess()
        let state = ConcurrentOpenState()
        let server = server(
            fixture: fixture,
            process: process,
            portOccupancyProbe: { state.portChecks += 1; return false },
            pageProbe: { _ in state.isReady },
            sleeper: { _ in
                try await withCheckedThrowingContinuation {
                    state.sleepContinuation = $0
                }
            },
            browserOpener: { state.opened.append($0); return true }
        )

        let first = Task { try await server.open() }
        while state.sleepContinuation == nil {
            await Task.yield()
        }
        let second = Task { try await server.open() }
        await Task.yield()
        state.isReady = true
        state.sleepContinuation?.resume()

        try await first.value
        try await second.value
        #expect(process.starts.count == 1)
        #expect(state.portChecks == 1)
        #expect(state.opened.count == 2)
    }

    @Test("cancellation stops the launched helper and never opens a browser")
    func cancellationStopsLaunch() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingLoginProcess()
        var opened = false
        var enteredSleep = false
        let server = server(
            fixture: fixture,
            process: process,
            pageProbe: { _ in false },
            sleeper: { _ in
                enteredSleep = true
                try await Task.sleep(for: .seconds(60))
            },
            browserOpener: { _ in opened = true; return true }
        )

        let opening = Task { try await server.open() }
        while !enteredSleep {
            await Task.yield()
        }
        opening.cancel()

        await #expect(throws: CancellationError.self) {
            try await opening.value
        }
        #expect(process.stopCount == 1)
        #expect(!process.isRunning)
        #expect(!opened)
    }

    @Test("recognizes only the Moore dashboard response fingerprint")
    func validatesPageFingerprint() throws {
        let url = try #require(URL(string: "http://127.0.0.1:18765/"))
        let body = Data("<!doctype html><title>Moore Exporter</title>".utf8)

        func response(status: Int = 200, server: String? = "MooreExporter/1.0 Python/3.13") throws -> HTTPURLResponse {
            var headers: [String: String] = [:]
            if let server {
                headers["Server"] = server
            }
            return try #require(HTTPURLResponse(
                url: url,
                statusCode: status,
                httpVersion: "HTTP/1.0",
                headerFields: headers
            ))
        }

        #expect(MooreLocalLoginServer.isMoorePage(data: body, response: try response()))
        #expect(!MooreLocalLoginServer.isMoorePage(
            data: body,
            response: try response(status: 404)
        ))
        #expect(!MooreLocalLoginServer.isMoorePage(
            data: body,
            response: try response(server: "Unknown/1.0")
        ))
        #expect(!MooreLocalLoginServer.isMoorePage(
            data: Data("<title>Something Else</title>".utf8),
            response: try response()
        ))
        #expect(!MooreLocalLoginServer.isMoorePage(
            data: body,
            response: try response(server: nil)
        ))
    }

    @Test("server release invokes its synchronous cleanup fallback")
    func releaseStopsProcess() async throws {
        let fixture = try fixture()
        defer { fixture.remove() }
        let process = RecordingLoginProcess()
        var server: MooreLocalLoginServer? = server(
            fixture: fixture,
            process: process,
            pageProbe: { _ in true }
        )

        #expect(server != nil)
        server = nil
        #expect(process.emergencyStopCount == 1)
    }
}
