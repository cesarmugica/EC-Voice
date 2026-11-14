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
  '--hidden-import', 'ecvoice_learn',
  '--hidden-import', 'ecvoice_appmute',
  '--hidden-import', 'ecvoice_tryout',
  '--collect-submodules', 'pycaw',
  '--collect-submodules', 'comtypes',
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
if (-not (Test-Path (Join-Path $appDir '_internal\engine\ecvoice_separate.py'))) { throw 'Falta el separador de voz dentro del programa.' }
$v = & (Join-Path $appDir 'ecvoice.exe') --version
Write-Host "Verificado: $v"

# --- Herramientas para «Aprender voz»: FFmpeg + ffprobe, yt-dlp y Deno ---
$binDir = Join-Path $appDir 'bin'
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
function Get-Zip([string]$Url, [string]$Name) {
  $zip = Join-Path $scriptDir "$Name.zip"; $dir = Join-Path $scriptDir "x-$Name"
  Write-Host "Descargando $Name..."
  Invoke-WebRequest -Uri $Url -OutFile $zip
  if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
  Expand-Archive -Path $zip -DestinationPath $dir -Force
  Remove-Item $zip -Force
  return $dir
}
$x = Get-Zip 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' 'ffmpeg'
foreach ($exe in 'ffmpeg.exe', 'ffprobe.exe') {
  $found = Get-ChildItem -Path $x -Recurse -Filter $exe | Select-Object -First 1
  if (-not $found) { throw "No se encontró $exe en el paquete de FFmpeg." }
  Copy-Item $found.FullName (Join-Path $binDir $exe) -Force
}
Remove-Item $x -Recurse -Force
$x = Get-Zip 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' 'deno'
Copy-Item (Join-Path $x 'deno.exe') (Join-Path $binDir 'deno.exe') -Force
Remove-Item $x -Recurse -Force
Write-Host 'Descargando yt-dlp (siempre la última)...'
Invoke-WebRequest -Uri 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' -OutFile (Join-Path $binDir 'yt-dlp.exe')
$ytv = & (Join-Path $binDir 'yt-dlp.exe') --version
if ($LASTEXITCODE -ne 0) { throw 'yt-dlp.exe no corrió.' }
Write-Host "yt-dlp $ytv · $(& (Join-Path $binDir 'ffmpeg.exe') -version | Select-Object -First 1)"

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
