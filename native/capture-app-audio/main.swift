// Captures a single running application's audio output via ScreenCaptureKit and
// streams it as raw interleaved float32 PCM on stdout, so the Python side (which
// owns the actual Demucs pipeline) can read it as a subprocess without needing any
// virtual audio device or system output rerouting.
//
// Usage: capture-app-audio --app "<app name substring>" [--samplerate 44100] [--channels 2]
// Output: raw float32 little-endian interleaved PCM, written continuously to stdout.
// Diagnostics go to stderr so stdout stays a clean PCM stream.

import ScreenCaptureKit
import AVFoundation
import Foundation

func die(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func logStatus(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

final class AudioForwarder: NSObject, SCStreamOutput, SCStreamDelegate {
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of outputType: SCStreamOutputType) {
        guard outputType == .audio, sampleBuffer.isValid else { return }
        guard let pcmBuffer = makePCMBuffer(from: sampleBuffer) else { return }
        write(pcmBuffer)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        die("Capture stream stopped with error: \(error)")
    }

    private func makePCMBuffer(from sampleBuffer: CMSampleBuffer) -> AVAudioPCMBuffer? {
        guard let formatDescription = sampleBuffer.formatDescription else { return nil }
        guard let asbd = formatDescription.audioStreamBasicDescription else { return nil }
        guard let format = AVAudioFormat(standardFormatWithSampleRate: asbd.mSampleRate, channels: asbd.mChannelsPerFrame) else { return nil }

        var blockBuffer: CMBlockBuffer?
        var audioBufferList = AudioBufferList()
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: &audioBufferList,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBuffer
        )
        guard status == noErr else { return nil }

        let frameCount = AVAudioFrameCount(sampleBuffer.numSamples)
        guard let pcmBuffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount) else { return nil }
        pcmBuffer.frameLength = frameCount

        withUnsafeMutablePointer(to: &audioBufferList) { ablPtr in
            let buffers = UnsafeMutableAudioBufferListPointer(ablPtr)
            guard let src = buffers.first?.mData, let dstChannels = pcmBuffer.floatChannelData else { return }
            let channelCount = Int(format.channelCount)
            let src32 = src.assumingMemoryBound(to: Float32.self)
            // ScreenCaptureKit audio arrives interleaved; de-interleave into planar float channels.
            for frame in 0..<Int(frameCount) {
                for ch in 0..<channelCount {
                    dstChannels[ch][frame] = src32[frame * channelCount + ch]
                }
            }
        }
        return pcmBuffer
    }

    private func write(_ buffer: AVAudioPCMBuffer) {
        guard let floatData = buffer.floatChannelData else { return }
        let frameLength = Int(buffer.frameLength)
        let channelCount = Int(buffer.format.channelCount)

        var interleaved = [Float32](repeating: 0, count: frameLength * channelCount)
        for frame in 0..<frameLength {
            for ch in 0..<channelCount {
                interleaved[frame * channelCount + ch] = floatData[ch][frame]
            }
        }
        interleaved.withUnsafeBufferPointer { ptr in
            FileHandle.standardOutput.write(Data(buffer: ptr))
        }
    }
}

let args = CommandLine.arguments

func stringArg(_ flag: String) -> String? {
    guard let i = args.firstIndex(of: flag), args.count > i + 1 else { return nil }
    return args[i + 1]
}

guard let targetName = stringArg("--app")?.lowercased() else {
    die("Usage: capture-app-audio --app <app name substring> [--samplerate 44100] [--channels 2]")
}
let sampleRate = stringArg("--samplerate").flatMap(Double.init) ?? 44100
let channels = stringArg("--channels").flatMap(Int.init) ?? 2

do {
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)

    // Prefer an exact (case-insensitive) name match over a substring match, so e.g.
    // "Firefox" doesn't accidentally match a background helper like "AutoFill (Firefox)".
    let exactMatch = content.applications.first(where: { $0.applicationName.lowercased() == targetName })
    let substringMatch = content.applications.first(where: { $0.applicationName.lowercased().contains(targetName) })
    guard let app = exactMatch ?? substringMatch else {
        let names = content.applications.map(\.applicationName).joined(separator: ", ")
        die("No running application matching '\(targetName)'. Running apps with audio-capturable windows: \(names)")
    }
    guard let display = content.displays.first else {
        die("No display found (needed to scope the capture filter).")
    }

    let filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])

    let config = SCStreamConfiguration()
    config.capturesAudio = true
    config.sampleRate = Int(sampleRate)
    config.channelCount = channels
    config.excludesCurrentProcessAudio = true
    // We don't need video frames at all; keep them minimal to save resources.
    config.width = 2
    config.height = 2
    config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
    config.showsCursor = false

    let forwarder = AudioForwarder()
    let stream = SCStream(filter: filter, configuration: config, delegate: forwarder)
    try stream.addStreamOutput(forwarder, type: .audio, sampleHandlerQueue: DispatchQueue(label: "audio.output.queue"))
    try await stream.startCapture()

    logStatus("Capturing audio from '\(app.applicationName)' at \(Int(sampleRate)) Hz, \(channels) channel(s).")

    while true {
        try await Task.sleep(nanoseconds: 1_000_000_000)
    }
} catch {
    die("Error: \(error)")
}
