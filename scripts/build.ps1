<#
构建脚本：把 Codex 模型同步器打包为单文件 Windows exe。

用法：
    powershell -ExecutionPolicy Bypass -File .\scripts\build.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\build.ps1 -Clean

参数：
    -Name   输出 exe 名称（默认 CodexModelSync）
    -Clean  构建前清理 build/ 与 dist/ 目录

注意：PyInstaller 在打包时需要能正常初始化 Tcl/Tk，请勿在受限沙箱环境执行，
否则会把 tkinter 误判为损坏并从应用中剔除。
#>
param(
    [string]$Name = "CodexModelSync",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

if ($Clean) {
    foreach ($dir in @("build", "dist")) {
        $target = Join-Path $root $dir
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
}

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name $Name `
    --icon "assets\icon.ico" `
    --add-data "assets\icon.ico;." `
    --distpath "dist" `
    --workpath "build" `
    "codex_model_sync_app.py"

Write-Host ""
Write-Host "构建完成: $root\dist\$Name.exe" -ForegroundColor Green

