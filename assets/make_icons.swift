// Draws the Desk Music Companion icon for the iPhone and the Mac from code.
// Run from the project folder:  swift assets/make_icons.swift
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!

func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor {
    CGColor(colorSpace: sRGB, components: [r, g, b, a])!
}

let gold = (r: CGFloat(0.96), g: CGFloat(0.74), b: CGFloat(0.36))
let blue = (r: CGFloat(0.33), g: CGFloat(0.56), b: CGFloat(0.72))
let cream = (r: CGFloat(0.965), g: CGFloat(0.957), b: CGFloat(0.937))

// The same uneven ring the visualizer draws around the sung line.
func ring(center: CGPoint, radius: CGFloat, twist: CGFloat) -> CGPath {
    let path = CGMutablePath()
    let steps = 240
    for i in 0..<steps {
        let a = CGFloat(i) / CGFloat(steps) * .pi * 2
        let wobble = 1 + 0.075 * sin(3 * a + 0.6 + twist) + 0.04 * sin(7 * a + 2.1 - twist) + 0.022 * sin(13 * a + twist * 2)
        let point = CGPoint(x: center.x + cos(a) * radius * wobble, y: center.y + sin(a) * radius * wobble)
        if i == 0 { path.move(to: point) } else { path.addLine(to: point) }
    }
    path.closeSubpath()
    return path
}

func bar(_ c: CGContext, x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat, fill: CGColor) {
    c.addPath(CGPath(roundedRect: CGRect(x: x, y: y, width: width, height: height), cornerWidth: height / 2, cornerHeight: height / 2, transform: nil))
    c.setFillColor(fill)
    c.fillPath()
}

func paint(_ c: CGContext, pixels: CGFloat, inset: Bool) {
    let scale = pixels / 1024
    c.scaleBy(x: scale, y: scale)
    let body = inset ? CGRect(x: 100, y: 100, width: 824, height: 824) : CGRect(x: 0, y: 0, width: 1024, height: 1024)
    let k = body.width / 1024
    let center = CGPoint(x: body.midX, y: body.midY)

    if inset {   // the Mac shows the rounded shape it is given, with its own shadow
        c.saveGState()
        c.setShadow(offset: CGSize(width: 0, height: -12 * scale), blur: 28 * scale, color: color(0, 0, 0, 0.4))
        c.addPath(CGPath(roundedRect: body, cornerWidth: 185, cornerHeight: 185, transform: nil))
        c.setFillColor(color(0.05, 0.04, 0.035))
        c.fillPath()
        c.restoreGState()
        c.addPath(CGPath(roundedRect: body, cornerWidth: 185, cornerHeight: 185, transform: nil))
        c.clip()
    }

    let ground = CGGradient(colorsSpace: sRGB, colors: [color(0.2, 0.135, 0.075), color(0.045, 0.036, 0.034)] as CFArray, locations: [0, 1])!
    c.drawRadialGradient(ground, startCenter: center, startRadius: 0, endCenter: center, endRadius: 720 * k, options: .drawsAfterEndLocation)

    // Ripples leaving the ring, turning from gold to blue as they fade.
    c.setLineJoin(.round)
    for i in stride(from: 15, through: 1, by: -1) {
        let t = CGFloat(i) / 15
        c.addPath(ring(center: center, radius: (352 + t * 130) * k, twist: t * 0.9))
        c.setStrokeColor(color(gold.r + (blue.r - gold.r) * t, gold.g + (blue.g - gold.g) * t, gold.b + (blue.b - gold.b) * t, 0.05 + (1 - t) * 0.26))
        c.setLineWidth((5 + t * 13) * k)
        c.strokePath()
    }

    c.saveGState()
    c.setShadow(offset: .zero, blur: 44 * k * scale, color: color(gold.r, gold.g, gold.b, 0.95))
    c.addPath(ring(center: center, radius: 352 * k, twist: 0))
    c.setStrokeColor(color(1, 0.86, 0.56))
    c.setLineWidth(15 * k)
    c.strokePath()
    c.restoreGState()

    // Three lines of lyrics, the middle one partly sung.
    let height = 50 * k
    let gap = 104 * k
    let dim = color(cream.r, cream.g, cream.b, 0.34)
    let full = color(cream.r, cream.g, cream.b)
    bar(c, x: center.x - 150 * k, y: center.y + gap - height / 2, width: 300 * k, height: height, fill: dim)
    bar(c, x: center.x - 170 * k, y: center.y - gap - height / 2, width: 340 * k, height: height, fill: dim)
    let width = 470 * k, left = center.x - width / 2
    bar(c, x: left, y: center.y - height / 2, width: width, height: height, fill: color(cream.r, cream.g, cream.b, 0.34))
    bar(c, x: left, y: center.y - height / 2, width: width * 0.64, height: height, fill: full)
}

func render(pixels: Int, inset: Bool, to url: URL) {
    let info = inset ? CGImageAlphaInfo.premultipliedLast.rawValue : CGImageAlphaInfo.noneSkipLast.rawValue
    let c = CGContext(data: nil, width: pixels, height: pixels, bitsPerComponent: 8, bytesPerRow: 0, space: sRGB, bitmapInfo: info)!
    c.interpolationQuality = .high
    paint(c, pixels: CGFloat(pixels), inset: inset)
    let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)!
    CGImageDestinationAddImage(destination, c.makeImage()!, nil)
    CGImageDestinationFinalize(destination)
}

let manager = FileManager.default
let root = URL(fileURLWithPath: manager.currentDirectoryPath)

// iPhone: one opaque square; iOS rounds the corners itself.
let catalog = root.appendingPathComponent("ios/DeskLyrics/Assets.xcassets")
let appIcon = catalog.appendingPathComponent("AppIcon.appiconset")
try manager.createDirectory(at: appIcon, withIntermediateDirectories: true)
render(pixels: 1024, inset: false, to: appIcon.appendingPathComponent("icon-1024.png"))
try #"{ "info": { "author": "xcode", "version": 1 } }"#.write(to: catalog.appendingPathComponent("Contents.json"), atomically: true, encoding: .utf8)
try #"""
{
  "images": [{ "filename": "icon-1024.png", "idiom": "universal", "platform": "ios", "size": "1024x1024" }],
  "info": { "author": "xcode", "version": 1 }
}
"""#.write(to: appIcon.appendingPathComponent("Contents.json"), atomically: true, encoding: .utf8)

// Mac: every size macOS asks for, packed into one .icns file.
let macFolder = root.appendingPathComponent("mac")
let iconset = macFolder.appendingPathComponent("AppIcon.iconset")
try manager.createDirectory(at: iconset, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    render(pixels: size, inset: true, to: iconset.appendingPathComponent("icon_\(size)x\(size).png"))
    render(pixels: size * 2, inset: true, to: iconset.appendingPathComponent("icon_\(size)x\(size)@2x.png"))
}
let pack = Process()
pack.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
pack.arguments = ["-c", "icns", iconset.path, "-o", macFolder.appendingPathComponent("AppIcon.icns").path]
try pack.run()
pack.waitUntilExit()
try manager.removeItem(at: iconset)

// For the README.
render(pixels: 256, inset: true, to: root.appendingPathComponent("assets/icon.png"))
print("Icons written.")
