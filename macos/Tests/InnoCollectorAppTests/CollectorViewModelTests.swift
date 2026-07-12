import Foundation
import Testing
import InnoAppCore
@testable import InnoCollectorFeature

private actor RecordingHelper: HelperCalling {
    struct Call: Equatable, Sendable {
        let command: String
        let arguments: [String: JSONValue]
    }

    private var calls: [Call] = []
    private let responses: [String: [String: JSONValue]]
    private let delay: Duration?
    private let failure: HelperClientError?

    init(
        responses: [String: [String: JSONValue]] = [:],
        delay: Duration? = nil,
        failure: HelperClientError? = nil
    ) {
        self.responses = responses
        self.delay = delay
        self.failure = failure
    }

    func call(command: String, arguments: [String: JSONValue]) async throws -> [String: JSONValue] {
        calls.append(Call(command: command, arguments: arguments))
        if let delay { try await Task.sleep(for: delay) }
        if let failure { throw failure }
        return responses[command] ?? [:]
    }

    func recordedCalls() -> [Call] { calls }
}

@MainActor
private final class RecordingLoginService: LocalLoginServing {
    private(set) var openCount = 0
    private(set) var stopCount = 0
    var error: LocalLoginError?
    var cancellation = false

    func open() async throws {
        openCount += 1
        if cancellation { throw CancellationError() }
        if let error { throw error }
    }

    func stop() {
        stopCount += 1
    }
}

@Suite("Collector view model")
@MainActor
struct CollectorViewModelTests {
    private let locations = try! AppLocations.resolve(
        role: .collector,
        applicationSupport: URL(fileURLWithPath: "/tmp/Application Support", isDirectory: true),
        bundleURL: URL(fileURLWithPath: "/Applications/Collector.app", isDirectory: true)
    )

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

    @Test("reports an unavailable login service without starting work")
    func unavailableLocalLogin() async {
        let model = CollectorViewModel(
            helper: RecordingHelper(),
            locations: locations
        )

        await model.openLocalLogin()

        #expect(model.errorMessage == "本地登录后台不可用。")
        #expect(!model.isBusy)
    }

    @Test(
        "maps remaining local login failures",
        arguments: [
            (LocalLoginError.unavailable, "本地登录后台不可用。"),
            (LocalLoginError.browserUnavailable, "请在浏览器打开 http://127.0.0.1:18765/。"),
            (LocalLoginError.launchFailed, "本地登录后台启动失败。"),
            (LocalLoginError.notReady, "本地登录后台启动失败。"),
        ]
    )
    func remainingLocalLoginErrors(error: LocalLoginError, message: String) async {
        let login = RecordingLoginService()
        login.error = error
        let model = CollectorViewModel(
            helper: RecordingHelper(),
            locations: locations,
            localLogin: login
        )

        await model.openLocalLogin()

        #expect(model.errorMessage == message)
        #expect(!model.isBusy)
    }

    @Test("cancelling local login stops its service without showing an error")
    func cancelledLocalLogin() async {
        let login = RecordingLoginService()
        login.cancellation = true
        let model = CollectorViewModel(
            helper: RecordingHelper(),
            locations: locations,
            localLogin: login
        )

        await model.openLocalLogin()

        #expect(login.openCount == 1)
        #expect(login.stopCount == 1)
        #expect(model.errorMessage == nil)
        #expect(!model.isBusy)
    }

    @Test("refresh maps stable summary fields")
    func refresh() async {
        let helper = RecordingHelper(responses: [
            "status": [
                "vault_exists": .boolean(true),
                "report": .object([
                    "article_count": .integer(225),
                    "project_count": .integer(10),
                    "failed_projects": .integer(8),
                ]),
            ],
            "list_received_drafts": ["receipts": .array([])],
        ])
        let model = CollectorViewModel(helper: helper, locations: locations)

        await model.refresh()

        #expect(model.summary == CollectorSummary(articleCount: 225, projectCount: 10, failedProjects: 8))
        #expect(model.errorMessage == nil)
    }

    @Test("refresh restores validated pending draft receipts")
    func refreshRestoresReceipts() async {
        let receipt = locations.inbox.appendingPathComponent("receipt-restored", isDirectory: true)
        let helper = RecordingHelper(responses: [
            "status": ["vault_exists": .boolean(false)],
            "list_received_drafts": [
                "receipts": .array([
                    .object([
                        "receipt_path": .string(receipt.path),
                        "draft_count": .integer(2),
                    ]),
                ]),
            ],
        ])
        let model = CollectorViewModel(helper: helper, locations: locations)

        await model.refresh()

        #expect(model.receivedDrafts == [
            ReceivedDraft(receipt: receipt, draftCount: 2, alreadyReceived: true),
        ])
        #expect(await helper.recordedCalls().map(\.command) == ["status", "list_received_drafts"])
    }

    @Test("collection requires a successful latest preflight")
    func preflightGate() async {
        let helper = RecordingHelper(responses: [
            "collect": ["article_count": .integer(0), "project_count": .integer(10), "failed_projects": .integer(0)],
        ])
        let model = CollectorViewModel(helper: helper, locations: locations)

        await model.collect()
        #expect(model.errorMessage == "请先完成成功的采集预检。")
        #expect(await helper.recordedCalls().isEmpty)

        await model.preflight()
        #expect(model.lastPreflightSucceeded)
        await model.collect()
        let calls = await helper.recordedCalls()
        #expect(calls.map(\.command) == ["collect", "collect"])
        #expect(calls[0].arguments["dry_run"] == .boolean(true))
        #expect(calls[1].arguments["dry_run"] == .boolean(false))
        guard case .string(let runtime) = calls[0].arguments["runtime"] else {
            Issue.record("collection runtime is missing")
            return
        }
        #expect(
            model.locations.vault
                == URL(fileURLWithPath: runtime, isDirectory: true)
                    .appendingPathComponent("vault/英诺被投项目资讯库", isDirectory: true)
        )
    }

    @Test("delivery and inbox actions use role paths")
    func deliveryAndInbox() async {
        let receiptURL = locations.inbox.appendingPathComponent("receipt-1", isDirectory: true)
        let helper = RecordingHelper(responses: [
            "receive_drafts": [
                "receipt_path": .string(receiptURL.path),
                "draft_count": .integer(1),
                "existing": .boolean(false),
            ],
            "accept_draft": [
                "created": .integer(1),
                "unchanged": .integer(0),
                "conflicts": .integer(0),
                "draft_count": .integer(1),
            ],
        ])
        let model = CollectorViewModel(helper: helper, locations: locations)
        let output = URL(fileURLWithPath: "/tmp/update.inno-update")
        let base = URL(fileURLWithPath: "/tmp/base.inno-update")
        let draft = URL(fileURLWithPath: "/tmp/drafts.inno-drafts")

        await model.buildUpdate(destination: output, basePackage: nil)
        await model.buildUpdate(destination: output, basePackage: base)
        await model.receiveDrafts(package: draft)
        let receipt = try! #require(model.receivedDrafts.first)
        await model.acceptDraft(receipt: receipt)

        let calls = await helper.recordedCalls()
        #expect(calls.map(\.command) == [
            "build_update", "build_update", "receive_drafts", "accept_draft",
        ])
        #expect(calls[0].arguments["output"] == .string(output.path))
        #expect(calls[0].arguments["base_package"] == nil)
        #expect(calls[1].arguments["base_package"] == .string(base.path))
        #expect(calls[2].arguments["inbox"] == .string(locations.inbox.path))
        #expect(calls[3].arguments["receipt"] == .string(receiptURL.path))
        #expect(calls[3].arguments["vault"] == .string(locations.vault.path))
        #expect(model.receivedDrafts.isEmpty)
    }

    @Test("busy state and stable error are visible")
    func busyAndError() async {
        let slow = RecordingHelper(delay: .milliseconds(100))
        let model = CollectorViewModel(helper: slow, locations: locations)
        let task = Task { await model.refresh() }
        await Task.yield()
        #expect(model.isBusy)
        await task.value
        #expect(!model.isBusy)

        let failing = RecordingHelper(failure: .helperFailure("bad package"))
        let failingModel = CollectorViewModel(helper: failing, locations: locations)
        await failingModel.refresh()
        #expect(failingModel.errorMessage == "bad package")
    }

    @Test("cancellation clears busy state without showing an error")
    func cancellation() async throws {
        let helper = RecordingHelper(delay: .seconds(5))
        let model = CollectorViewModel(helper: helper, locations: locations)
        let task = Task { await model.refresh() }
        await Task.yield()
        #expect(model.isBusy)

        task.cancel()
        await task.value

        #expect(!model.isBusy)
        #expect(model.errorMessage == nil)
    }

    @Test(
        "real collector helper reports its role",
        .enabled(
            if: ProcessInfo.processInfo.environment["INNO_COLLECTOR_HELPER"] != nil,
            "requires INNO_COLLECTOR_HELPER"
        )
    )
    func realCollectorHelper() async throws {
        let path = try #require(ProcessInfo.processInfo.environment["INNO_COLLECTOR_HELPER"])
        let helper = HelperClient(
            executable: URL(fileURLWithPath: path).standardizedFileURL,
            timeout: 60
        )

        let result = try await helper.call(command: "status", arguments: [:])

        #expect(result["role"] == .string("collector"))
    }
}
