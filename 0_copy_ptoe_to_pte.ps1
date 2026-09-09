param(
    [string]$Source      = 'D:\code-project\python\PToEA\dist\ptoe',
    [string]$Destination = 'D:\tools\PTE'
)

# 检查源目录是否存在
if (-not (Test-Path -Path $Source -PathType Container)) {
    Write-Error "Source path '$Source' does not exist or is not a directory."
    exit 1
}

# 确保目标目录存在（必要时创建）
if (-not (Test-Path -Path $Destination)) {
    try {
        New-Item -Path $Destination -ItemType Directory -Force | Out-Null
    } catch {
        Write-Error "Failed to create destination directory '$Destination': $_"
        exit 2
    }
}

try {
    Write-Host "Copying contents from '$Source' -> '$Destination' (overwrite enabled)..."
    # Copy all files and folders from $Source into $Destination, overwrite existing files
    Copy-Item -Path (Join-Path -Path $Source -ChildPath '*') -Destination $Destination -Recurse -Force -ErrorAction Stop
    Write-Host "Copy completed successfully."
    exit 0
} catch {
    Write-Error "Copy failed: $_"
    exit 3
}