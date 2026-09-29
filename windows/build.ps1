# 在 Windows 上把地端版桌面程式建置成 OfficialDocOCR.exe（PyInstaller onedir）。
#
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1              # 內附模型，完全離線
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1 -NoModel     # 不附模型，首次開啟時下載
#
# venv、暫存與輸出都放在 $BuildRoot（預設 %USERPROFILE%\official_doc_parser_build），
# 不用 repo 內的 .venv：repo 可能同時被 WSL/Linux 使用，也可能位於 \\wsl.localhost 上。
param(
    [string]$BuildRoot = (Join-Path $env:USERPROFILE "official_doc_parser_build"),
    [switch]$NoModel
)

# 不設 $ErrorActionPreference = "Stop"：PowerShell 5.1 會把 uv/PyInstaller 寫到 stderr 的
# 進度訊息當成錯誤中止；改看每個指令的 exit code。
$repo = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $BuildRoot "venv"
$dist = Join-Path $BuildRoot "dist"
if ($NoModel) { $env:OCR_DOC_HF_HUB = "" } else { $env:OCR_DOC_HF_HUB = Join-Path $BuildRoot "hf_hub" }

$env:UV_PROJECT_ENVIRONMENT = $venv
Push-Location $repo
try {
    uv sync --frozen --extra local --extra exe
    if ($LASTEXITCODE) { throw "uv sync 失敗（exit $LASTEXITCODE）" }

    if (-not $NoModel) {
        # 模型（約 3.4 GB）依 model_manifest.json 的固定版本下載到本機 HF cache，
        # 再整理成不含 symlink 的可攜 cache 供打包。
        & (Join-Path $venv "Scripts\python.exe") model_store.py stage $env:OCR_DOC_HF_HUB
        if ($LASTEXITCODE) { throw "模型整理失敗（exit $LASTEXITCODE）" }
    }

    & (Join-Path $venv "Scripts\pyinstaller.exe") --noconfirm --clean `
        --workpath (Join-Path $BuildRoot "work") `
        --distpath $dist `
        (Join-Path $PSScriptRoot "ocr_app.spec")
    if ($LASTEXITCODE) { throw "PyInstaller 失敗（exit $LASTEXITCODE）" }

    # NVIDIA Open Model License §3.1：散布模型時須附協議全文與 Notice 檔（C-RADIOv2-H 的條件）。
    # 不附模型的版本會在使用者端下載同一批檔案，一樣附上。
    $out = Join-Path $dist "OfficialDocOCR"
    Copy-Item (Join-Path $PSScriptRoot "NOTICE.txt") $out -ErrorAction Stop
    Invoke-WebRequest -UseBasicParsing -ErrorAction Stop `
        "https://developer.download.nvidia.com/licenses/nvidia-open-model-license-agreement-june-2024.pdf" `
        -OutFile (Join-Path $out "NVIDIA-Open-Model-License.pdf")
}
finally {
    Pop-Location
}

Write-Host "完成：$(Join-Path $dist 'OfficialDocOCR')（整個資料夾一起發佈，exe 單獨無法執行）"
