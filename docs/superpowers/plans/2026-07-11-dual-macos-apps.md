# Dual macOS Apps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build separate installable collector and reader/editor macOS applications that use the phase-one JSON helpers while keeping collection code and credentials out of the reader product.

**Architecture:** A Swift Package contains two thin SwiftUI executable targets, two testable feature libraries, and one shared library for request framing, models, file selection, and process management. Each application receives a different bundled helper at release time. View models depend on a `HelperCalling` protocol, allowing deterministic tests without launching Python.

**Tech Stack:** Swift 6.3, SwiftUI, Foundation, Swift Package Manager, XCTest/Swift Testing, phase-one Python helpers.

---

**Prerequisite:** Complete `docs/superpowers/plans/2026-07-11-content-package-core.md` and keep its full Python suite green.

## File map

- Create `macos/Package.swift`: shared library and two executable products.
- Create `macos/Sources/InnoAppCore/HelperModels.swift`: Codable helper envelopes and product models.
- Create `macos/Sources/InnoAppCore/HelperClient.swift`: safe subprocess invocation with timeout and bounded output.
- Create `macos/Sources/InnoAppCore/FileLocations.swift`: Application Support, Vault, inbox, and helper path resolution.
- Create `macos/Sources/InnoCollectorFeature/CollectorViewModel.swift` and `CollectorContentView.swift`, plus thin entry `macos/Sources/InnoCollectorApp/InnoCollectorApp.swift`.
- Create `macos/Sources/InnoReaderFeature/ReaderViewModel.swift`, `ReaderContentView.swift`, `LibraryIndex.swift`, and `ObsidianLauncher.swift`, plus thin entry `macos/Sources/InnoReaderApp/InnoReaderApp.swift`.
- Create `macos/Tests/InnoAppCoreTests/HelperClientTests.swift` and `FileLocationsTests.swift`.
- Create `macos/Tests/InnoCollectorAppTests/CollectorViewModelTests.swift`.
- Create `macos/Tests/InnoReaderAppTests/ReaderViewModelTests.swift`, `LibraryIndexTests.swift`, and `ObsidianLauncherTests.swift`.

### Task 1: Scaffold a testable dual-app Swift Package

**Files:**
- Create: `macos/Package.swift`
- Create: `macos/Sources/InnoAppCore/HelperModels.swift`
- Create: `macos/Tests/InnoAppCoreTests/HelperModelsTests.swift`

- [ ] **Step 1: Create the package manifest**

```swift
// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "InnoNewsSuite",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "InnoAppCore", targets: ["InnoAppCore"]),
        .executable(name: "InnoCollectorApp", targets: ["InnoCollectorApp"]),
        .executable(name: "InnoReaderApp", targets: ["InnoReaderApp"]),
    ],
    targets: [
        .target(name: "InnoAppCore"),
        .target(name: "InnoCollectorFeature", dependencies: ["InnoAppCore"]),
        .target(name: "InnoReaderFeature", dependencies: ["InnoAppCore"]),
        .executableTarget(name: "InnoCollectorApp", dependencies: ["InnoCollectorFeature", "InnoAppCore"]),
        .executableTarget(name: "InnoReaderApp", dependencies: ["InnoReaderFeature", "InnoAppCore"]),
        .testTarget(name: "InnoAppCoreTests", dependencies: ["InnoAppCore"]),
        .testTarget(name: "InnoCollectorAppTests", dependencies: ["InnoCollectorFeature", "InnoAppCore"]),
        .testTarget(name: "InnoReaderAppTests", dependencies: ["InnoReaderFeature", "InnoAppCore"]),
    ]
)
```

- [ ] **Step 2: Write failing Codable-envelope tests**

```swift
import XCTest
@testable import InnoAppCore

final class HelperModelsTests: XCTestCase {
    func testRequestUsesStableKeys() throws {
        let request = HelperRequest(id: "r1", command: "status", arguments: [:])
        let object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any]
        XCTAssertEqual(Set(object?.keys.map { $0 } ?? []), Set(["id", "command", "arguments"]))
    }

    func testFailureResponseDecodesWithoutResult() throws {
        let data = #"{"error":"bad package","id":"r1","ok":false}"#.data(using: .utf8)!
        let response = try JSONDecoder().decode(HelperResponse.self, from: data)
        XCTAssertFalse(response.ok)
        XCTAssertEqual(response.error, "bad package")
    }
}
```

- [ ] **Step 3: Run and confirm missing types**

Run: `cd macos && swift test --filter HelperModelsTests`

Expected: FAIL because `HelperRequest` and `HelperResponse` do not exist.

- [ ] **Step 4: Add Sendable Codable models**

```swift
public enum JSONValue: Codable, Equatable, Sendable {
    case string(String), integer(Int), boolean(Bool), array([JSONValue]), object([String: JSONValue]), null
}

public struct HelperRequest: Codable, Equatable, Sendable {
    public let id: String
    public let command: String
    public let arguments: [String: JSONValue]

    public init(id: String, command: String, arguments: [String: JSONValue]) {
        self.id = id
        self.command = command
        self.arguments = arguments
    }
}

public struct HelperResponse: Codable, Equatable, Sendable {
    public let id: String
    public let ok: Bool
    public let result: [String: JSONValue]?
    public let error: String?
}
```

Implement `JSONValue.init(from:)` by trying a keyed container, unkeyed container, `Bool`, `Int`, and `String` in that order, then `decodeNil`; implement the symmetric encoder. Reject floating-point values because the Python protocol does not need them.

- [ ] **Step 5: Run tests and commit**

Run: `cd macos && swift test --filter HelperModelsTests`

Expected: PASS.

```bash
git add macos/Package.swift macos/Sources/InnoAppCore/HelperModels.swift macos/Tests/InnoAppCoreTests/HelperModelsTests.swift
git commit -m "feat: scaffold dual macOS app package"
```

### Task 2: Implement bounded helper process invocation

**Files:**
- Create: `macos/Sources/InnoAppCore/HelperClient.swift`
- Create: `macos/Tests/InnoAppCoreTests/HelperClientTests.swift`

- [ ] **Step 1: Write failing success, failure, timeout, and output-limit tests**

Use temporary executable scripts that read stdin and emit one JSON line. Require stderr never to appear in a user-facing error, require mismatched response IDs to fail, terminate a sleeping process after 300 seconds in production and 0.1 seconds in the test, and reject stdout over 8 MiB.

```swift
func testMismatchedResponseIDIsRejected() async throws {
    let client = HelperClient(executable: try fixture("reply-wrong-id"), timeout: 1)
    do {
        _ = try await client.call(command: "status", arguments: [:])
        XCTFail("expected protocol error")
    } catch let error as HelperClientError {
        XCTAssertEqual(error, .responseIDMismatch)
    }
}
```

- [ ] **Step 2: Run and confirm the client is missing**

Run: `cd macos && swift test --filter HelperClientTests`

Expected: FAIL because `HelperClient` does not exist.

- [ ] **Step 3: Add the protocol and error surface**

```swift
public protocol HelperCalling: Sendable {
    func call(command: String, arguments: [String: JSONValue]) async throws -> [String: JSONValue]
}

public enum HelperClientError: Error, Equatable {
    case launchFailed
    case timedOut
    case outputTooLarge
    case invalidResponse
    case responseIDMismatch
    case helperFailure(String)
}
```

- [ ] **Step 4: Implement `HelperClient` as an actor**

Create a UUID request ID, encode one `HelperRequest`, launch the configured executable with no shell, write the request bytes to stdin, collect stdout into a bounded pipe, discard bounded stderr after process exit, race termination against the timeout, decode exactly one JSON object, require the same ID, and return `result` only when `ok == true`. Convert all launch details to the stable errors above so local paths and arguments never reach the UI.

- [ ] **Step 5: Run tests and commit**

Run: `cd macos && swift test --filter HelperClientTests`

Expected: PASS.

```bash
git add macos/Sources/InnoAppCore/HelperClient.swift macos/Tests/InnoAppCoreTests/HelperClientTests.swift
git commit -m "feat: call local helpers from macOS apps"
```

### Task 3: Define safe application file locations

**Files:**
- Create: `macos/Sources/InnoAppCore/FileLocations.swift`
- Create: `macos/Tests/InnoAppCoreTests/FileLocationsTests.swift`

- [ ] **Step 1: Write failing location tests**

Assert that collector state, reader state, Vaults, inbox, and helpers live under separate product-specific Application Support roots and that no default path points to the source checkout, `~/.moore`, Desktop, or Downloads.

- [ ] **Step 2: Run and confirm missing locations**

Run: `cd macos && swift test --filter FileLocationsTests`

Expected: FAIL.

- [ ] **Step 3: Add immutable locations**

```swift
public struct AppLocations: Equatable, Sendable {
    public let supportRoot: URL
    public let vault: URL
    public let inbox: URL
    public let helper: URL
    public let projectsConfig: URL?

    public static func collector(fileManager: FileManager = .default, bundle: Bundle = .main) throws -> Self
    public static func reader(fileManager: FileManager = .default, bundle: Bundle = .main) throws -> Self
}
```

Use bundle identifiers `com.inno.news.collector` and `com.inno.news.reader`; resolve helpers only from `Bundle.main.builtInPlugInsURL`; standardize URLs and reject any helper URL escaping that directory. Collector locations resolve `Resources/config/projects.json` as `projectsConfig`; reader locations set it to `nil` and fail a test if that resource exists in the reader bundle.

- [ ] **Step 4: Run tests and commit**

Run: `cd macos && swift test --filter FileLocationsTests`

Expected: PASS.

```bash
git add macos/Sources/InnoAppCore/FileLocations.swift macos/Tests/InnoAppCoreTests/FileLocationsTests.swift
git commit -m "feat: isolate collector and reader app data"
```

### Task 4: Build the collector application vertical slice

**Files:**
- Create: `macos/Sources/InnoCollectorApp/InnoCollectorApp.swift`
- Create: `macos/Sources/InnoCollectorFeature/CollectorViewModel.swift`
- Create: `macos/Sources/InnoCollectorFeature/CollectorContentView.swift`
- Create: `macos/Tests/InnoCollectorAppTests/CollectorViewModelTests.swift`

- [ ] **Step 1: Write failing view-model tests**

Use a recording fake `HelperCalling`. Cover initial `status`, dry-run preflight, explicit collection, update-package creation, draft receipt, cancellation, disabled buttons while busy, and sanitized error display. Assert collection cannot start until the most recent preflight succeeded.

- [ ] **Step 2: Run and confirm missing collector types**

Run: `cd macos && swift test --filter CollectorViewModelTests`

Expected: FAIL.

- [ ] **Step 3: Add collector state and actions**

```swift
@MainActor
public final class CollectorViewModel: ObservableObject {
    @Published public private(set) var summary: CollectorSummary?
    @Published public private(set) var isBusy = false
    @Published public private(set) var lastPreflightSucceeded = false
    @Published public private(set) var errorMessage: String?

    private let helper: HelperCalling

    public init(helper: HelperCalling) { self.helper = helper }
    public func refresh() async
    public func preflight() async
    public func collect() async
    public func buildUpdate(destination: URL, basePackage: URL?) async
    public func receiveDrafts(package: URL) async
}
```

Implement every action through one private `perform` method that sets `isBusy` with `defer`, clears the previous error, calls the helper, and maps only stable returned fields into `CollectorSummary`. Pass the byte-preserved bundled `projectsConfig` path in preflight and collection requests. `collect()` must return immediately with a visible validation message when `lastPreflightSucceeded` is false.

- [ ] **Step 4: Add the SwiftUI shell**

`CollectorContentView` in `InnoCollectorFeature` uses a sidebar with `概览`, `采集`, `资料库`, `交付`, and `稿件收件箱`. The first implementation wires status cards, preflight/collect buttons, package save panel, and draft open panel; the read-only library list can show helper-provided rows. Do not add cloud, account, or publishing controls.

`InnoCollectorApp` creates `AppLocations.collector()`, constructs a `HelperClient` for the bundled collector helper, and injects one `CollectorViewModel` as a `StateObject`.

- [ ] **Step 5: Run tests and a local launch smoke test**

Run: `cd macos && swift test --filter CollectorViewModelTests`

Expected: PASS.

Run: `cd macos && swift run InnoCollectorApp`

Expected: the collector window opens; with no bundled helper it shows a stable unavailable message and does not crash.

- [ ] **Step 6: Commit**

```bash
git add macos/Sources/InnoCollectorFeature macos/Sources/InnoCollectorApp macos/Tests/InnoCollectorAppTests
git commit -m "feat: add collector macOS application"
```

### Task 5: Build the reader library, update, and dashboard flows

**Files:**
- Create: `macos/Sources/InnoReaderApp/InnoReaderApp.swift`
- Create: `macos/Sources/InnoReaderFeature/ReaderViewModel.swift`
- Create: `macos/Sources/InnoReaderFeature/ReaderContentView.swift`
- Create: `macos/Sources/InnoReaderFeature/LibraryIndex.swift`
- Create: `macos/Tests/InnoReaderAppTests/ReaderViewModelTests.swift`
- Create: `macos/Tests/InnoReaderAppTests/LibraryIndexTests.swift`

- [ ] **Step 1: Write failing reader tests**

Cover baseline import, incremental preview, apply confirmation, version mismatch, library search by title/project/account, stable ordering by publish date, dashboard file opening, and the absence of any `collect` request.

```swift
func testReaderNeverUsesCollectorCommand() async {
    let helper = RecordingHelper()
    let model = ReaderViewModel(helper: helper, locations: locations)
    await model.refresh()
    await model.previewUpdate(package: packageURL)
    await model.applyPreviewedUpdate()
    XCTAssertFalse(helper.commands.contains("collect"))
}
```

- [ ] **Step 2: Run and confirm missing reader types**

Run: `cd macos && swift test --filter 'ReaderViewModelTests|LibraryIndexTests'`

Expected: FAIL.

- [ ] **Step 3: Add a read-only library index**

`LibraryIndex.load(vault:)` reads `90-系统/manifest.json`, accepts only records with strict stable IDs and safe relative article paths, and exposes immutable `LibraryArticle` values. Search is case- and width-insensitive over title, project, and account. Opening an article standardizes the resolved URL and rejects any path outside the Vault.

- [ ] **Step 4: Add reader update state**

```swift
@MainActor
public final class ReaderViewModel: ObservableObject {
    @Published public private(set) var articles: [LibraryArticle] = []
    @Published public private(set) var updatePreview: UpdatePreview?
    @Published public private(set) var isBusy = false
    @Published public private(set) var errorMessage: String?

    public func refresh() async
    public func previewUpdate(package: URL) async
    public func applyPreviewedUpdate() async
    public func rebuildDashboard() async
}
```

Keep the selected package only after a successful preview. Require an explicit user action for apply. Refresh the library only after the helper reports success.

- [ ] **Step 5: Add the reader SwiftUI shell**

Use sidebar items `阅读`, `看板`, `编辑`, `更新`, and `Obsidian`. The reading screen has search and project filters; the update screen has an open panel, a human-readable diff, and an apply button; the dashboard opens the local `80-离线看板/index.html` through `NSWorkspace` and never embeds remote web content.

- [ ] **Step 6: Run tests and commit**

Run: `cd macos && swift test --filter 'ReaderViewModelTests|LibraryIndexTests'`

Expected: PASS.

```bash
git add macos/Sources/InnoReaderFeature macos/Sources/InnoReaderApp macos/Tests/InnoReaderAppTests
git commit -m "feat: add reader and update macOS flows"
```

### Task 6: Add independent editing, draft export, and Obsidian launch

**Files:**
- Modify: `macos/Sources/InnoReaderFeature/ReaderViewModel.swift`
- Modify: `macos/Sources/InnoReaderFeature/ReaderContentView.swift`
- Create: `macos/Sources/InnoReaderFeature/ObsidianLauncher.swift`
- Modify: `macos/Tests/InnoReaderAppTests/ReaderViewModelTests.swift`
- Create: `macos/Tests/InnoReaderAppTests/ObsidianLauncherTests.swift`

- [ ] **Step 1: Write failing editing-boundary tests**

Assert that creating a draft writes only through the reader helper into `10-编辑稿`, exporting calls only `build_drafts`, and neither action accepts a destination inside `03-文章` or `04-附件`. Test an Obsidian-installed URL open and a not-installed fallback.

- [ ] **Step 2: Run and confirm failures**

Run: `cd macos && swift test --filter 'ReaderViewModelTests|ObsidianLauncherTests'`

Expected: FAIL for missing draft actions and launcher.

- [ ] **Step 3: Add draft actions**

Add `createDraft(from article: LibraryArticle, kind: DraftKind) async` and `exportDrafts(ids: [String], destination: URL) async`. `DraftKind` is exactly `note`, `summary`, `pitch`, or `edit`; the helper creates strict frontmatter and the Swift layer never writes source files directly.

- [ ] **Step 4: Add the Obsidian launcher**

```swift
public struct ObsidianLauncher {
    public let workspace: NSWorkspace

    public func open(vault: URL) -> Bool {
        guard workspace.urlForApplication(toOpen: URL(string: "obsidian://open")!) != nil else {
            return false
        }
        var components = URLComponents(string: "obsidian://open")!
        components.queryItems = [URLQueryItem(name: "path", value: vault.path)]
        return components.url.map(workspace.open) ?? false
    }
}
```

When `open` returns false, show installation guidance and keep all reader functions usable.

- [ ] **Step 5: Run all Swift tests and commit**

Run: `cd macos && swift test`

Expected: all Swift tests PASS.

```bash
git add macos/Sources/InnoReaderFeature macos/Tests/InnoReaderAppTests
git commit -m "feat: add reader drafts and Obsidian handoff"
```

### Task 7: Verify role isolation with executable fixtures

**Files:**
- Modify: `macos/Tests/InnoCollectorAppTests/CollectorViewModelTests.swift`
- Modify: `macos/Tests/InnoReaderAppTests/ReaderViewModelTests.swift`
- Create: `macos/Tests/InnoAppCoreTests/RoleIsolationTests.swift`

- [ ] **Step 1: Add real-helper integration tests gated by environment variables**

Read `INNO_COLLECTOR_HELPER` and `INNO_READER_HELPER`. When present, launch the real binaries, require both `status` calls to succeed, require the reader binary to reject `collect`, and scan the reader binary's adjacent bundle files to ensure none are named `wechat_exporter.py`, `collector_helper`, `cookies.sqlite`, or `projects.json`.

- [ ] **Step 2: Run all Python and Swift tests**

Run: `./.venv/bin/python -m unittest discover -s tests -q`

Expected: all Python tests PASS.

Run: `cd macos && swift test`

Expected: all Swift tests PASS; real-helper tests skip only when the two environment variables are absent.

- [ ] **Step 3: Commit**

```bash
git add macos/Tests
git commit -m "test: enforce macOS app role isolation"
```

## Phase-two acceptance gate

Accept this phase only when `swift test` passes, both executables launch, the collector workflow requires successful preflight, the reader can preview/apply packages and manage drafts, all source path resolution is confined to the Vault, and no reader code path or bundled fixture can invoke collection.
