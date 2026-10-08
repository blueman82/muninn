"""CI-only ordinary-account and real InteractiveToken no-op topology probe."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from install.context import Ctx, run_real
from install.lifecycle import task_bytes, task_xml
from muninn import platform_io
from muninn.obs_service import literal, powershell
from tests.lifecycle_account_privileges import SOURCE
from tests.native_diagnostics import write

_API = r"""
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class MuninnCiLogon {
 [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
 public struct Startup {
  public int cb; public string reserved, desktop, title;
  public int x,y,xSize,ySize,xChars,yChars,fill,flags;
  public short show,reservedSize;
  public IntPtr reservedBytes,input,output,error;
 }
 [StructLayout(LayoutKind.Sequential)]
 public struct Process {public IntPtr process,thread; public int pid,tid;}
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 public static extern bool LogonUserW(
  string user,string domain,string password,
  int type,int provider,out IntPtr token);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 public static extern bool CreateProcessAsUserW(IntPtr token,string executable,
  StringBuilder command,IntPtr processAttributes,IntPtr threadAttributes,
  bool inherit,int flags,IntPtr env,string cwd,ref Startup startup,
  out Process process);
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern int WaitForSingleObject(IntPtr handle,int milliseconds);
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern bool GetExitCodeProcess(IntPtr process,out int code);
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern bool CloseHandle(IntPtr handle);
}
"""

_OWNED = r"""
function Test-Owned($task,$base){
 [xml]$wanted=[IO.File]::ReadAllText((Join-Path $base 'task.xml'))
 [xml]$actual=$task.Xml
 $ns=[Xml.XmlNamespaceManager]::new($wanted.NameTable)
 $ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
 foreach($path in @('Actions','Principals','Triggers')){
  if($actual.SelectNodes('/t:Task/t:'+$path+'/*',$ns).Count -ne 1){
   return $false
  }
 }
 foreach($path in @(
  'Principals/Principal/UserId','Principals/Principal/LogonType',
  'Principals/Principal/RunLevel','Actions/Exec/Command','Actions/Exec/Arguments',
  'Settings/AllowHardTerminate','Settings/ExecutionTimeLimit',
  'Settings/MultipleInstancesPolicy','Settings/StopIfGoingOnBatteries',
  'Settings/DisallowStartIfOnBatteries','Settings/StartWhenAvailable',
  'Settings/Enabled','Settings/RestartOnFailure/Interval',
  'Settings/RestartOnFailure/Count','Triggers/LogonTrigger/Enabled',
  'Triggers/LogonTrigger/UserId')){
  $xpath='/t:Task/t:'+($path.Replace('/','/t:'))
  $a=$actual.SelectSingleNode($xpath,$ns)
  $w=$wanted.SelectSingleNode($xpath,$ns)
  if($null -eq $a -or $null -eq $w -or $a.InnerText -ne $w.InnerText){
   return $false
  }
 }
 return $actual.SelectSingleNode('/t:Task/t:Actions',$ns).Context -eq
        $wanted.SelectSingleNode('/t:Task/t:Actions',$ns).Context
}
"""

_CHILD = r"""
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$base=Split-Path -Parent $MyInvocation.MyCommand.Path
$report=@{phase='account_child_start';ordinary_child_admin=1;
 scheduler_child_admin=1;scheduler_exit=-1;scheduler_instances=-1;
 interactive_recognized=0;account_retained=1}
$name=Split-Path -Leaf $base;$task=$null;$service=$null
try {
 $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
 $principal=[Security.Principal.WindowsPrincipal]::new($identity)
 $admin=[Security.Principal.WindowsBuiltInRole]::Administrator
 $report.ordinary_child_admin=[int]$principal.IsInRole($admin)
 if($report.ordinary_child_admin -ne 0){throw 'ordinary_identity_required'}
 $service=New-Object -ComObject Schedule.Service;$service.Connect()
 $folder=$service.GetFolder('\')
 $definition=$service.NewTask(0)
 $definition.XmlText=[IO.File]::ReadAllText((Join-Path $base 'task.xml'))
 if($definition.Principal.UserId -ne $identity.User.Value -or
    $definition.Principal.LogonType -ne 3 -or
    $definition.Principal.RunLevel -ne 0){throw 'task_identity_mismatch'}
 $report.phase='account_task_create'
 $task=$folder.RegisterTaskDefinition($name,$definition,2,$null,$null,3,$null)
 [IO.File]::WriteAllText((Join-Path $base 'registered.xml'),$task.Xml)
 if(!(Test-Owned $task $base)){throw 'task_definition_mismatch'}
 $report.phase='account_task_run';$running=$task.Run($null)
 $report.phase='account_task_wait'
 $deadline=[DateTime]::UtcNow.AddSeconds(30)
 $result=Join-Path $base 'task-result.json'
 while(!(Test-Path -LiteralPath $result) -or
       $task.GetInstances(0).Count -ne 0){
  if([DateTime]::UtcNow -gt $deadline){throw 'task_execution_unproven'}
  Start-Sleep -Milliseconds 100
 }
 $scheduled=Get-Content -LiteralPath $result -Raw|ConvertFrom-Json
 if($scheduled.sid -ne $identity.User.Value -or $scheduled.pid -eq $PID -or
    $scheduled.created -le 0){
  throw 'scheduled_identity_mismatch'
 }
 $report.scheduler_child_admin=[int]$scheduled.admin
 $report.scheduler_exit=[int]$task.LastTaskResult
 $report.scheduler_instances=[int]$task.GetInstances(0).Count
 if($report.scheduler_child_admin -ne 0 -or $report.scheduler_exit -ne 0){
  throw 'scheduled_ordinary_execution_required'
 }
 $report.interactive_recognized=1
} catch {
 $report.winerror=[int]$_.Exception.HResult
} finally {
 if($null -ne $task){
  try {$report.scheduler_instances=[int]$task.GetInstances(0).Count}
  catch {$report.scheduler_instances=-1}
 }
 [IO.File]::WriteAllText((Join-Path $base 'child-result.json'),
  ($report|ConvertTo-Json -Compress))
}
"""

_OUTER = r"""
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$base=@BASE@;$name='mn-'+[Guid]::NewGuid().ToString('N').Substring(0,15)
$report=@{phase='account_create';account_retained=0}
$created=$false;$quiescent=$true;$token=[IntPtr]::Zero
$process=New-Object MuninnCiLogon+Process
try {
 $caller=[Security.Principal.WindowsIdentity]::GetCurrent()
 try {$privileges=[MuninnCiPrivileges]::Read($caller.Token)}
 finally {$caller.Dispose()}
 foreach($key in $privileges.Keys){$report[$key]=$privileges[$key]}
 $password=[Guid]::NewGuid().ToString('N')+'aA7!'
 $secure=ConvertTo-SecureString -String $password -AsPlainText -Force
 $user=New-LocalUser -Name $name -Password $secure -AccountNeverExpires
 $created=$true;$report.account_retained=1
 Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $user
 $acl=New-Object Security.AccessControl.DirectorySecurity
 $acl.SetAccessRuleProtection($true,$false);$acl.SetOwner($user.SID)
 foreach($sid in @($user.SID.Value,'S-1-5-18','S-1-5-32-544')){
  $rule=[Security.AccessControl.FileSystemAccessRule]::new(
   [Security.Principal.SecurityIdentifier]::new($sid),'FullControl',
   'ContainerInherit,ObjectInherit','None','Allow')
  $acl.AddAccessRule($rule)
 }
 Set-Acl -LiteralPath $base -AclObject $acl
 foreach($file in Get-ChildItem -LiteralPath $base -File){
  $fileAcl=New-Object Security.AccessControl.FileSecurity
  $fileAcl.SetAccessRuleProtection($true,$false);$fileAcl.SetOwner($user.SID)
  foreach($sid in @($user.SID.Value,'S-1-5-18','S-1-5-32-544')){
   $fileAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    [Security.Principal.SecurityIdentifier]::new($sid),'FullControl','Allow'))
  }
  Set-Acl -LiteralPath $file.FullName -AclObject $fileAcl
 }
 $xml=Join-Path $base 'task.xml'
 [IO.File]::WriteAllText($xml,
  [IO.File]::ReadAllText($xml).Replace('@SID@',$user.SID.Value),
  [Text.Encoding]::Unicode)
 $report.phase='account_logon'
 if(![MuninnCiLogon]::LogonUserW($name,'.',$password,2,0,[ref]$token)){
  $code=[Runtime.InteropServices.Marshal]::GetLastWin32Error()
  throw [ComponentModel.Win32Exception]::new($code)
 }
 $password=$null;$secure.Dispose()
 $startup=New-Object MuninnCiLogon+Startup
 $startup.cb=[Runtime.InteropServices.Marshal]::SizeOf($startup)
 $startup.desktop=''
 $relative='System32\WindowsPowerShell\v1.0\powershell.exe'
 $exe=Join-Path $env:SystemRoot $relative
 $childArgs=' -NoProfile -NonInteractive -ExecutionPolicy Bypass -File '
 $command=[Text.StringBuilder]::new('"'+$exe+'"'+$childArgs+'"'+
  (Join-Path $base 'child.ps1')+'"')
 $report.phase='account_child_start'
 if(![MuninnCiLogon]::CreateProcessAsUserW($token,$exe,$command,
  [IntPtr]::Zero,[IntPtr]::Zero,$false,0x08000000,[IntPtr]::Zero,$base,
  [ref]$startup,[ref]$process)){
  $code=[Runtime.InteropServices.Marshal]::GetLastWin32Error()
  throw [ComponentModel.Win32Exception]::new($code)
 }
 $quiescent=$false
 if([MuninnCiLogon]::WaitForSingleObject($process.process,60000) -ne 0){
  throw 'ordinary_child_exit_unproven'
 }
 $exitCode=0
 if(![MuninnCiLogon]::GetExitCodeProcess($process.process,[ref]$exitCode) -or
    $exitCode -ne 0){throw 'ordinary_child_exit_failed'}
 $child=Join-Path $base 'child-result.json'
 $parsed=Get-Content -LiteralPath $child -Raw|ConvertFrom-Json
 $report=@{phase=[string]$parsed.phase;account_retained=1}
 foreach($key in $privileges.Keys){$report[$key]=$privileges[$key]}
 foreach($key in @('ordinary_child_admin','scheduler_child_admin',
  'scheduler_exit','scheduler_instances','interactive_recognized')){
  if($null -eq $parsed.$key){throw 'ordinary_child_report_invalid'}
  $report[$key]=[int]$parsed.$key
 }
 if($null -ne $parsed.winerror){$report.winerror=[int]$parsed.winerror}
 $service=New-Object -ComObject Schedule.Service;$service.Connect()
 $folder=$service.GetFolder('\')
 try {$owned=$folder.GetTask((Split-Path -Leaf $base))}
 catch {
  if($_.Exception.HResult -ne -2147024894){throw}
  $owned=$null
 }
 if($null -ne $owned){
  if(!(Test-Owned $owned $base)){throw 'retained_task_definition_mismatch'}
  $owned.Enabled=$false
  if($owned.GetInstances(0).Count -ne 0){throw 'retained_task_still_running'}
  $folder.DeleteTask((Split-Path -Leaf $base),0)
 }
 if($report.scheduler_instances -lt 0){throw 'scheduler_instances_unknown'}
 $quiescent=$report.scheduler_instances -eq 0
} catch {
 $report.winerror=[int]$_.Exception.HResult
 if($_.Exception -is [ComponentModel.Win32Exception]){
  $report.winerror=[int]$_.Exception.NativeErrorCode
 }
} finally {
 $password=$null
 if($process.thread -ne [IntPtr]::Zero){
  [void][MuninnCiLogon]::CloseHandle($process.thread)
 }
 if($process.process -ne [IntPtr]::Zero){
  [void][MuninnCiLogon]::CloseHandle($process.process)
 }
 if($created -and $quiescent){
  try {
   $remaining=Get-LocalUser -Name $name
   if($remaining.SID.Value -ne $user.SID.Value){
    throw 'account_identity_mismatch'
   }
   Remove-LocalUser -SID $user.SID;$report.account_retained=0
  } catch {$report.account_retained=1;$report.winerror=$_.Exception.HResult}
 }
 if($token -ne [IntPtr]::Zero){[void][MuninnCiLogon]::CloseHandle($token)}
}
$report|ConvertTo-Json -Compress
"""


def outer_script(parent: Path) -> str:
    """Build fixed native calls without embedding runtime credentials."""
    return (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
        "Add-Type -TypeDefinition @'\n"
        + _API
        + SOURCE
        + "\n'@\n"
        + _OWNED
        + _OUTER.replace("@BASE@", literal(str(parent)))
    )


def noop_definition(parent: Path) -> bytes:
    """Reuse the product task safety settings with a fixed no-op action."""
    ctx = Ctx(parent, run_real, "noop", platform="win32")
    root = ET.fromstring(
        task_xml(ctx, "@SID@", Path("/unused"), Path("/unused"))
    )
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    action = root.find("t:Actions/t:Exec", ns)
    assert action is not None
    command = action.find("t:Command", ns)
    args = action.find("t:Arguments", ns)
    assert command is not None and args is not None
    destination = literal(str(parent / "task-result.json"))
    script = (
        "$i=[Security.Principal.WindowsIdentity]::GetCurrent();"
        "$p=[Security.Principal.WindowsPrincipal]::new($i);"
        "$a=[Security.Principal.WindowsBuiltInRole]::Administrator;"
        "$r=@{sid=$i.User.Value;admin=[int]$p.IsInRole($a);pid=$PID;"
        "created=[Diagnostics.Process]::GetCurrentProcess().StartTime.Ticks};"
        f"[IO.File]::WriteAllText(({destination}),"
        "($r|ConvertTo-Json -Compress))"
    )
    argv = powershell(script)
    command.text, args.text = argv[0], " ".join(argv[1:])
    return task_bytes(root)


def check_result(report: Mapping[str, object]) -> None:
    """Require actual ordinary children and verified completed task exit."""
    wanted = {
        "ordinary_child_admin": 0,
        "scheduler_child_admin": 0,
        "scheduler_exit": 0,
        "scheduler_instances": 0,
        "interactive_recognized": 1,
        "account_retained": 0,
    }
    if "winerror" in report or any(
        type(report.get(key)) is not int or report[key] != value
        for key, value in wanted.items()
    ):
        raise ValueError("ordinary_interactive_topology_unproven")


def main() -> int:
    """Probe only ephemeral native resources after provider cold routes."""
    if sys.platform != "win32":
        raise OSError("native_windows_required")
    temporary = tempfile.TemporaryDirectory(
        prefix="muninn-account-spike-", delete=False
    )
    parent = Path(temporary.name)
    platform_io.ensure_private_dir(parent)
    (parent / "task.xml").write_bytes(noop_definition(parent))
    (parent / "child.ps1").write_text(_OWNED + _CHILD, encoding="utf-8-sig")
    (parent / "outer.ps1").write_text(
        outer_script(parent), encoding="utf-8-sig"
    )
    write("ordinary", "account_create")
    try:
        argv = powershell("")
        result = subprocess.run(
            [
                argv[0],
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(parent / "outer.ps1"),
            ],
            capture_output=True,
            timeout=90,
            check=True,
        )
        value: object = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("ordinary_report_invalid")
        report = cast(dict[str, object], value)
        phase = report.get("phase")
        if not isinstance(phase, str):
            raise ValueError("ordinary_phase_invalid")
        metrics = {
            key: value
            for key, value in report.items()
            if type(value) is int and key != "winerror"
        }
        error = report.get("winerror")
        diagnostic_error = (
            OSError(error, "native_account_spike_failed")
            if isinstance(error, int)
            else None
        )
        write(
            "ordinary",
            phase,
            error=diagnostic_error,
            metrics=cast(dict[str, int], metrics),
        )
        check_result(report)
    except Exception as exc:
        write("ordinary", None, error=exc)
        print(json.dumps({"retained_state": str(parent)}))
        raise
    temporary.cleanup()
    write("ordinary", "complete", completed=True)
    print(json.dumps({"ordinary_interactive_noop": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
