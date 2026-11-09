// Measures the Mac's audio output and prints spectrum and waveform levels as JSON lines,
// about 30 per second. Nothing is stored or recorded, and playback is not affected.
// Exits when its stdin closes or the default output device changes.
import Accelerate
import CoreAudio
import Foundation

let barCount = 32
let waveCount = 96
let fftSize = 2048
let log2Size = vDSP_Length(11)

func report(_ message: String) -> Never {
    print("{\"error\":\"\(message)\"}")
    fflush(stdout)
    exit(1)
}

func check(_ status: OSStatus, _ step: String) {
    if status != noErr { report("\(step) failed (\(status))") }
}

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
}

final class SampleRing {
    private var samples = [Float](repeating: 0, count: fftSize)
    private var head = 0
    private var lock = os_unfair_lock()

    func write(_ source: UnsafePointer<Float>, count: Int) {
        os_unfair_lock_lock(&lock)
        for i in 0..<count {
            samples[head] = source[i]
            head = (head + 1) % fftSize
        }
        os_unfair_lock_unlock(&lock)
    }

    func latest() -> [Float] {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        return Array(samples[head...] + samples[..<head])
    }
}

// --- Capture -----------------------------------------------------------------

var deviceAddress = address(kAudioHardwarePropertyDefaultOutputDevice)
var outputDevice = AudioObjectID(kAudioObjectUnknown)
var size = UInt32(MemoryLayout<AudioObjectID>.size)
check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &deviceAddress, 0, nil, &size, &outputDevice), "finding the output")

var uidAddress = address(kAudioDevicePropertyDeviceUID)
var outputUID: Unmanaged<CFString>?
size = UInt32(MemoryLayout<CFString?>.size)
check(AudioObjectGetPropertyData(outputDevice, &uidAddress, 0, nil, &size, &outputUID), "reading the output")
let outputName = outputUID!.takeRetainedValue() as String

let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
description.name = "Desk Lyrics Visualizer"
description.uuid = UUID()
description.muteBehavior = .unmuted
description.isPrivate = true

var tap = AudioObjectID(kAudioObjectUnknown)
check(AudioHardwareCreateProcessTap(description, &tap), "creating the audio tap")

var formatAddress = address(kAudioTapPropertyFormat)
var format = AudioStreamBasicDescription()
size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
check(AudioObjectGetPropertyData(tap, &formatAddress, 0, nil, &size, &format), "reading the audio format")
let sampleRate = format.mSampleRate > 0 ? Float(format.mSampleRate) : 48000

let aggregateSettings: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Desk Lyrics Visualizer",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceMainSubDeviceKey: outputName,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputName]],
    kAudioAggregateDeviceTapListKey: [[
        kAudioSubTapDriftCompensationKey: true,
        kAudioSubTapUIDKey: description.uuid.uuidString,
    ]],
]
var aggregate = AudioObjectID(kAudioObjectUnknown)
check(AudioHardwareCreateAggregateDevice(aggregateSettings as CFDictionary, &aggregate), "creating the capture device")

let ring = SampleRing()
let sideRing = SampleRing()
var mono = [Float](repeating: 0, count: 16384)
var side = [Float](repeating: 0, count: 16384)
let captureQueue = DispatchQueue(label: "capture", qos: .userInteractive)
var ioProc: AudioDeviceIOProcID?
check(AudioDeviceCreateIOProcIDWithBlock(&ioProc, aggregate, captureQueue) { _, input, _, _, _ in
    // The tap's stream comes after any input channels the output device has.
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
    guard let last = buffers.last, last.mDataByteSize > 0,
          let lastData = last.mData?.assumingMemoryBound(to: Float.self) else { return }
    let step = max(1, Int(last.mNumberChannels))
    let frames = min(Int(last.mDataByteSize) / 4 / step, mono.count)
    var left = lastData
    let right = step > 1 ? lastData + 1 : lastData
    if step == 1, buffers.count > 1, let other = buffers[buffers.count - 2].mData?.assumingMemoryBound(to: Float.self) {
        left = other
    }
    for frame in 0..<frames {
        let l = left[frame * step], r = right[frame * step]
        mono[frame] = (l + r) / 2
        side[frame] = (l - r) / 2
    }
    mono.withUnsafeBufferPointer { ring.write($0.baseAddress!, count: frames) }
    side.withUnsafeBufferPointer { sideRing.write($0.baseAddress!, count: frames) }
}, "starting capture")
check(AudioDeviceStart(aggregate, ioProc), "starting capture")

// --- Analysis ----------------------------------------------------------------

guard let setup = vDSP_create_fftsetup(log2Size, FFTRadix(kFFTRadix2)) else { report("preparing the analyzer failed") }
var hann = [Float](repeating: 0, count: fftSize)
vDSP_hann_window(&hann, vDSP_Length(fftSize), Int32(vDSP_HANN_NORM))
var real = [Float](repeating: 0, count: fftSize / 2)
var imaginary = [Float](repeating: 0, count: fftSize / 2)
let binWidth = sampleRate / Float(fftSize)

// Bars are spaced evenly in pitch, from deep bass to the top of the treble.
let edges: [Float] = (0...barCount).map { 40 * pow(16000 / 40, Float($0) / Float(barCount)) }

func powerSpectrum(_ samples: [Float]) -> [Float] {
    var windowed = [Float](repeating: 0, count: fftSize)
    vDSP_vmul(samples, 1, hann, 1, &windowed, 1, vDSP_Length(fftSize))
    var power = [Float](repeating: 0, count: fftSize / 2)
    real.withUnsafeMutableBufferPointer { r in
        imaginary.withUnsafeMutableBufferPointer { i in
            var split = DSPSplitComplex(realp: r.baseAddress!, imagp: i.baseAddress!)
            windowed.withUnsafeBytes {
                vDSP_ctoz($0.bindMemory(to: DSPComplex.self).baseAddress!, 2, &split, 1, vDSP_Length(fftSize / 2))
            }
            vDSP_fft_zrip(setup, &split, 1, log2Size, FFTDirection(FFT_FORWARD))
            vDSP_zvmags(&split, 1, &power, 1, vDSP_Length(fftSize / 2))
        }
    }
    return power
}

// Lead vocals are usually mixed to the center, so sound that is in both channels
// equally, within the range of the voice, is the best available estimate of singing.
let vocalBins = max(1, Int(200 / binWidth))...min(fftSize / 2 - 1, Int(4000 / binWidth))

func vocalLevel(mid: [Float], side: [Float]) -> Int {
    var total: Float = 0
    for bin in vocalBins { total += max(0, mid[bin] - 1.5 * side[bin]) }
    let amplitude = sqrt(total / Float(vocalBins.count)) / Float(fftSize / 2)
    let decibels = 20 * log10(max(amplitude, 1e-7))
    return Int(max(0, min(1, (decibels + 66) / 56)) * 255)
}

func spectrum(_ power: [Float]) -> [Int] {
    (0..<barCount).map { bar in
        let low = max(1, Int(edges[bar] / binWidth))
        let high = min(fftSize / 2 - 1, max(low, Int(edges[bar + 1] / binWidth)))
        let peak = power[low...high].max() ?? 0
        let amplitude = sqrt(peak) / Float(fftSize / 2)
        let center = (edges[bar] + edges[bar + 1]) / 2
        // Music is naturally quieter at high pitches, so lift them to fill the display.
        let decibels = 20 * log10(max(amplitude, 1e-7)) + 3 * log2(center / 1000)
        return Int(max(0, min(1, (decibels + 66) / 56)) * 255)
    }
}

func waveform(_ samples: [Float]) -> [Int] {
    // Start each trace on a rising zero crossing so the line holds still.
    var start = 0
    for i in 1..<(fftSize / 4) where samples[i - 1] <= 0 && samples[i] > 0 {
        start = i
        break
    }
    let span = fftSize / 2
    return (0..<waveCount).map { point in
        let value = samples[start + point * span / waveCount]
        return Int(max(0, min(255, (value * 1.6 + 1) * 127.5)))
    }
}

let timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "analysis"))
timer.schedule(deadline: .now(), repeating: .milliseconds(33))
timer.setEventHandler {
    let samples = ring.latest()
    let midPower = powerSpectrum(samples)
    let sidePower = powerSpectrum(sideRing.latest())
    print("{\"b\":\(spectrum(midPower)),\"w\":\(waveform(samples)),\"v\":\(vocalLevel(mid: midPower, side: sidePower))}")
    fflush(stdout)
}
timer.resume()

// --- Stopping ----------------------------------------------------------------

DispatchQueue.global().async {
    var byte: UInt8 = 0
    while read(0, &byte, 1) > 0 {}
    exit(0)
}

AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &deviceAddress, DispatchQueue.main) { _, _ in
    exit(0)   // the server starts a fresh helper for the new output
}

dispatchMain()
