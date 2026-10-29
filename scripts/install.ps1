<#
Install the LiveBridge Remote Script into Live's User Library on Windows (install.sh is the macOS one).

  powershell -ExecutionPolicy Bypass -File scripts\install.ps1             copy LiveBridge into the User Library
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Check      did Live load it? Is the copy current? Where do Claude's registrations point?
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Desktop    register the MCP server with the Claude desktop app
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall

It always copies: a symlink needs admin rights or Developer Mode on Windows. Run it again after
every change to remote_script\. Live only loads Remote Scripts at startup: restart Live after installing.
Windows PowerShell 5.1 runs it, so keep it ASCII (5.1 reads a file without a BOM as ANSI).
#>
param(
    [switch]$Check,
    [switch]$Desktop,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'

$Repo = Split-Path -Parent $PSScriptRoot
$Src = Join-Path $Repo 'remote_script\LiveBridge'
$Server = Join-Path $Repo 'server'
$Helper = Join-Path $PSScriptRoot 'mcp_config.py'
$Name = 'LiveBridge'
$Port = 9878
$Config = Join-Path $HOME '.config\abletonconnect\config.json'
if (Test-Path -LiteralPath $Config) {
    try {
        $configured = (Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json).port
        if ($configured) { $Port = [int]$configured }
    } catch { }
}

function Fail([string]$Message) {
    [Console]::Error.WriteLine($Message)
    exit 1
}

# Newest "Live x.y.z\Preferences" folder: Library.cfg and Log.txt live there.
function Get-PrefsDir {
    $root = Join-Path $env:APPDATA 'Ableton'
    if (-not (Test-Path -LiteralPath $root)) { return $null }
    $dirs = @(Get-ChildItem -LiteralPath $root -Directory -Filter 'Live *' |
        ForEach-Object { Get-Item -LiteralPath (Join-Path $_.FullName 'Preferences') -ErrorAction SilentlyContinue } |
        Where-Object { $_ } |
        Sort-Object LastWriteTime -Descending)
    if ($dirs.Count -eq 0) { return $null }
    return $dirs[0].FullName
}

# The User Library location from Library.cfg, falling back to Live's default.
function Get-UserLibrary {
    $prefs = Get-PrefsDir
    $cfg = if ($prefs) { Join-Path $prefs 'Library.cfg' } else { $null }
    if ($cfg -and (Test-Path -LiteralPath $cfg)) {
        try {
            $text = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8
            $block = [regex]::Match($text, '<UserLibrary>(.*?)</UserLibrary>', 'Singleline')
            $path = [regex]::Match($block.Groups[1].Value, '<ProjectPath Value="([^"]+)"')
            $libName = [regex]::Match($block.Groups[1].Value, '<ProjectName Value="([^"]+)"')
            if ($block.Success -and $path.Success -and $libName.Success) {
                # .NET, not Join-Path: Join-Path fails on a drive that isn't mounted.
                $lib = [IO.Path]::Combine($path.Groups[1].Value, $libName.Groups[1].Value)
                if (Test-Path -LiteralPath $lib -PathType Container) { return $lib }
            }
        } catch { }
    }
    return Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ableton\User Library'
}

$DestDir = Join-Path (Get-UserLibrary) 'Remote Scripts'
$Dest = Join-Path $DestDir $Name

# Relative paths of the Remote Script's files, without the bytecode Live writes next to them.
function Get-BridgeFiles([string]$Root) {
    $full = (Get-Item -LiteralPath $Root).FullName.TrimEnd('\')
    Get-ChildItem -LiteralPath $full -Recurse -File -Force |
        Where-Object { $_.Extension -ne '.pyc' -and $_.FullName -notlike '*\__pycache__\*' } |
        ForEach-Object { $_.FullName.Substring($full.Length + 1) } |
        Sort-Object
}

function Remove-Existing {
    if (-not (Test-Path -LiteralPath $Dest)) { return }
    $item = Get-Item -LiteralPath $Dest -Force
    if ($item.PSIsContainer -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        # A symlink or junction someone made by hand: remove the link, never what it points to.
        [IO.Directory]::Delete($Dest, $false)
    } elseif ($item.PSIsContainer) {
        # Only delete a folder that is clearly a previous LiveBridge copy.
        $bridge = Join-Path $Dest 'bridge.py'
        $isBridge = (Test-Path -LiteralPath (Join-Path $Dest 'protocol.py')) -and (Test-Path -LiteralPath $bridge) -and
            (Select-String -LiteralPath $bridge -Pattern 'LiveBridge' -SimpleMatch -Quiet)
        if (-not $isBridge) { Fail "Refusing to replace ${Dest}: it doesn't look like a LiveBridge install." }
        Remove-Item -LiteralPath $Dest -Recurse -Force
    } else {
        Fail "Refusing to replace ${Dest}: unexpected file."
    }
}

function Copy-Bridge {
    foreach ($file in @(Get-BridgeFiles $Src)) {
        $target = Join-Path $Dest $file
        [IO.Directory]::CreateDirectory((Split-Path -Parent $target)) | Out-Null
        [IO.File]::Copy((Join-Path $Src $file), $target, $true)
    }
}

# How many files differ between the repo and the installed copy.
function Compare-Bridge {
    $a = @(Get-BridgeFiles $Src)
    $b = @(Get-BridgeFiles $Dest)
    if ($b.Count -eq 0) { return $a.Count }
    $differ = @(Compare-Object $a $b).Count
    foreach ($file in $a) {
        if ($b -contains $file) {
            $hashA = (Get-FileHash -LiteralPath (Join-Path $Src $file)).Hash
            $hashB = (Get-FileHash -LiteralPath (Join-Path $Dest $file)).Hash
            if ($hashA -ne $hashB) { $differ++ }
        }
    }
    return $differ
}

function Test-LiveRunning {
    # The process carries the edition: "Ableton Live 12 Suite", "Ableton Live 12 Lite".
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like 'Ableton Live*' }).Count -gt 0
}

# uv's absolute path: the Claude desktop app doesn't get the PATH a new terminal has.
function Find-Uv {
    $cmd = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Path }
    $candidates = @(
        (Join-Path $HOME '.local\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'),
        (Join-Path $HOME '.cargo\bin\uv.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    return $null
}

# The desktop app's config. The Microsoft Store build keeps its AppData inside its package folder.
function Get-DesktopConfig {
    $packages = Join-Path $env:LOCALAPPDATA 'Packages'
    $store = @(Get-ChildItem -LiteralPath $packages -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude' } |
        Where-Object { Test-Path -LiteralPath $_ })
    if ($store.Count -gt 0) { return Join-Path $store[0] 'claude_desktop_config.json' }
    return Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'
}

# Claude desktop app processes. Claude Code's own claude.exe runs from ~\.local, so it doesn't count.
# A process whose path can't be read counts, to be safe.
function Get-ClaudeApp {
    $code = Join-Path $HOME '.local\'
    return @(Get-Process -Name 'claude' -ErrorAction SilentlyContinue |
        Where-Object { -not $_.Path -or -not $_.Path.StartsWith($code, [StringComparison]::OrdinalIgnoreCase) })
}

# Add or replace the "ableton" entry in the Claude desktop app's config, keeping everything else.
function Register-Desktop {
    $desktopConfig = Get-DesktopConfig
    $app = Get-ClaudeApp
    if ($app.Count -gt 0) {
        Fail ("The Claude desktop app is running ($($app[0].Path)). It rewrites $desktopConfig`n" +
            "from memory when it saves its settings, which drops an entry added while it's open.`n" +
            "Quit the Claude app (its tray icon > Quit), run scripts\install.ps1 -Desktop again from a terminal, then reopen it.")
    }
    $uv = Find-Uv
    if (-not $uv) { Fail 'uv not found. Install it from https://docs.astral.sh/uv/ and run this again.' }
    [IO.Directory]::CreateDirectory((Split-Path -Parent $desktopConfig)) | Out-Null
    if (Test-Path -LiteralPath $desktopConfig) {
        $backup = $desktopConfig + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Copy-Item -LiteralPath $desktopConfig -Destination $backup
        Write-Host "Backed up $desktopConfig"
        Write-Host "       to $backup"
    }
    & $uv run --no-project --quiet python $Helper register $desktopConfig $uv $Server
    if ($LASTEXITCODE -ne 0) { Fail "Couldn't update $desktopConfig (see above). The backup is next to it." }
    Write-Host 'Open the Claude desktop app to load it.'
}

function Show-Registrations {
    $uv = Find-Uv
    if (-not $uv) {
        Write-Host 'MCP:      uv not found, so the registrations can''t be read. Install it from https://docs.astral.sh/uv/'
        return
    }
    & $uv run --no-project --quiet python $Helper show (Join-Path $HOME '.claude.json') (Get-DesktopConfig) $Server
}

function Show-Check {
    Write-Host "Repo:     '$Repo'"
    if ($Repo -match '\s(\\|$)') {
        Write-Host '          WARNING: a folder in this path ends in a space. The scripts quote it and work, but'
        Write-Host '          anything that splits or trims paths will break.'
    }
    Write-Host "Install:  $Dest"
    if (-not (Test-Path -LiteralPath $Dest)) {
        Write-Host '          NOT INSTALLED (run scripts\install.ps1)'
    } else {
        $differ = Compare-Bridge
        if ($differ -eq 0) {
            Write-Host '          copy, same as the repo'
        } else {
            Write-Host "          copy, OUT OF DATE: $differ file(s) differ from the repo."
            Write-Host '          Run scripts\install.ps1 and restart Live.'
        }
    }
    Show-Registrations
    $prefs = Get-PrefsDir
    if (-not $prefs) {
        Write-Host "Log:      no Live preferences folder under $env:APPDATA\Ableton. Has Live 12 been started on this account?"
    } else {
        $log = Join-Path $prefs 'Log.txt'
        Write-Host "Log:      $log"
    }
    if (Test-LiveRunning) { Write-Host 'Live:     running' } else { Write-Host 'Live:     not running' }
    if ($prefs -and (Test-Path -LiteralPath $log)) {
        # Only look at the log since Live last started.
        $lines = @(Get-Content -LiteralPath $log -Encoding UTF8)
        $since = 0
        for ($i = $lines.Count - 1; $i -ge 0; $i--) {
            if ($lines[$i] -like '*info: Started: Live*') { $since = $i; break }
        }
        $recent = @()
        if ($lines.Count -gt 0) { $recent = @($lines[$since..($lines.Count - 1)] | Where-Object { $_ -like '*LiveBridge*' }) }
        if ($recent.Count -gt 0) {
            Write-Host 'Log lines since Live started:'
            $recent | Select-Object -Last 15 | ForEach-Object { Write-Host "          $_" }
        } else {
            Write-Host '          No LiveBridge lines since Live started.'
            Write-Host '          Is LiveBridge selected under Settings > Link, Tempo & MIDI > Control Surface?'
        }
    }
    $listening = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listening.Count -gt 0) {
        $owner = (Get-Process -Id $listening[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
        Write-Host "Port:     $($listening[0].LocalAddress):$Port is listening ($owner)"
    } else {
        Write-Host "Port:     nothing listening on $Port"
    }
}

if ($Check) { Show-Check; exit 0 }
if ($Desktop) { Register-Desktop; exit 0 }
if ($Uninstall) {
    Remove-Existing
    Write-Host "Removed $Dest. Restart Live."
    exit 0
}

[IO.Directory]::CreateDirectory($DestDir) | Out-Null
Remove-Existing
Copy-Bridge
Write-Host "Copied  $Src -> $Dest"
Write-Host '        Run scripts\install.ps1 again after every change to remote_script\.'

if (Test-LiveRunning) {
    Write-Host ''
    Write-Host 'Live is running. Restart it: Live only loads Remote Scripts at startup.'
}
Write-Host ''
Write-Host 'Then in Live > Settings > Link, Tempo & MIDI, set an empty Control Surface slot to'
Write-Host "`"$Name`" with Input: None and Output: None. Check it loaded with: scripts\install.ps1 -Check"
$uv = Find-Uv
if ($uv) {
    Write-Host ''
    Write-Host 'Register the MCP server with Claude Code (once):'
    Write-Host "  claude mcp add --scope user ableton -- `"$uv`" --directory `"$Server`" run abletonconnect-mcp"
}
