# pack.ps1 — 用 PyInstaller 把 mian.py 打包为独立可运行的 ptoe 程序（dist\ptoe\）。
#
# 产物特点：
#   - onedir：目录式分发（dist\ptoe\ptoe.exe + _internal\ 依赖），无需 Python 环境
#     即可运行；启动时不再解包到临时目录，首次启动无 onefile 式卡顿
#   - console：终端程序。双击启动进入交互式终端菜单（PDF→EPUB 转换 /
#     手动矫正 / 配置 / 模型管理 / 退出）；命令行带参数用法与原 CLI 一致
#   - pyproject.toml 一并打包，保证 exe 内 --version 显示正确版本号
#
# 用法（在项目根目录）：
#   powershell -ExecutionPolicy Bypass -File .\0pack.ps1
#
# 前置：本机已装 uv；首次构建会自动拉取 pyinstaller（不写入项目依赖）。
# 依赖说明：--with pyinstaller 会基于项目 venv 分析模块（pymupdf/requests/
# zhconv/tkinter 全部打入），不影响 pyproject.toml / uv.lock。

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "==> 构建 dist\ptoe\ptoe.exe（PyInstaller onedir / console）..." -ForegroundColor Cyan

# 2026-09 防锁：运行中的 ptoe.exe（含 GUI/correction 实例）会占用自身镜像与
# _internal\ 下 DLL → PyInstaller 清理旧 dist 目录时因句柄被占而失败
# （WinError 32 "另一个程序正在使用此文件"）。构建前先结束残留实例。
$ptoeProc = Get-Process -Name "ptoe" -ErrorAction SilentlyContinue
if ($ptoeProc) {
    Write-Host "==> 检测到 ptoe 进程仍在运行，先结束后再打包..." -ForegroundColor Yellow
    $ptoeProc | Stop-Process -Force
    Start-Sleep -Seconds 2
}

# 2026-08 词典数据（形近/同音/通用词表）随 --add-data "dicts;dicts" 打包
# 2026-08 矫正界面脚本 ui/app.js 随 --add-data "ui;ui" 打包（缺失则矫正页 404）
# 2026-09 PaddleOCR 引擎（--engine paddle）：
#   - --collect-all paddlex：paddlex/configs/pipelines/OCR.yaml 等 350 个管道/模型
#     配置文件缺失 → "The pipeline (OCR) does not exist!"（PyInstaller 默认只收 .py）
#   - --collect-all paddle：libpaddle.pyd 运行时动态 LoadLibrary 的 libs/*.dll
#     （phi.dll/mklml.dll 等）静态分析看不见，必须显式收集，否则 import paddle 失败
#   - --collect-all paddleocr：当前无数据文件，保留以兼容未来版本
#   - ocr-core 依赖（paddlex 的 deps.py 用 importlib.metadata 检查依赖是否安装，
#     PyInstaller 默认不带 dist-info → 元数据缺失被误判为依赖不存在）：
#     --copy-metadata 负责补元数据；--collect-all imagesize 补模块本体
#     （pyclipper/pypdfium2/shapely/bidi 的模块已由 import 分析收进包，仅缺元数据）
uv run --with pyinstaller pyinstaller --noconfirm --clean `
  --onedir --console `
  --noupx `
  --name ptoe `
  --add-data "pyproject.toml;." `
  --add-data "dicts;dicts" `
  --add-data "ui;ui" `
  --collect-all pymupdf `
  --collect-all cv2 `
  --collect-all zhconv `
  --collect-all paddle `
  --collect-all paddleocr `
  --collect-all paddlex `
  --collect-all nvidia `
  --copy-metadata imagesize `
  --copy-metadata pyclipper `
  --copy-metadata pypdfium2 `
  --copy-metadata python-bidi `
  --copy-metadata shapely `
  --collect-all imagesize `
  --collect-all webview `
  --collect-all pythonnet `
  --hidden-import webview.platforms.edgechromium `
  --hidden-import webview.platforms.winforms `
  --hidden-import webview.platforms.wpf `
  --hidden-import clr `
  --hidden-import System `
  --hidden-import System.Windows.Forms `
  mian.py

if ($LASTEXITCODE -ne 0) {
    Write-Host "打包失败（退出码 $LASTEXITCODE）" -ForegroundColor Red
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------
# 2026-09-10 体积裁剪：移除推理用不到的冗余 CUDA 库与未用数据。
#
# 依据（均已实证）：
#   - libpaddle.pyd 的 PE 导入链只依赖 cublas + cudnn（cublas64_12.dll、
#     cudnn64_9.dll -> cudnn_adv/cnn/graph/ops），cufft/curand/cusolver/
#     cusparse/nvjitlink 均不在导入链上，标准 CNN 推理（检测/识别）用不到；
#   - paddle\__init__.py 的 Windows 分支用 os.path.exists 逐个检查
#     _internal\nvidia\<pkg>\bin 是否存在，目录缺失时静默跳过、不报错；
#   - 经裁剪后的 dist\ptoe\ptoe.exe 用 --engine paddle 实跑 3 页 OCR→EPUB
#     全流程通过（GPU 正常初始化：device 0 / CC 8.6 / Runtime 12.6）。
#   - cv2 的 opencv_videoio_ffmpeg*_64.dll 是视频 I/O 用（图像预处理用不到），
#     data 下的 haarcascade/lbpcascade 是人脸检测级联（同样用不到）。
# ---------------------------------------------------------------
$internalDir = Join-Path $PSScriptRoot "dist\ptoe\_internal"
$trimTargets = @(
    @{ Path = "nvidia\cufft";     Desc = "CUFFT 快速傅里叶（推理不用）" },
    @{ Path = "nvidia\curand";    Desc = "CURAND 随机数（训练用）" },
    @{ Path = "nvidia\cusolver";  Desc = "CUSOLVER 线性求解（推理不用）" },
    @{ Path = "nvidia\cusparse";  Desc = "CUSPARSE 稀疏矩阵（推理不用）" },
    @{ Path = "nvidia\nvjitlink"; Desc = "NVJITLINK CUDA JIT（推理不用）" }
)
$trimMed = 0
foreach ($t in $trimTargets) {
    $full = Join-Path $internalDir $t.Path
    if (Test-Path -LiteralPath $full) {
        $sz = (Get-ChildItem -LiteralPath $full -Recurse -File | Measure-Object -Property Length -Sum).Sum
        Remove-Item -LiteralPath $full -Recurse -Force
        $trimMed += $sz
        Write-Host ("  - 移除 {0}（{1:N1} MB，{2}）" -f $t.Path, ($sz/1MB), $t.Desc) -ForegroundColor DarkGray
    }
}
foreach ($pkg in @('cufft','curand','cusolver','cusparse','nvjitlink')) {
    Get-ChildItem -LiteralPath $internalDir -Directory -Filter "nvidia_${pkg}_*dist-info" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
}
# cv2：视频 I/O 的 ffmpeg DLL 与人脸检测级联数据
foreach ($f in @('opencv_videoio_ffmpeg4100_64.dll','opencv_videoio_ffmpeg500_64.dll')) {
    $full = Join-Path $internalDir "cv2\$f"
    if (Test-Path -LiteralPath $full) {
        $trimMed += (Get-Item -LiteralPath $full).Length
        Remove-Item -LiteralPath $full -Force
    }
}
Get-ChildItem -LiteralPath (Join-Path $internalDir "cv2\data") -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "haarcascade_*.xml" -or $_.Name -like "lbpcascade*" } |
    ForEach-Object { $trimMed += $_.Length; Remove-Item -LiteralPath $_.FullName -Force }
if ($trimMed -gt 0) {
    Write-Host ("  == 体积裁剪完成：共释放 {0:N1} MB" -f ($trimMed/1MB)) -ForegroundColor Cyan
}

Write-Host ""
Write-Host "完成：dist\ptoe\ptoe.exe（分发包为整个 dist\ptoe\ 目录）" -ForegroundColor Green
Write-Host "  - 分发：把 dist\ptoe\ 整个目录（含 _internal\）复制/压缩给目标机器"
Write-Host "  - 双击运行：打开交互式终端菜单（双击 dist\ptoe\ptoe.exe）"
Write-Host "  - 命令行：ptoe.exe epub <pdf> [--dpi 0] [--model <key>] ... （与原 CLI 一致）"
Write-Host "  - 首次运行需保证 config.json 中 llama_server / models_dir 指向有效路径"
Write-Host "    （config.json 与 exe 同目录生成；双击运行时 CWD 为 exe 所在目录）"
