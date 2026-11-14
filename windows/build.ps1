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
  '--hidden-import', 'ecvoice_audio',
  '--hidden-import', 'ecvoice_echo',
  '--collect-all', 'sounddevice',
  '--collect-all', '_sounddevice_data',
  '--collect-all', 'soundcard',
  '--exclude-module', 'tkinter',
  '--distpath', $distDir,
  '--workpath', (Join-Path $scriptDir '.build'),
  '--specpath', (Join-Path $scriptDir '.build'),
  (Join-Path $srcRoot 'ecvoice_server.py')
)
if (-not (Test-Path (Join-Path $appDir '_internal\engine\ecvoice_engine.py'))) { throw 'Falta el motor (engine) dentro del programa.' }
$v = & (Join-Path $appDir 'ecvoice.exe') --version
Write-Host "Verificado: $v"

# --- Cable virtual VB-CABLE (VB-Audio, donationware; permite incluirlo tal cual) ---
$cableUrl = 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack46.zip'
$cableDir = Join-Path $appDir 'vbcable'
$cableZip = Join-Path $scriptDir 'vbcable.zip'
Write-Host 'Descargando VB-CABLE...'
Invoke-WebRequest -Uri $cableUrl -OutFile $cableZip
New-Item -ItemType Directory -Force -Path $cableDir | Out-Null
Expand-Archive -Path $cableZip -DestinationPath $cableDir -Force
if (-not (Test-Path (Join-Path $cableDir 'VBCABLE_Setup_x64.exe'))) { throw 'No se encontró VBCABLE_Setup_x64.exe en el paquete de VB-CABLE.' }
Remove-Item $cableZip -Force

# --- Comprobar que el Python del motor (NuGet de python.org) sirve ---
$pyVer = (Select-String -Path (Join-Path $srcRoot 'ecvoice_server.py') -Pattern '^PYTHON_VERSION = "(.+)"').Matches[0].Groups[1].Value
$nupkg = Join-Path $scriptDir 'python.zip'
Invoke-WebRequest -Uri "https://www.nuget.org/api/v2/package/python/$pyVer" -OutFile $nupkg
$pyTest = Join-Path $scriptDir 'pytest-nuget'
Expand-Archive -Path $nupkg -DestinationPath $pyTest -Force
$pyExe = Join-Path $pyTest 'tools\python.exe'
if (-not (Test-Path $pyExe)) { throw 'El paquete NuGet de Python no trae tools\python.exe.' }
Invoke-Checked $pyExe @('-c', 'import ssl, sqlite3, ctypes, json, multiprocessing, socket; print("Python del motor OK")')
Remove-Item $pyTest -Recurse -Force; Remove-Item $nupkg -Force

# --- Instalador ---
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw 'Falta Inno Setup 6.' }
Invoke-Checked $iscc @("/DAppVersion=$version", "/DSourceDir=$appDir", "/O$outDir", (Join-Path $scriptDir 'ecvoice.iss'))
Write-Host "Listo: $outDir\EC-Voice-Setup-$version.exe" -ForegroundColor Green
