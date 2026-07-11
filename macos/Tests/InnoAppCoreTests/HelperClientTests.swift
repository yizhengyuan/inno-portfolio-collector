import Foundation
import Testing
@testable import InnoAppCore

@Suite("Helper client")
struct HelperClientTests {
    private func fixture(_ body: String) throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let script = directory.appendingPathComponent("helper.py")
        let source = "#!/usr/bin/env python3\nimport json,sys,time\nrequest=json.load(sys.stdin)\n" + body + "\n"
        try source.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return script
    }

    @Test("successful helper result is returned")
    func success() async throws {
        let executable = try fixture(
            "print(json.dumps({'id':request['id'],'ok':True,'result':{'value':'好'}}))"
        )
        let client = HelperClient(executable: executable, timeout: 10)

        let result = try await client.call(command: "status", arguments: [:])

        #expect(result == ["value": .string("好")])
    }

    @Test("helper failure ignores stderr")
    func helperFailureIgnoresStderr() async throws {
        let executable = try fixture(
            "print('auth-key=stderr-secret',file=sys.stderr);print(json.dumps({'id':request['id'],'ok':False,'error':'bad package'}))"
        )
        let client = HelperClient(executable: executable, timeout: 10)

        do {
            _ = try await client.call(command: "status", arguments: [:])
            Issue.record("expected helper failure")
        } catch let error as HelperClientError {
            #expect(error == .helperFailure("bad package"))
        }
    }

    @Test("mismatched response ID is rejected")
    func mismatchedResponseID() async throws {
        let executable = try fixture(
            "print(json.dumps({'id':'wrong','ok':True,'result':{}}))"
        )
        let client = HelperClient(executable: executable, timeout: 10)

        do {
            _ = try await client.call(command: "status", arguments: [:])
            Issue.record("expected response ID mismatch")
        } catch let error as HelperClientError {
            #expect(error == .responseIDMismatch)
        }
    }

    @Test("helper timeout terminates the process")
    func timeout() async throws {
        let executable = try fixture("time.sleep(5)")
        let client = HelperClient(executable: executable, timeout: 0.1)

        do {
            _ = try await client.call(command: "status", arguments: [:])
            Issue.record("expected timeout")
        } catch let error as HelperClientError {
            #expect(error == .timedOut)
        }
    }

    @Test("stdout above the configured bound is rejected")
    func outputLimit() async throws {
        let executable = try fixture("print('x'*256)")
        let client = HelperClient(executable: executable, timeout: 10, maxOutputBytes: 64)

        do {
            _ = try await client.call(command: "status", arguments: [:])
            Issue.record("expected output limit")
        } catch let error as HelperClientError {
            #expect(error == .outputTooLarge)
        }
    }

    @Test("cancelling a call terminates the helper")
    func cancellation() async throws {
        let executable = try fixture("time.sleep(5)")
        let client = HelperClient(executable: executable, timeout: 10)
        let task = Task {
            try await client.call(command: "status", arguments: [:])
        }
        try await Task.sleep(for: .milliseconds(100))

        task.cancel()

        do {
            _ = try await task.value
            Issue.record("expected cancellation")
        } catch is CancellationError {
            // Expected: cancellation is distinct from timeout or helper failure.
        }
    }
}
