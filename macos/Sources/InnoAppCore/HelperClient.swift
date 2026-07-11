import Foundation

public protocol HelperCalling: Sendable {
    func call(
        command: String,
        arguments: [String: JSONValue]
    ) async throws -> [String: JSONValue]
}

public enum HelperClientError: Error, Equatable, Sendable {
    case launchFailed
    case timedOut
    case outputTooLarge
    case invalidResponse
    case responseIDMismatch
    case helperFailure(String)
}

public actor HelperClient: HelperCalling {
    private let executable: URL
    private let timeout: TimeInterval
    private let maxOutputBytes: Int

    public init(
        executable: URL,
        timeout: TimeInterval = 300,
        maxOutputBytes: Int = 8 * 1024 * 1024
    ) {
        self.executable = executable
        self.timeout = timeout
        self.maxOutputBytes = maxOutputBytes
    }

    public func call(
        command: String,
        arguments: [String: JSONValue]
    ) async throws -> [String: JSONValue] {
        let executable = self.executable
        let timeout = self.timeout
        let maxOutputBytes = self.maxOutputBytes
        let requestID = UUID().uuidString
        let request: Data
        do {
            request = try JSONEncoder().encode(
                HelperRequest(id: requestID, command: command, arguments: arguments)
            )
        } catch {
            throw HelperClientError.invalidResponse
        }

        let worker = Task.detached(priority: .userInitiated) {
            let fileManager = FileManager.default
            let outputURL = fileManager.temporaryDirectory
                .appendingPathComponent("inno-helper-\(UUID().uuidString).json")
            guard fileManager.createFile(atPath: outputURL.path, contents: nil) else {
                throw HelperClientError.launchFailed
            }
            defer { try? fileManager.removeItem(at: outputURL) }

            let outputHandle: FileHandle
            do {
                outputHandle = try FileHandle(forWritingTo: outputURL)
            } catch {
                throw HelperClientError.launchFailed
            }
            defer { try? outputHandle.close() }

            let process = Process()
            let input = Pipe()
            process.executableURL = executable
            process.standardInput = input
            process.standardOutput = outputHandle
            process.standardError = FileHandle.nullDevice
            do {
                try process.run()
                try input.fileHandleForReading.close()
                try input.fileHandleForWriting.write(contentsOf: request)
                try input.fileHandleForWriting.close()
            } catch {
                if process.isRunning { process.terminate() }
                throw HelperClientError.launchFailed
            }
            defer {
                if process.isRunning { process.terminate() }
                process.waitUntilExit()
            }

            let deadline = Date().addingTimeInterval(timeout)
            while process.isRunning {
                let size = (try? fileManager.attributesOfItem(atPath: outputURL.path)[.size] as? NSNumber)?.intValue ?? 0
                if size > maxOutputBytes {
                    throw HelperClientError.outputTooLarge
                }
                if Date() >= deadline {
                    throw HelperClientError.timedOut
                }
                try await Task.sleep(for: .milliseconds(10))
            }
            try? outputHandle.synchronize()
            let attributes = try? fileManager.attributesOfItem(atPath: outputURL.path)
            let size = (attributes?[.size] as? NSNumber)?.intValue ?? 0
            if size > maxOutputBytes {
                throw HelperClientError.outputTooLarge
            }
            let data: Data
            do {
                data = try Data(contentsOf: outputURL)
            } catch {
                throw HelperClientError.invalidResponse
            }
            let response: HelperResponse
            do {
                response = try JSONDecoder().decode(HelperResponse.self, from: data)
            } catch {
                throw HelperClientError.invalidResponse
            }
            guard response.id == requestID else {
                throw HelperClientError.responseIDMismatch
            }
            guard response.ok else {
                throw HelperClientError.helperFailure(response.error ?? "helper failed")
            }
            guard let result = response.result else {
                throw HelperClientError.invalidResponse
            }
            return result
        }
        return try await withTaskCancellationHandler {
            try await worker.value
        } onCancel: {
            worker.cancel()
        }
    }
}
