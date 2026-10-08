# The bootstrap must check installed selection before running its interpreter.
# Python repeats this check once the private selected runtime is importable.
$ErrorActionPreference = 'Stop'
$Forwarded = @($args)
$InstallerEntry = $Forwarded.Count -gt 0 -and $Forwarded[0] -ceq '--muninn-installer-entry'
if ($InstallerEntry) { $Forwarded = @($Forwarded | Select-Object -Skip 1) }

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
    Assert-Acl $acl
    return $full
}

function Assert-Acl($acl, [ValidateSet('private', 'ancestor', 'executable')]
                    [string]$Policy = 'private') {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $allowed = @($identity, 'S-1-5-18', 'S-1-5-32-544')
    if ($Policy -ne 'private') {
        $service = [Security.Principal.NTAccount]::new('NT SERVICE', 'TrustedInstaller')
        $allowed += $service.Translate([Security.Principal.SecurityIdentifier]).Value
    }
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
    foreach ($ace in $raw.DiscretionaryAcl) {
        if ($Policy -ne 'private' -and ([int]$ace.AceFlags -band 8)) { continue }
        if ($ace.AceType -eq [Security.AccessControl.AceType]::AccessAllowed) {
            $dangerous = 0x100D0040
            if ($Policy -eq 'executable') { $dangerous = $dangerous -bor 0x40000116 }
            if (($allowed + @('S-1-3-4')) -notcontains $ace.SecurityIdentifier.Value -and
                ($Policy -eq 'private' -or ($ace.AccessMask -band $dangerous))) {
                throw 'Unsafe foreign access is refused'
            }
        }
    }
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

function Assert-InterpreterField([string]$Path) {
    if ($Path -notmatch '^[A-Za-z]:\\' -or $Path.Substring(2).Contains(':')) {
        throw 'A recorded local absolute path is required'
    }
    foreach ($part in $Path.Substring(3).Split('\')) {
        if (-not $part -or $part -match '[. ]$|^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') {
            throw 'Ambiguous recorded component'
        }
    }
    $drive = [IO.DriveInfo]::new($Path.Substring(0, 3))
    if ($drive.DriveType -ne [IO.DriveType]::Fixed) { throw 'Unsafe recorded drive' }
    $full = [IO.Path]::GetFullPath($Path)
    if ([IO.Path]::GetExtension($full) -ine '.exe') { throw 'Interpreter must be an EXE' }
    $parts = [System.Collections.Generic.List[string]]::new()
    $parts.Add($full)
    $directory = [IO.DirectoryInfo]::new([IO.Path]::GetDirectoryName($full))
    while ($null -ne $directory) {
        $parts.Add($directory.FullName)
        $directory = $directory.Parent
    }
    foreach ($part in $parts) {
        try {
            $item = Get-Item -LiteralPath $part -Force -ErrorAction Stop
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                $item.PSIsContainer -ne ($part -ne $full)) { throw 'Unsafe recorded object' }
        } catch [System.Management.Automation.ItemNotFoundException] { }
    }
    return $full
}

function Assert-Executable([string]$Candidate) {
    $python = Assert-Ordinary $Candidate $false
    Hold-Directories (Split-Path -Parent $python)
    $executable = [MuninnBootstrap.NativeGuard]::OpenMetadata($python)
    $guards.Add($executable)
    Assert-Acl ($executable.GetAccessControl()) 'executable'
    return $python
}

function Python-Info([string]$Candidate) {
    $python = Assert-Executable $Candidate
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $python
    $start.Arguments = '-I -B -X utf8 -c "import sys; print(sys.executable); sys.exit(sys.version_info < (3, 13))"'
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            $process.Kill()
            throw 'Python version probe timed out'
        }
        $output = $outputTask.GetAwaiter().GetResult()
        [void]$errorTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { return $null }
        return $output.Trim()
    } finally { $process.Dispose() }
}

$guards = [System.Collections.Generic.List[System.IDisposable]]::new()
$guarded = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)

function Hold-Directories([string]$Path) {
    $parts = [System.Collections.Generic.List[string]]::new()
    $directory = [IO.DirectoryInfo]::new([IO.Path]::GetFullPath($Path))
    while ($null -ne $directory) {
        $parts.Insert(0, $directory.FullName)
        $directory = $directory.Parent
    }
    foreach ($part in $parts) {
        if ($guarded.Add($part)) {
            $guard = [MuninnBootstrap.NativeGuard]::HoldDirectory($part)
            $guards.Add($guard)
            Assert-Acl (Get-Acl -LiteralPath $part) 'ancestor'
        }
    }
}

try {
    Add-Type -TypeDefinition @'
// Hold local directory identities during interpreter bootstrap and release use.
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace MuninnBootstrap {
    public static class NativeGuard {
        [StructLayout(LayoutKind.Sequential)]
        private struct Attributes { public uint Value; public uint Tag; }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(string path, uint access,
            uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint GetFileType(SafeFileHandle handle);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandleEx(SafeFileHandle handle,
            int kind, out Attributes attributes, uint size);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle,
            StringBuilder value, uint capacity, uint flags);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetLongPathNameW(string path, StringBuilder value,
            uint capacity);

        private static SafeFileHandle Open(string path, bool directory) {
            // Omit DELETE sharing so ancestors cannot be renamed during use.
            // Metadata-only opens do not establish read/delete sharing protection.
            var handle = CreateFileW(path, directory ? 0x20081u : 0x80000000u,
                directory ? 3u : 1u, IntPtr.Zero, 3u, 0x02200000u, IntPtr.Zero);
            try {
                if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                Attributes attributes;
                if (GetFileType(handle) != 1u ||
                    !GetFileInformationByHandleEx(handle, 9, out attributes, 8u))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                if ((attributes.Value & 0x400u) != 0 ||
                    ((attributes.Value & 0x10u) != 0) != directory)
                    throw new IOException("Unsafe bootstrap object");
                var expected = new StringBuilder(32768);
                var actual = new StringBuilder(32768);
                uint a = GetLongPathNameW(path, expected, 32768u);
                uint b = GetFinalPathNameByHandleW(handle, actual, 32768u, 0u);
                if (a == 0 || a >= 32768 || b == 0 || b >= 32768)
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                string resolved = actual.ToString();
                if (resolved.StartsWith(@"\\?\")) resolved = resolved.Substring(4);
                if (!String.Equals(expected.ToString().TrimEnd('\\'),
                    resolved.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                    throw new IOException("Bootstrap object identity changed");
                return handle;
            } catch { handle.Dispose(); throw; }
        }

        public static SafeFileHandle HoldDirectory(string path) {
            return Open(path, true);
        }

        public static FileStream OpenMetadata(string path) {
            var handle = Open(path, false);
            try { return new FileStream(handle, FileAccess.Read); }
            catch { handle.Dispose(); throw; }
        }

        public static string ReadMetadata(FileStream stream) {
            var data = new byte[4097];
            int used = 0;
            while (used < data.Length) {
                int count = stream.Read(data, used, data.Length - used);
                if (count == 0) break;
                used += count;
            }
            if (used > 4096) throw new IOException("Selection is too large");
            return new UTF8Encoding(false, true).GetString(data, 0, used);
        }
    }
}
'@
    $root = Split-Path -Parent $PSScriptRoot
    $installed = -not (Test-Path -LiteralPath (Join-Path $root 'muninn/cli.py'))
    $recorded = $null
    if ($installed) {
        Hold-Directories $root
        $base = Assert-Private $root $true
        Hold-Directories (Join-Path $base 'lib')
        $lib = Assert-Private (Join-Path $base 'lib') $true
        $manifest = Assert-Ordinary (Join-Path $lib 'selection.json') $false
        $metadata = [MuninnBootstrap.NativeGuard]::OpenMetadata($manifest)
        $guards.Add($metadata)
        Assert-Acl ($metadata.GetAccessControl())
        $record = [MuninnBootstrap.NativeGuard]::ReadMetadata($metadata) | ConvertFrom-Json
        $names = @($record.PSObject.Properties.Name | Sort-Object)
        if (($names -join ',') -cne 'python,sha' -or
            $record.sha -isnot [string] -or $record.sha -cnotmatch '^[0-9a-f]{40}$' -or
            $record.python -isnot [string]) { throw 'Invalid selection' }
        Hold-Directories (Join-Path $lib $record.sha)
        $root = Assert-Private (Join-Path $lib $record.sha) $true
        $recorded = Assert-InterpreterField $record.python
    }
    $python = $null
    $candidates = @()
    if ($env:MUNINN_PYTHON) { $candidates += Assert-InterpreterField $env:MUNINN_PYTHON }
    if ($recorded) { $candidates += $recorded }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $python = Python-Info $candidate
            if ($python) { break }
        }
    }
    if (-not $python) {
        foreach ($name in @('python3.13.exe', 'python3.14.exe', 'python.exe')) {
            $commands = @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)
            foreach ($command in $commands) {
                $candidate = Assert-InterpreterField $command.Path
                $python = Python-Info $candidate
                if ($python) { break }
            }
            if ($python) { break }
        }
    }
    if (-not $python) { throw 'No Python 3.13 or newer found; set MUNINN_PYTHON' }
    $program = 'import sys; sys.path.insert(0,sys.argv.pop(1)); from muninn.cli import main; raise SystemExit(main(sys.argv[1:]))'
    if ($installed) {
        $program = 'import os,sys; sys.path.insert(0,sys.argv.pop(1)); from pathlib import Path; from muninn.platform_paths import read_selection; selected=read_selection(Path(sys.argv.pop(1))); assert selected is not None; assert selected[0]==Path(sys.path[0]); from muninn.cli import main; raise SystemExit(main(sys.argv[1:]))'
    }
    if ($InstallerEntry) {
        $program = $program.Replace('from muninn.cli import main', 'from install.entry import main')
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
} finally {
    for ($index = $guards.Count - 1; $index -ge 0; $index--) {
        $guards[$index].Dispose()
    }
}
