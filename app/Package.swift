// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Myco",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "Myco", targets: ["Myco"])
    ],
    targets: [
        .executableTarget(
            name: "Myco",
            path: "Sources/Myco"
        )
    ]
)
