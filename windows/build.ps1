# ============================================================
#  EC-Voice — genera el instalador de Windows
#  Lo corre GitHub Actions (ver .github/workflows/windows-installer.yml).
#  El instalador es pequeño: el motor de IA (Python, PyTorch con CUDA y
#  Applio, ~4 GB) se descarga desde la app la primera vez.
#  Resultado: windows\out\EC-Voice-Setup-<versión>.exe
# ============================================================
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path
$srcRoot     = Join-Path $projectRoot 'src'

function Invoke-Checked([string]$FilePath, [string[]]$ArgumentList = @()) {
  & $FilePath @ArgumentList
  if ($LASTEXITCODE -ne 0) { throw "$FilePath terminó con código $LASTEXITCODE" }
}

$version = (Select-String -Path (Join-Path $srcRoot 'ecvoice_server.py') -Pattern '^APP_VERSION = "(.+)"').Matches[0].Groups[1].Value
Write-Host "EC-Voice $version" -ForegroundColor Cyan

$distDir = Join-Path $scriptDir 'dist'
$outDir  = Join-Path $scriptDir 'out'
$appDir  = Join-Path $distDir 'ecvoice'
New-Item -ItemType Directory -Force -Path $distDir, $outDir | Out-Null

# --- Fecha de publicación (hora de Ciudad de México) ---
try { $tz = [TimeZoneInfo]::FindSystemTimeZoneById('Central Standard Time (Mexico)') } catch { $tz = [TimeZoneInfo]::Local }
$buildDate = [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $tz).ToString('yyyy-MM-dd HH:mm')
Set-Content -Path (Join-Path $srcRoot 'ecvoice_build.py') -Value "BUILD_DATE = `"$buildDate`"" -Encoding ascii

# --- Programa (PyInstaller, carpeta, sin consola) ---
Write-Host 'Compilando EC-Voice...'
Invoke-Checked 'python' @(
  '-m', 'PyInstaller', '--clean', '--noconfirm', '--onedir', '--noconsole',
  '--name', 'ecvoice',
  '--icon', (Join-Path $srcRoot 'web\img\ecvoice.ico'),
  '--add-data', "$(Join-Path $srcRoot 'web');web",
  '--add-data', "$(Join-Path $srcRoot 'engine');engine",
  '--paths', $srcRoot,
  '--hidden-import', 'ecvoice_build',
  '--exclude-module', 'tkinter',
  '--distpath', $distDir,
  '--workpath', (Join-Path $scriptDir '.build'),
  '--specpath', (Join-Path $scriptDir '.build'),
  (Join-Path $srcRoot 'ecvoice_server.py')
)
if (-not (Test-Path (Join-Path $appDir '_internal\engine\ecvoice_engine.py'))) { throw 'Falta el motor (engine) dentro del programa.' }
$v = & (Join-Path $appDir 'ecvoice.exe') --version
Write-Host "Verificado: $v"

# --- Instalador ---
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw 'Falta Inno Setup 6.' }
Invoke-Checked $iscc @("/DAppVersion=$version", "/DSourceDir=$appDir", "/O$outDir", (Join-Path $scriptDir 'ecvoice.iss'))
Write-Host "Listo: $outDir\EC-Voice-Setup-$version.exe" -ForegroundColor Green
