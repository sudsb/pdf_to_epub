<#
.SYNOPSIS
    用 PyInstaller 把 mian.py 打包为独立可运行的 ptoe 程序。

.DESCRIPTION
    默认产出 **onedir**（目录式分发）：dist\ptoe\ptoe.exe + _internal\ 依赖，
    无需 Python 环境即可运行，启动时不解包到临时目录（无 onefile 式首启卡顿）。

    体积优化（2026-09，详见 ptoe.spec 注释；均已实测验证）：
      * 默认约 2.5 GB（优化前 3.75 GB，-33%），--engine paddle 全功能保留（含 GPU）
      * -NoPaddle 则排除 PaddleOCR 引擎，包体降到约 250 MB（该引擎为可选功能）
      * 裁剪在"采集阶段"完成，不再先复制上千 MB 再删除 → 打包更快、更省磁盘
      * 只裁实测无影响的项；cublasLt/cudnn_*_engines/mklml/mkldnn/phi.dll 等必需项
        一律保留（移除会直接导致 import paddle 失败或 CUDNN_SUBLIBRARY_LOADING_FAILED）

.PARAMETER OneFile
    打成单个 exe（dist\ptoe.exe）。体积小、便于单文件分发，但每次启动都要解包到
    %TEMP%，首启慢。默认关闭（默认 onedir）。

.PARAMETER NoPaddle
    不打包 PaddleOCR 引擎（paddle/paddleocr/paddlex/nvidia，约 3.4 GB）。
    产物约 250 MB；此时 `--engine paddle` 不可用，其余功能（llama/vLLM OCR、
    手动矫正、配置界面、EPUB 打包）完全不受影响。

.PARAMETER KeepCudnnAdv
    保留 cudnn_adv64_9.dll（+232 MB）。默认裁剪——它只提供 cuDNN 高级 API
    （RNN/注意力等），本程序的检测+识别流程实测不会触发。若你后续要跑更强模型
    （如文档方向分类/版面分析）并遇到 CUDNN 报错，加此开关重打包即可。

.PARAMETER SkipSmokeTest
    跳过打包后的 `ptoe.exe --version` 启动冒烟测试。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\0pack.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\0pack.ps1 -NoPaddle    # 约 250MB 瘦身版
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\0pack.ps1 -OneFile     # 单文件版

.NOTES
    前置：本机已装 uv；首次构建会自动拉取 pyinstaller（不写入项目依赖）。
#>
[CmdletBinding()]
param(
    [switch]$OneFile,
    [switch]$NoPaddle,
    [switch]$KeepCudnnAdv,
    [switch]$SkipSmokeTest,
    [string]$Name = 'ptoe'
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$modeLabel = if ($OneFile) { "onefile（单文件 exe）" } else { "onedir（目录式分发，默认）" }
$engineLabel = if ($NoPaddle) { "不含 PaddleOCR 引擎（约 250 MB）" } else { "含 PaddleOCR 引擎（GPU，约 2.5 GB）" }
Write-Host "==> 打包 $Name.exe：" -ForegroundColor Cyan
Write-Host "    - 形态：$modeLabel"
Write-Host "    - 引擎：$engineLabel"
if (-not $KeepCudnnAdv -and -not $NoPaddle) {
    Write-Host "    - 裁剪：移除 cudnn_adv64_9.dll（-233 MB；如需保留请加 -KeepCudnnAdv）" -ForegroundColor DarkGray
}

# 2026-09 防锁：运行中的 ptoe.exe（含 GUI/correction 实例）会占用自身镜像与
# _internal\ 下 DLL → PyInstaller 清理旧 dist 目录时因句柄被占而失败
# （WinError 32 "另一个程序正在使用此文件"）。构建前先结束残留实例。
$ptoeProc = Get-Process -Name $Name -ErrorAction SilentlyContinue
if ($ptoeProc) {
    Write-Host "==> 检测到 $Name 进程仍在运行，先结束后再打包..." -ForegroundColor Yellow
    $ptoeProc | Stop-Process -Force
    Start-Sleep -Seconds 2
}

# 通过环境变量把开关交给 ptoe.spec（spec 不能用命令行开关）
$env:PTOE_ONEFILE = if ($OneFile) { "1" } else { "0" }
$env:PTOE_NO_PADDLE = if ($NoPaddle) { "1" } else { "0" }
$env:PTOE_KEEP_CUDNN_ADV = if ($KeepCudnnAdv) { "1" } else { "0" }
$env:PTOE_NAME = $Name

try {
    Write-Host "==> PyInstaller 分析/收集/打包中（首次较慢）..." -ForegroundColor Cyan
    uv run --with pyinstaller pyinstaller --noconfirm --clean ptoe.spec
    if ($LASTEXITCODE -ne 0) {
        Write-Host "打包失败（退出码 $LASTEXITCODE）" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}
finally {
    Remove-Item Env:PTOE_ONEFILE, Env:PTOE_NO_PADDLE, Env:PTOE_KEEP_CUDNN_ADV, Env:PTOE_NAME -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------
# 产物体积报告 + 关键裁剪项校验
# ---------------------------------------------------------------
if ($OneFile) {
    $exePath = Join-Path $PSScriptRoot "dist\$Name.exe"
    if (-not (Test-Path -LiteralPath $exePath)) {
        Write-Host "未找到产物 $exePath" -ForegroundColor Red
        exit 1
    }
    $totalMB = (Get-Item -LiteralPath $exePath).Length / 1MB
    Write-Host ""
    Write-Host ("==> 产物：dist\{0}.exe   体积 {1:N1} MB（单文件，{2}）" -f $Name, $totalMB, $engineLabel) -ForegroundColor Green
}
else {
    $distDir = Join-Path $PSScriptRoot "dist\$Name"
    $internalDir = Join-Path $distDir "_internal"
    $exePath = Join-Path $distDir "$Name.exe"
    if (-not (Test-Path -LiteralPath $exePath)) {
        Write-Host "未找到产物 $exePath" -ForegroundColor Red
        exit 1
    }
    $totalMB = (Get-ChildItem -LiteralPath $distDir -Recurse -File |
        Measure-Object -Property Length -Sum).Sum / 1MB
    Write-Host ""
    Write-Host ("==> 产物：dist\{0}\   体积 {1:N1} MB（{2}，{3}）" -f $Name, $totalMB, $modeLabel, $engineLabel) -ForegroundColor Green

    Write-Host "    主要占用：" -ForegroundColor DarkGray
    Get-ChildItem -LiteralPath $internalDir -Recurse -File -ErrorAction SilentlyContinue |
        Sort-Object Length -Descending | Select-Object -First 8 | ForEach-Object {
            $rel = $_.FullName.Substring($internalDir.Length + 1)
            Write-Host ("      {0,8:N1} MB  {1}" -f ($_.Length / 1MB), $rel) -ForegroundColor DarkGray
        }

    # 裁剪校验：这些目录若存在说明过滤没生效（多为 spec 被改坏）
    $shouldBeGone = @(
        "nvidia\cufft", "nvidia\curand", "nvidia\cusolver", "nvidia\cusparse", "nvidia\nvjitlink",
        "paddle\include", "paddle\_typing", "pymupdf\mupdf-devel"
    )
    $leftover = @()
    foreach ($rel in $shouldBeGone) {
        $full = Join-Path $internalDir $rel
        if (Test-Path -LiteralPath $full) { $leftover += $rel }
    }
    if (-not $KeepCudnnAdv -and (Test-Path -LiteralPath (Join-Path $internalDir "nvidia\cudnn\bin\cudnn_adv64_9.dll"))) {
        $leftover += "nvidia\cudnn\bin\cudnn_adv64_9.dll"
    }
    if ($leftover.Count -gt 0) {
        Write-Host ("    [警告] 以下本应裁剪的项仍在包内，请检查 ptoe.spec：{0}" -f ($leftover -join ", ")) -ForegroundColor Yellow
    }
    if (-not $NoPaddle -and -not (Test-Path -LiteralPath (Join-Path $internalDir "paddle"))) {
        Write-Host "    [警告] 未找到 paddle 目录，--engine paddle 将不可用（若需瘦身版请显式用 -NoPaddle）" -ForegroundColor Yellow
    }

    # 必需项校验：误删会直接导致 GPU OCR 无法初始化
    $required = @(
        "nvidia\cublas\bin\cublasLt64_12.dll",
        "nvidia\cudnn\bin\cudnn_engines_precompiled64_9.dll",
        "nvidia\cudnn\bin\cudnn_heuristic64_9.dll",
        "nvidia\cudnn\bin\cudnn_ops64_9.dll"
    )
    if (-not $NoPaddle) {
        foreach ($rel in $required) {
            if (-not (Test-Path -LiteralPath (Join-Path $internalDir $rel))) {
                Write-Host "    [错误] 必需项缺失：_internal\$rel（GPU OCR 会失败）" -ForegroundColor Red
                exit 1
            }
        }
        foreach ($rel in @("paddle\libs\phi.dll", "paddle\libs\mklml.dll", "paddle\libs\mkldnn.dll", "paddle\base\libpaddle.pyd")) {
            if (-not (Test-Path -LiteralPath (Join-Path $internalDir $rel))) {
                Write-Host "    [错误] 必需项缺失：_internal\$rel" -ForegroundColor Red
                exit 1
            }
        }
    }
}

# ---------------------------------------------------------------
# 启动冒烟测试：确认 exe 能起来（版本号同时验证 pyproject.toml 已打入）
# ---------------------------------------------------------------
if (-not $SkipSmokeTest) {
    Write-Host "==> 冒烟测试：$Name.exe --version" -ForegroundColor Cyan
    $verOut = & $exePath --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host ("    OK：{0}" -f ($verOut | Out-String).Trim()) -ForegroundColor Green
    }
    else {
        Write-Host "    [警告] 冒烟测试失败（退出码 $LASTEXITCODE）：$verOut" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "完成。" -ForegroundColor Green
if ($OneFile) {
    Write-Host "  - 分发：直接复制 dist\$Name.exe 给目标机器（单文件）"
}
else {
    Write-Host "  - 分发：把 dist\$Name\ 整个目录（含 _internal\）复制/压缩给目标机器"
}
Write-Host "  - 双击运行：打开交互式终端菜单（PDF→EPUB 转换 / 手动矫正 / 配置 / 模型管理 / 退出）"
Write-Host "  - 命令行：$Name.exe epub <pdf> [--dpi 0] [--model <key>] [--engine llama|vllm|paddle] ..."
Write-Host "  - 首次运行需保证 config.json 中 llama_server / models_dir 指向有效路径"
Write-Host "    （config.json 与 exe 同目录生成；双击运行时 CWD 为 exe 所在目录）"
if (-not $NoPaddle) {
    Write-Host "  - PaddleOCR 首次使用会自动下载模型到 <exe目录>\models\paddlex\official_models"
}
