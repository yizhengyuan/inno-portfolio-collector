# Collector Local Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe Collector-only button that starts Moore's localhost login dashboard, then build and verify a Reader-isolated Collector self-use pilot DMG.

**Architecture:** `AppLocations` exposes the Collector-only Moore helper and exporter runtime paths. A new `MooreLocalLoginServer` owns a narrowly configured child process, validates bundle/runtime boundaries, probes only `127.0.0.1:18765`, and opens that URL through an injected browser function. `CollectorViewModel` depends on a small `LocalLoginServing` interface, while SwiftUI only triggers the ViewModel and stops the service when the view disappears.

**Tech Stack:** Swift 6, SwiftUI, Foundation `Process`/`URLSession`, Swift Testing, Python `unittest`, PyInstaller, macOS `codesign`/`hdiutil`/`shasum`.

---

## File map

- Modify `macos/Sources/InnoAppCore/FileLocations.swift`: expose Collector-only `mooreHelper` and `exporterRuntime` URLs without granting them to Reader.
- Modify `macos/Tests/InnoAppCoreTests/FileLocationsTests.swift`: prove the new paths stay in their required boundaries.
- Create `macos/Sources/InnoCollectorFeature/MooreLocalLoginServer.swift`: define the login interface, stable errors, process adapter, path validation, readiness probing, reuse, and cleanup.
- Create `macos/Tests/InnoCollectorAppTests/MooreLocalLoginServerTests.swift`: test exact commands, boundary failures, port conflicts, reuse, readiness failures, browser failures, and stop semantics.
- Modify `macos/Sources/InnoCollectorFeature/CollectorViewModel.swift`: inject and invoke the login service with stable Chinese errors.
- Modify `macos/Tests/InnoCollectorAppTests/CollectorViewModelTests.swift`: test ViewModel success/failure/stop behavior.
- Modify `macos/Sources/InnoCollectorFeature/CollectorContentView.swift`: add the self-use warning and login button; stop the service on disappearance.
- Modify `macos/Sources/InnoCollectorApp/InnoCollectorApp.swift`: construct the concrete login service from `AppLocations`.
- Create ignored pilot artifacts under `dist/自用试用/`: Collector DMG, Chinese instructions, and SHA-256; do not commit, tag, or publish them.

### Task 1: Add Collector-only Moore locations

**Files:**
- Modify: `macos/Sources/InnoAppCore/FileLocations.swift`
- Test: `macos/Tests/InnoAppCoreTests/FileLocationsTests.swift`

- [ ] **Step 1: Write the failing path-boundary test**

Add these expectations to `rolesAreSeparated()`:

```swift
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
```

Extend `helpersStayInsideBundle()` with a Collector assertion:

```swift
let collector = try AppLocations.resolve(
    role: .collector,
    applicationSupport: applicationSupport,
    bundleURL: bundle
)
#expect(collector.mooreHelper?.deletingLastPathComponent() == plugins)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
./scripts/test_swift.sh --filter FileLocationsTests
```

Expected: compilation fails because `AppLocations` has no `mooreHelper` or `exporterRuntime` members.

- [ ] **Step 3: Implement the minimal role-specific paths**

In `AppLocations`, add:

```swift
public let mooreHelper: URL?
public let exporterRuntime: URL?
```

In `resolve`, compute and return:

```swift
let mooreHelper = plugins
    .appendingPathComponent("MooreExporterHelper", isDirectory: false)
    .standardizedFileURL

return Self(
    supportRoot: supportRoot,
    vault: vault,
    inbox: supportRoot.appendingPathComponent("DraftInbox", isDirectory: true),
    helper: helper,
    projectsConfig: role == .collector
        ? resources.appendingPathComponent("config/projects.json", isDirectory: false)
        : nil,
    mooreHelper: role == .collector ? mooreHelper : nil,
    exporterRuntime: role == .collector
        ? supportRoot.appendingPathComponent("ExporterRuntime", isDirectory: true)
        : nil
)
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
./scripts/test_swift.sh --filter FileLocationsTests
```

Expected: all `FileLocationsTests` pass.

- [ ] **Step 5: Commit the location boundary**

```bash
git add macos/Sources/InnoAppCore/FileLocations.swift \
  macos/Tests/InnoAppCoreTests/FileLocationsTests.swift
git commit -m "feat: expose Collector-only login paths"
```

### Task 2: Implement the local login server with fail-closed boundaries

**Files:**
- Create: `macos/Sources/InnoCollectorFeature/MooreLocalLoginServer.swift`
- Create: `macos/Tests/InnoCollectorAppTests/MooreLocalLoginServerTests.swift`

- [ ] **Step 1: Write failing tests for exact launch configuration and reuse**

Create test doubles in `MooreLocalLoginServerTests.swift`:

```swift
import Foundation
import Testing
@testable import InnoCollectorFeature

@MainActor
private final class RecordingLoginProcess: LocalLoginProcessControlling {
    private(set) var starts: [(URL, [String])] = []
    private(set) var stopCount = 0
    var isRunning = false
    var remainsRunningAfterStart = true
    var startError: Error?

    func start(executable: URL, arguments: [String]) throws {
        if let startError { throw startError }
        starts.append((executable, arguments))
        isRunning = remainsRunningAfterStart
    }

    func stop() {
        stopCount += 1
        isRunning = false
    }
}

@Suite("Moore local login server")
@MainActor
struct MooreLocalLoginServerTests {
    private func fixture() throws -> (URL, URL, URL) {
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
        return (helper, plugins, support)
    }

    @Test("starts the bundled helper once and reopens the fixed local URL")
    func startsAndReuses() async throws {
        let (helper, plugins, support) = try fixture()
        let process = RecordingLoginProcess()
        var probes = [false, true, true]
        var opened: [URL] = []
        let server = MooreLocalLoginServer(
            executable: helper,
            pluginsDirectory: plugins,
            runtimeDirectory: support.appendingPathComponent("ExporterRuntime"),
            supportRoot: support,
            process: process,
            pageProbe: { _ in probes.removeFirst() },
            browserOpener: { opened.append($0); return true }
        )

        try await server.open()
        try await server.open()

        #expect(process.starts.count == 1)
        #expect(process.starts[0].0 == helper)
        #expect(process.starts[0].1 == [
            "--runtime-dir", support.appendingPathComponent("ExporterRuntime").path,
            "exporter-server-start", "--host", "127.0.0.1",
            "--port", "18765", "--no-open",
        ])
        #expect(opened == [
            URL(string: "http://127.0.0.1:18765/")!,
            URL(string: "http://127.0.0.1:18765/")!,
        ])
    }
}
```

- [ ] **Step 2: Run the new suite and verify RED**

Run:

```bash
./scripts/test_swift.sh --filter MooreLocalLoginServerTests
```

Expected: compilation fails because `LocalLoginProcessControlling` and `MooreLocalLoginServer` do not exist.

- [ ] **Step 3: Add the minimal public interface, errors, and process adapter**

Create `MooreLocalLoginServer.swift` with these types:

```swift
import AppKit
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
protocol LocalLoginProcessControlling: AnyObject {
    var isRunning: Bool { get }
    func start(executable: URL, arguments: [String]) throws
    func stop()
}

@MainActor
final class FoundationLoginProcess: LocalLoginProcessControlling {
    private var process: Process?
    var isRunning: Bool { process?.isRunning == true }

    func start(executable: URL, arguments: [String]) throws {
        stop()
        let next = Process()
        next.executableURL = executable
        next.arguments = arguments
        next.standardOutput = FileHandle.nullDevice
        next.standardError = FileHandle.nullDevice
        try next.run()
        process = next
    }

    func stop() {
        if process?.isRunning == true { process?.terminate() }
        process = nil
    }
}
```

- [ ] **Step 4: Implement validation, port ownership, readiness, reuse, and cleanup**

Add the concrete service:

```swift
@MainActor
public final class MooreLocalLoginServer: LocalLoginServing {
    public typealias PageProbe = @MainActor (URL) async -> Bool
    public typealias Sleeper = @MainActor (Duration) async -> Void
    public typealias BrowserOpener = @MainActor (URL) -> Bool

    private let executable: URL
    private let pluginsDirectory: URL
    private let runtimeDirectory: URL
    private let supportRoot: URL
    private let serverURL = URL(string: "http://127.0.0.1:18765/")!
    private let process: any LocalLoginProcessControlling
    private let pageProbe: PageProbe
    private let sleeper: Sleeper
    private let browserOpener: BrowserOpener

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
            pageProbe: Self.defaultProbe,
            sleeper: { try? await Task.sleep(for: $0) },
            browserOpener: { NSWorkspace.shared.open($0) }
        )
    }

    init(
        executable: URL,
        pluginsDirectory: URL,
        runtimeDirectory: URL,
        supportRoot: URL,
        process: any LocalLoginProcessControlling,
        pageProbe: @escaping PageProbe,
        sleeper: @escaping Sleeper = { _ in },
        browserOpener: @escaping BrowserOpener
    ) {
        self.executable = executable.standardizedFileURL
        self.pluginsDirectory = pluginsDirectory.standardizedFileURL
        self.runtimeDirectory = runtimeDirectory.standardizedFileURL
        self.supportRoot = supportRoot.standardizedFileURL
        self.process = process
        self.pageProbe = pageProbe
        self.sleeper = sleeper
        self.browserOpener = browserOpener
    }

    public func open() async throws {
        try validateBoundaries()
        if process.isRunning {
            guard await pageProbe(serverURL) else { throw LocalLoginError.notReady }
            guard browserOpener(serverURL) else {
                throw LocalLoginError.browserUnavailable
            }
            return
        }
        guard !(await pageProbe(serverURL)) else { throw LocalLoginError.portInUse }
        do {
            try process.start(
                executable: executable,
                arguments: [
                    "--runtime-dir", runtimeDirectory.path,
                    "exporter-server-start", "--host", "127.0.0.1",
                    "--port", "18765", "--no-open",
                ]
            )
        } catch {
            throw LocalLoginError.launchFailed
        }
        guard process.isRunning else {
            process.stop()
            throw LocalLoginError.launchFailed
        }
        guard await waitUntilReady() else {
            process.stop()
            throw LocalLoginError.notReady
        }
        guard process.isRunning else {
            process.stop()
            throw LocalLoginError.launchFailed
        }
        guard browserOpener(serverURL) else {
            throw LocalLoginError.browserUnavailable
        }
    }

    public func stop() { process.stop() }

    private func validateBoundaries() throws {
        let values = try? executable.resourceValues(forKeys: [
            .isRegularFileKey, .isSymbolicLinkKey,
        ])
        guard
            executable.lastPathComponent == "MooreExporterHelper",
            executable.deletingLastPathComponent() == pluginsDirectory,
            values?.isRegularFile == true,
            values?.isSymbolicLink != true,
            FileManager.default.isExecutableFile(atPath: executable.path),
            runtimeDirectory.deletingLastPathComponent() == supportRoot,
            runtimeDirectory.lastPathComponent == "ExporterRuntime"
        else { throw LocalLoginError.unavailable }
    }

    private func waitUntilReady() async -> Bool {
        for _ in 0..<30 {
            guard process.isRunning else { return false }
            if await pageProbe(serverURL) { return true }
            await sleeper(.milliseconds(100))
        }
        return false
    }

    private static func defaultProbe(_ url: URL) async -> Bool {
        var request = URLRequest(url: url)
        request.timeoutInterval = 0.25
        if let (_, response) = try? await URLSession.shared.data(for: request),
           let http = response as? HTTPURLResponse,
           (200..<500).contains(http.statusCode) {
            return true
        }
        return false
    }
}
```

The pre-launch probe is deliberately a single fast request. Only the post-launch
readiness path retries, so a normal click does not wait three seconds before the
helper starts. Tests inject the no-op sleeper shown by the internal initializer.

- [ ] **Step 5: Run the focused suite and verify GREEN**

Run:

```bash
./scripts/test_swift.sh --filter MooreLocalLoginServerTests
```

Expected: the launch/reuse test passes.

- [ ] **Step 6: Add failing edge-case tests**

Add tests using the same fixture and fake process for:

```swift
@Test("refuses an already occupied port without starting or opening")
func occupiedPort() async throws {
    let (helper, plugins, support) = try fixture()
    let process = RecordingLoginProcess()
    var opened = false
    let server = MooreLocalLoginServer(
        executable: helper,
        pluginsDirectory: plugins,
        runtimeDirectory: support.appendingPathComponent("ExporterRuntime"),
        supportRoot: support,
        process: process,
        pageProbe: { _ in true },
        browserOpener: { _ in opened = true; return true }
    )
    await #expect(throws: LocalLoginError.portInUse) { try await server.open() }
    #expect(process.starts.isEmpty)
    #expect(!opened)
}

@Test("stops its process when readiness fails")
func readinessFailure() async throws {
    let (helper, plugins, support) = try fixture()
    let process = RecordingLoginProcess()
    let server = MooreLocalLoginServer(
        executable: helper,
        pluginsDirectory: plugins,
        runtimeDirectory: support.appendingPathComponent("ExporterRuntime"),
        supportRoot: support,
        process: process,
        pageProbe: { _ in false },
        browserOpener: { _ in true }
    )
    await #expect(throws: LocalLoginError.notReady) { try await server.open() }
    #expect(process.stopCount == 1)
}
```

Add the remaining cases explicitly:

```swift
private enum SyntheticStartError: Error { case failed }

@Test("rejects a missing or non-executable helper")
func rejectsInvalidFiles() async throws {
    let (missing, plugins, support) = try fixture()
    try FileManager.default.removeItem(at: missing)
    let missingProcess = RecordingLoginProcess()
    let missingServer = MooreLocalLoginServer(
        executable: missing, pluginsDirectory: plugins,
        runtimeDirectory: support.appendingPathComponent("ExporterRuntime"),
        supportRoot: support, process: missingProcess, pageProbe: { _ in false },
        browserOpener: { _ in true }
    )
    await #expect(throws: LocalLoginError.unavailable) {
        try await missingServer.open()
    }

    let (plain, plainPlugins, plainSupport) = try fixture()
    try FileManager.default.setAttributes(
        [.posixPermissions: 0o644], ofItemAtPath: plain.path
    )
    let plainProcess = RecordingLoginProcess()
    let plainServer = MooreLocalLoginServer(
        executable: plain, pluginsDirectory: plainPlugins,
        runtimeDirectory: plainSupport.appendingPathComponent("ExporterRuntime"),
        supportRoot: plainSupport, process: plainProcess,
        pageProbe: { _ in false }, browserOpener: { _ in true }
    )
    await #expect(throws: LocalLoginError.unavailable) {
        try await plainServer.open()
    }
    #expect(missingProcess.starts.isEmpty)
    #expect(plainProcess.starts.isEmpty)
}

@Test("rejects a symlink helper")
func rejectsSymlink() async throws {
    let (helper, plugins, support) = try fixture()
    let target = plugins.deletingLastPathComponent()
        .appendingPathComponent("real-helper")
    try FileManager.default.moveItem(at: helper, to: target)
    try FileManager.default.createSymbolicLink(at: helper, withDestinationURL: target)
    let process = RecordingLoginProcess()
    let server = MooreLocalLoginServer(
        executable: helper, pluginsDirectory: plugins,
        runtimeDirectory: support.appendingPathComponent("ExporterRuntime"),
        supportRoot: support, process: process, pageProbe: { _ in false },
        browserOpener: { _ in true }
    )
    await #expect(throws: LocalLoginError.unavailable) { try await server.open() }
    #expect(process.starts.isEmpty)
}

@Test("rejects helper and runtime paths outside their boundaries")
func rejectsOutsidePaths() async throws {
    let (helper, plugins, support) = try fixture()
    let process = RecordingLoginProcess()
    let outsideHelper = MooreLocalLoginServer(
        executable: helper, pluginsDirectory: plugins.deletingLastPathComponent(),
        runtimeDirectory: support.appendingPathComponent("ExporterRuntime"),
        supportRoot: support, process: process, pageProbe: { _ in false },
        browserOpener: { _ in true }
    )
    await #expect(throws: LocalLoginError.unavailable) {
        try await outsideHelper.open()
    }
    let outsideRuntime = MooreLocalLoginServer(
        executable: helper, pluginsDirectory: plugins,
        runtimeDirectory: support.deletingLastPathComponent()
            .appendingPathComponent("ExporterRuntime"),
        supportRoot: support, process: process, pageProbe: { _ in false },
        browserOpener: { _ in true }
    )
    await #expect(throws: LocalLoginError.unavailable) {
        try await outsideRuntime.open()
    }
}

@Test("maps process and browser failures and supports explicit stop")
func lifecycleFailures() async throws {
    let (helper, plugins, support) = try fixture()
    let failedProcess = RecordingLoginProcess()
    failedProcess.startError = SyntheticStartError.failed
    let launchFailure = MooreLocalLoginServer(
        executable: helper, pluginsDirectory: plugins,
        runtimeDirectory: support.appendingPathComponent("ExporterRuntime"),
        supportRoot: support, process: failedProcess, pageProbe: { _ in false },
        browserOpener: { _ in true }
    )
    await #expect(throws: LocalLoginError.launchFailed) {
        try await launchFailure.open()
    }

    let exitedProcess = RecordingLoginProcess()
    exitedProcess.remainsRunningAfterStart = false
    let immediateExit = MooreLocalLoginServer(
        executable: helper, pluginsDirectory: plugins,
        runtimeDirectory: support.appendingPathComponent("ExporterRuntime"),
        supportRoot: support, process: exitedProcess,
        pageProbe: { _ in false }, browserOpener: { _ in true }
    )
    await #expect(throws: LocalLoginError.launchFailed) {
        try await immediateExit.open()
    }
    #expect(exitedProcess.stopCount == 1)

    let process = RecordingLoginProcess()
    var probes = [false, true]
    let browserFailure = MooreLocalLoginServer(
        executable: helper, pluginsDirectory: plugins,
        runtimeDirectory: support.appendingPathComponent("ExporterRuntime"),
        supportRoot: support, process: process,
        pageProbe: { _ in probes.removeFirst() },
        browserOpener: { _ in false }
    )
    await #expect(throws: LocalLoginError.browserUnavailable) {
        try await browserFailure.open()
    }
    browserFailure.stop()
    #expect(process.stopCount == 1)
}
```

- [ ] **Step 7: Run all server tests**

Run:

```bash
./scripts/test_swift.sh --filter MooreLocalLoginServerTests
```

Expected: all server lifecycle and security tests pass.

- [ ] **Step 8: Commit the service**

```bash
git add macos/Sources/InnoCollectorFeature/MooreLocalLoginServer.swift \
  macos/Tests/InnoCollectorAppTests/MooreLocalLoginServerTests.swift
git commit -m "feat: add local Collector login server"
```

### Task 3: Wire login through ViewModel and SwiftUI

**Files:**
- Modify: `macos/Sources/InnoCollectorFeature/CollectorViewModel.swift`
- Modify: `macos/Tests/InnoCollectorAppTests/CollectorViewModelTests.swift`
- Modify: `macos/Sources/InnoCollectorFeature/CollectorContentView.swift`
- Modify: `macos/Sources/InnoCollectorApp/InnoCollectorApp.swift`

- [ ] **Step 1: Write failing ViewModel tests**

Add a fake service above `CollectorViewModelTests`:

```swift
@MainActor
private final class RecordingLoginService: LocalLoginServing {
    private(set) var openCount = 0
    private(set) var stopCount = 0
    var error: LocalLoginError?

    func open() async throws {
        openCount += 1
        if let error { throw error }
    }

    func stop() { stopCount += 1 }
}
```

Add tests:

```swift
@Test("opens and stops the local login service")
func localLoginLifecycle() async {
    let login = RecordingLoginService()
    let model = CollectorViewModel(
        helper: RecordingHelper(),
        locations: locations,
        localLogin: login
    )
    await model.openLocalLogin()
    model.stopLocalLogin()
    #expect(login.openCount == 1)
    #expect(login.stopCount == 1)
    #expect(model.errorMessage == nil)
}

@Test("maps local login failures to stable Chinese errors")
func localLoginErrors() async {
    let login = RecordingLoginService()
    login.error = .portInUse
    let model = CollectorViewModel(
        helper: RecordingHelper(),
        locations: locations,
        localLogin: login
    )
    await model.openLocalLogin()
    #expect(model.errorMessage == "本地登录端口被占用，请关闭旧后台或重启后重试。")
}
```

- [ ] **Step 2: Run ViewModel tests and verify RED**

Run:

```bash
./scripts/test_swift.sh --filter CollectorViewModelTests
```

Expected: compilation fails because the initializer and login methods do not exist.

- [ ] **Step 3: Add ViewModel injection and stable error mapping**

Add:

```swift
private let localLogin: (any LocalLoginServing)?

public init(
    helper: any HelperCalling,
    locations: AppLocations,
    localLogin: (any LocalLoginServing)? = nil
) {
    self.helper = helper
    self.locations = locations
    self.localLogin = localLogin
}

public func openLocalLogin() async {
    guard !isBusy else { return }
    isBusy = true
    errorMessage = nil
    defer { isBusy = false }
    guard let localLogin else {
        errorMessage = "本地登录后台不可用。"
        return
    }
    do {
        try await localLogin.open()
    } catch let error as LocalLoginError {
        switch error {
        case .portInUse:
            errorMessage = "本地登录端口被占用，请关闭旧后台或重启后重试。"
        case .browserUnavailable:
            errorMessage = "请在浏览器打开 http://127.0.0.1:18765/。"
        case .unavailable, .launchFailed, .notReady:
            errorMessage = "本地登录后台启动失败。"
        }
    } catch {
        errorMessage = "本地登录后台启动失败。"
    }
}

public func stopLocalLogin() { localLogin?.stop() }
```

- [ ] **Step 4: Run ViewModel tests and verify GREEN**

Run:

```bash
./scripts/test_swift.sh --filter CollectorViewModelTests
```

Expected: all Collector ViewModel tests pass.

- [ ] **Step 5: Wire the Collector UI**

In the `.collect` branch before the preflight text, add:

```swift
Text("仅供采集者本人在这台 Mac 扫码登录；请勿分享采集端或登录状态。")
    .foregroundStyle(.secondary)
Button("打开本地登录后台") {
    start { await model.openLocalLogin() }
}
.disabled(model.isBusy)
Divider()
```

Add to the root `NavigationSplitView` modifiers:

```swift
.onDisappear { model.stopLocalLogin() }
```

- [ ] **Step 6: Construct the concrete service in `InnoCollectorApp`**

Replace the `CollectorViewModel` construction with:

```swift
let localLogin = locations.mooreHelper.flatMap { helper in
    locations.exporterRuntime.map { runtime in
        MooreLocalLoginServer(
            executable: helper,
            pluginsDirectory: locations.helper.deletingLastPathComponent(),
            runtimeDirectory: runtime,
            supportRoot: locations.supportRoot
        )
    }
}
_model = StateObject(wrappedValue: CollectorViewModel(
    helper: HelperClient(executable: locations.helper),
    locations: locations,
    localLogin: localLogin
))
```

- [ ] **Step 7: Run all Swift tests**

Run:

```bash
./scripts/test_swift.sh
```

Expected: all Swift tests pass; Reader tests remain unchanged.

- [ ] **Step 8: Commit the application wiring**

```bash
git add macos/Sources/InnoCollectorFeature/CollectorViewModel.swift \
  macos/Tests/InnoCollectorAppTests/CollectorViewModelTests.swift \
  macos/Sources/InnoCollectorFeature/CollectorContentView.swift \
  macos/Sources/InnoCollectorApp/InnoCollectorApp.swift
git commit -m "feat: expose Collector login dashboard"
```

### Task 4: Verify repository and real App behavior

**Files:**
- No production-file changes expected.

- [ ] **Step 1: Run all repository checks**

```bash
/Users/yzy/Desktop/playground/inno-portfolio-collector/.venv/bin/python \
  -m unittest discover -s tests
./scripts/test_swift.sh
/Users/yzy/Desktop/playground/inno-portfolio-collector/.venv/bin/python \
  scripts/check_repository_policy.py
git diff --check
```

Expected: Python has zero failures with one expected skip, Swift has zero failures, policy passes, and `git diff --check` is silent.

- [ ] **Step 2: Build fresh release Apps**

```bash
rm -rf /tmp/inno-collector-login-release
/Users/yzy/Desktop/playground/inno-portfolio-collector/.venv/bin/python \
  scripts/build_macos_apps.py \
  --configuration release \
  --output /tmp/inno-collector-login-release/apps
```

Expected: `InnoCollector.app` and `InnoReader.app` are created.

Run the packaged role-isolation test against the actual helper binaries:

```bash
INNO_COLLECTOR_HELPER=/tmp/inno-collector-login-release/apps/InnoCollector.app/Contents/PlugIns/InnoCollectorHelper \
INNO_READER_HELPER=/tmp/inno-collector-login-release/apps/InnoReader.app/Contents/PlugIns/InnoReaderHelper \
  ./scripts/test_swift.sh --filter RoleIsolationTests
```

Expected: Collector and Reader report distinct roles, Reader refuses collection
material, and the packaged role-isolation test passes rather than skipping.

- [ ] **Step 3: Verify both App signatures and role permissions**

```bash
codesign --verify --deep --strict \
  /tmp/inno-collector-login-release/apps/InnoCollector.app
codesign --verify --deep --strict \
  /tmp/inno-collector-login-release/apps/InnoReader.app
codesign -d --entitlements :- \
  /tmp/inno-collector-login-release/apps/InnoCollector.app
codesign -d --entitlements :- \
  /tmp/inno-collector-login-release/apps/InnoReader.app
```

Expected: both verifications exit 0; Collector contains only `network.client=true`; Reader entitlements are empty.

- [ ] **Step 4: Verify bundle isolation and leak scans**

```bash
test -e /tmp/inno-collector-login-release/apps/InnoCollector.app/Contents/PlugIns/MooreExporterHelper
test ! -e /tmp/inno-collector-login-release/apps/InnoReader.app/Contents/PlugIns/MooreExporterHelper
test ! -e /tmp/inno-collector-login-release/apps/InnoReader.app/Contents/Resources/config/projects.json
test -n "$(strings /tmp/inno-collector-login-release/apps/InnoCollector.app/Contents/MacOS/InnoCollectorApp | rg '打开本地登录后台')"
test -z "$(strings /tmp/inno-collector-login-release/apps/InnoReader.app/Contents/MacOS/InnoReaderApp | rg '打开本地登录后台')"
! rg -a '/(Users|Volumes)/[^/[:cntrl:]]+/' /tmp/inno-collector-login-release/apps
! rg -a 'gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----' \
  /tmp/inno-collector-login-release/apps
```

Expected: all commands exit 0 with no leak matches.

- [ ] **Step 5: Launch the Collector and manually exercise the local login boundary**

Open the fresh Collector App, click “打开本地登录后台”, and verify:

1. the browser URL is exactly `http://127.0.0.1:18765/`;
2. the page shows the Moore local dashboard and local QR-login entry;
3. `lsof -nP -iTCP:18765 -sTCP:LISTEN` shows only `127.0.0.1:18765`;
4. closing Collector terminates the listener;
5. no QR scan is required for this structural check, so no new credential is created during automated review.

- [ ] **Step 6: Request focused code review**

Use `superpowers:requesting-code-review` against the branch commits. Require review of process ownership, port-conflict handling, path validation, error redaction, Reader isolation, and test evidence. Fix every blocking/high-priority finding with a new RED-GREEN cycle.

### Task 5: Build and reverse-audit the self-use Collector pilot DMG

**Files:**
- Create ignored artifact: `dist/自用试用/InnoCollector-0.1.0-pilot-20260712.dmg`
- Create ignored artifact: `dist/自用试用/SHA256SUMS.txt`
- Create ignored artifact: `dist/自用试用/安装说明.txt`

- [ ] **Step 1: Write the self-use installation instructions**

Create `dist/自用试用/安装说明.txt` with exactly this content using `apply_patch`:

```text
英诺资讯采集 Collector 0.1.0 自用试用版

仅供采集者本人使用，不得转发给客户。
本试用包使用 ad-hoc 签名，尚未经过 Apple Developer ID 正式签名和公证。
登录凭据只保存在本机 Keychain 和 Application Support；不得分享 Cookie、Token 或运行目录。
首次打开只使用“系统设置 → 隐私与安全性 → 仍要打开”，不得关闭 Gatekeeper。
先打开本地登录后台并扫码，再回到 Collector 运行预检；只有预检成功后才能采集。

安装步骤：
1. 双击 DMG，把“英诺资讯采集.app”拖到“应用程序”。
2. 首次打开如被 macOS 拦截，进入“系统设置 → 隐私与安全性”，只点击“仍要打开”。
3. 在“采集”页点击“打开本地登录后台”，确认浏览器地址为 http://127.0.0.1:18765/ 后扫码。
4. 回到 Collector 运行预检；预检成功后再开始采集。

卸载与清理：
1. 退出 Collector。
2. 删除“应用程序”中的“英诺资讯采集.app”。
3. 如需同时删除本机采集状态，再删除 ~/Library/Application Support/com.inno.news.collector/。
4. 如需清除登录凭据，在“钥匙串访问”中查找并删除本工具对应条目；不要导出或分享凭据。
```

- [ ] **Step 2: Stage only the Collector App and instructions**

```bash
rm -rf /tmp/inno-collector-pilot-stage
mkdir -p /tmp/inno-collector-pilot-stage dist/自用试用
ditto /tmp/inno-collector-login-release/apps/InnoCollector.app \
  '/tmp/inno-collector-pilot-stage/英诺资讯采集.app'
ditto dist/自用试用/安装说明.txt \
  /tmp/inno-collector-pilot-stage/安装说明.txt
```

Expected: the stage contains exactly the App and instructions; it contains no runtime directory.

- [ ] **Step 3: Create the compressed DMG**

```bash
hdiutil create \
  -volname '英诺资讯采集 0.1.0 自用试用版' \
  -srcfolder /tmp/inno-collector-pilot-stage \
  -format UDZO \
  dist/自用试用/InnoCollector-0.1.0-pilot-20260712.dmg
```

Expected: `hdiutil` reports the DMG path and exits 0.

- [ ] **Step 4: Generate and verify SHA-256**

Compute the hash:

```bash
cd dist/自用试用
shasum -a 256 InnoCollector-0.1.0-pilot-20260712.dmg
```

Copy the command's complete output line unchanged into a new
`SHA256SUMS.txt` using `apply_patch`; do not generate this tracked-independent
artifact with shell redirection. Then run:

```bash
shasum -a 256 -c SHA256SUMS.txt
```

Expected: `InnoCollector-0.1.0-pilot-20260712.dmg: OK`.

- [ ] **Step 5: Mount and reverse-audit the final DMG**

```bash
rm -rf /tmp/inno-collector-pilot-mount
mkdir -p /tmp/inno-collector-pilot-mount
hdiutil verify dist/自用试用/InnoCollector-0.1.0-pilot-20260712.dmg
hdiutil attach -readonly -nobrowse \
  -mountpoint /tmp/inno-collector-pilot-mount \
  dist/自用试用/InnoCollector-0.1.0-pilot-20260712.dmg
```

Verify exactly two top-level entries, deep signature structure, required
Collector/Moore helpers, `projects.json`, all four legal documents, Collector
network entitlement, no Reader helper, no runtime/login files, no
`.superpowers`/user source list, no real articles, no local paths, and no
high-confidence secrets:

```bash
test "$(find /tmp/inno-collector-pilot-mount -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" = 2
test -d '/tmp/inno-collector-pilot-mount/英诺资讯采集.app'
test -f /tmp/inno-collector-pilot-mount/安装说明.txt
codesign --verify --deep --strict \
  '/tmp/inno-collector-pilot-mount/英诺资讯采集.app'
test -x '/tmp/inno-collector-pilot-mount/英诺资讯采集.app/Contents/PlugIns/InnoCollectorHelper'
test -x '/tmp/inno-collector-pilot-mount/英诺资讯采集.app/Contents/PlugIns/MooreExporterHelper'
test -f '/tmp/inno-collector-pilot-mount/英诺资讯采集.app/Contents/Resources/config/projects.json'
for name in \
  inno-news-suite-LICENSE.txt \
  wechat-article-exporter-LICENSE.txt \
  moore-wechat-article-downloader-LICENSE.txt \
  THIRD_PARTY_NOTICES.md; do
  test -f "/tmp/inno-collector-pilot-mount/英诺资讯采集.app/Contents/Resources/ThirdPartyLicenses/$name"
done
codesign -d --entitlements :- \
  '/tmp/inno-collector-pilot-mount/英诺资讯采集.app' \
  >/tmp/inno-collector-entitlements.plist 2>/dev/null
rg -q '<key>com.apple.security.network.client</key>' \
  /tmp/inno-collector-entitlements.plist
test -z "$(find /tmp/inno-collector-pilot-mount \
  \( -iname '*reader*' -o -iname '*runtime*' -o -iname '*cookie*' \
     -o -iname '*token*' -o -iname '.superpowers' \) -print)"
test -z "$(find /tmp/inno-collector-pilot-mount \
  \( -path '*/02-项目/*' -o -path '*/04-附件/*' -o -iname '*.xlsx' \
     -o -iname '*.zip' \) -print)"
! rg -a '/(Users|Volumes)/[^/[:cntrl:]]+/' /tmp/inno-collector-pilot-mount
! rg -a 'gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----' \
  /tmp/inno-collector-pilot-mount
```

Detach with:

```bash
hdiutil detach /tmp/inno-collector-pilot-mount
```

- [ ] **Step 6: Confirm this remains a local pilot, not a formal release**

```bash
test -z "$(git tag --list)"
test -z "$(gh release list --limit 1)"
git status --short --branch
```

Expected: no tag, no GitHub Release, and no tracked DMG/dist changes.

### Task 6: Integrate after review and CI

**Files:**
- Update only files required by code-review findings.

- [ ] **Step 1: Run fresh final verification**

Run Python tests, Swift tests, repository policy, `git diff --check`, real App build checks, and the mounted DMG audit again. Do not rely on earlier outputs.

- [ ] **Step 2: Push the feature branch and create a PR**

```bash
git push -u origin feat/collector-local-login
gh pr create \
  --base main \
  --head feat/collector-local-login \
  --title 'Add Collector local login dashboard' \
  --body $'## Summary\n- add a Collector-only localhost login dashboard bound to 127.0.0.1:18765\n- validate bundled helper and Application Support runtime boundaries before launch\n- keep Reader free of the Moore helper and collection configuration\n\n## Verification\n- Python, Swift, and repository-policy suites pass\n- release Apps pass codesign, entitlement, isolation, path, and secret checks\n- local Collector pilot DMG passes mount-and-reverse audit\n\nThe ad-hoc Collector pilot DMG remains local and is not uploaded to this PR.'
```

- [ ] **Step 3: Wait for all PR CI jobs to pass**

```bash
gh pr checks --watch --interval 10
```

Expected: repository-policy, python-tests, and swift-tests all pass.

- [ ] **Step 4: Merge using the previously approved automatic workflow**

```bash
gh pr merge --squash --delete-branch
```

Then watch the resulting `main` push CI and confirm the local main checkout is clean and equal to `origin/main`.

- [ ] **Step 5: Record pilot progress without closing formal release issues**

Comment on Issue #4 with the local Collector pilot filename, SHA-256, tests, and explicit non-public/ad-hoc status. Keep Issues #1, #2, and #4 open until formal signing, clean-account acceptance, and public release are completed.
