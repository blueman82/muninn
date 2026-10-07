# The bootstrap must check installed selection before running its interpreter.
# Python repeats this check once the private selected runtime is importable.
$ErrorActionPreference = 'Stop'
$Forwarded = @($args)

function Assert-Ordinary([string]$Path, [bool]$Directory) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -notmatch '^[A-Za-z]:\\' -or $full.Substring(2).Contains(':')) {
        throw 'A fixed local absolute path is required'
    }
    $drive = New-Object IO.DriveInfo ($full.Substring(0, 3))
    if ($drive.DriveType -ne [IO.DriveType]::Fixed) { throw 'Unsafe drive' }
    $item = Get-Item -LiteralPath $full -Force
    if ($item.PSIsContainer -ne $Directory) { throw 'Wrong object type' }
    $current = $item
    while ($null -ne $current) {
        if (($current.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Reparse paths are refused'
        }
        if ($current.Name -match '[. ]$|^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') {
            throw 'Ambiguous path component'
        }
        if ($current -is [IO.DirectoryInfo]) { $current = $current.Parent }
        else { $current = $current.Directory }
    }
    return $item.FullName
}

function Assert-Private([string]$Path, [bool]$Directory) {
    $full = Assert-Ordinary $Path $Directory
    $acl = Get-Acl -LiteralPath $full
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $allowed = @($identity, 'S-1-5-18', 'S-1-5-32-544')
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($allowed -notcontains $owner) { throw 'Unsafe owner' }
    $raw = [Security.AccessControl.RawSecurityDescriptor]::new(
        $acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::All))
    if ($null -eq $raw.DiscretionaryAcl) { throw 'Null DACL is refused' }
    foreach ($ace in $raw.DiscretionaryAcl) {
        if ($ace.AceType -notin @([Security.AccessControl.AceType]::AccessAllowed,
                                  [Security.AccessControl.AceType]::AccessDenied)) {
            throw 'Unknown access rule is refused'
        }
    }
    $rules = $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
    if ($rules.Count -eq 0) { throw 'Absent private access rules' }
    foreach ($rule in $rules) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            if (($allowed + @('S-1-3-4')) -notcontains $rule.IdentityReference.Value) {
                throw 'Broader access is refused'
            }
        }
    }
    return $full
}

function Quote-Argument([string]$Value) {
    # ProcessStartInfo avoids PowerShell 5 losing empty args or embedded quotes.
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (2 * $slashes + 1)))
        } else { [void]$builder.Append(('\' * $slashes)) }
        [void]$builder.Append($character)
        $slashes = 0
    }
    [void]$builder.Append(('\' * (2 * $slashes)))
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Python-Info([string]$Candidate) {
    $python = Assert-Ordinary $Candidate $false
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $python
    $start.Arguments = '-I -B -X utf8 -c "import sys; print(sys.executable); sys.exit(sys.version_info < (3, 13))"'
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        $output = $process.StandardOutput.ReadToEnd()
        [void]$process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw 'Python 3.13 or newer is required' }
        return $output.Trim()
    } finally { $process.Dispose() }
}

try {
    $root = Split-Path -Parent $PSScriptRoot
    $installed = -not (Test-Path -LiteralPath (Join-Path $root 'muninn/cli.py'))
    $recorded = $null
    if ($installed) {
        $base = Assert-Private $root $true
        $lib = Assert-Private (Join-Path $base 'lib') $true
        $manifest = Assert-Private (Join-Path $lib 'selection.json') $false
        if ((Get-Item -LiteralPath $manifest).Length -gt 4096) { throw 'Selection is too large' }
        $record = [IO.File]::ReadAllText($manifest, [Text.Encoding]::UTF8) | ConvertFrom-Json
        $names = @($record.PSObject.Properties.Name | Sort-Object)
        if (($names -join ',') -ne 'python,sha' -or
            $record.sha -isnot [string] -or $record.sha -cnotmatch '^[0-9a-f]{40}$' -or
            $record.python -isnot [string]) { throw 'Invalid selection' }
        $root = Assert-Private (Join-Path $lib $record.sha) $true
        $recorded = Assert-Ordinary $record.python $false
    }
    $python = $null
    if ($env:MUNINN_PYTHON) { $python = Python-Info $env:MUNINN_PYTHON }
    elseif ($recorded) { $python = Python-Info $recorded }
    else {
        foreach ($name in @('python3.13.exe', 'python3.14.exe', 'python.exe')) {
            $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
            if ($command) {
                try { $python = Python-Info $command.Source; break } catch { }
            }
        }
        if (-not $python) { throw 'No Python 3.13 or newer found; set MUNINN_PYTHON' }
    }
    $program = 'import sys; sys.path.insert(0,sys.argv.pop(1)); from muninn.cli import main; raise SystemExit(main(sys.argv[1:]))'
    if ($installed) {
        $program = 'import os,sys; sys.path.insert(0,sys.argv.pop(1)); from pathlib import Path; from muninn.platform_paths import read_selection; selected=read_selection(Path(sys.argv.pop(1))); assert selected is not None; assert selected[0]==Path(sys.path[0]); from muninn.cli import main; raise SystemExit(main(sys.argv[1:]))'
    }
    $arguments = @('-I', '-B', '-X', 'utf8', '-c', $program, $root)
    if ($installed) { $arguments += $base }
    $arguments += $Forwarded
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $python
    $start.Arguments = (($arguments | ForEach-Object { Quote-Argument $_ }) -join ' ')
    $start.UseShellExecute = $false
    $process = [Diagnostics.Process]::Start($start)
    $process.WaitForExit()
    $code = $process.ExitCode
    $process.Dispose()
    exit $code
} catch {
    [Console]::Error.WriteLine('muninn: native launcher refused unsafe selection or unavailable Python')
    exit 127
}
