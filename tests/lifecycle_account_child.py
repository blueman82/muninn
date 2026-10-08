"""Fixed ordinary-account child scripts and numeric bootstrap diagnostics."""

from __future__ import annotations

from pathlib import Path

from muninn.obs_service import literal
from tests.lifecycle_account_scripts import OWNED

CHILD = r"""
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
 $report.ordinary_child_session=[Diagnostics.Process]::GetCurrentProcess().SessionId
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
 $report.scheduler_child_session=[int]$scheduled.session
 if($report.scheduler_child_session -ne $report.ordinary_child_session){
  throw 'scheduled_session_mismatch'
 }
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


_PROBE = r"""
$childModuleReadable=-1;$childModuleHResult=0;$moduleStream=$null
try {
 $manifest=[IO.Path]::Combine($PSHOME,'Modules',
  'Microsoft.PowerShell.Management','Microsoft.PowerShell.Management.psd1')
 $moduleStream=[IO.File]::OpenRead($manifest)
 [void]$moduleStream.ReadByte();$childModuleReadable=1
} catch {
 $childModuleReadable=0
 $childModuleHResult=[int]$_.Exception.GetBaseException().HResult
} finally {
 if($null -ne $moduleStream){
  try {$moduleStream.Dispose()}
  catch {$childModuleReadable=0
   $childModuleHResult=[int]$_.Exception.GetBaseException().HResult}
 }
}
"""

EXCEPTION_KIND = r"""
function Get-ChildExceptionKind($failure){
 if($null -eq $failure){return -1}
 if($failure -is [UnauthorizedAccessException]){return 1}
 if($failure -is [Security.SecurityException]){return 2}
 if($failure -is [Runtime.InteropServices.COMException]){return 3}
 if($failure -is [ComponentModel.Win32Exception]){return 4}
 if($failure -is [Management.Automation.RuntimeException]){return 5}
 return 0
}
"""


_TRAP = r"""
trap {try {
 $originalChildError=$_
 $nativeChildFailure=$originalChildError.Exception.GetBaseException()
 $childExceptionKind=-1
 try {$childExceptionKind=Get-ChildExceptionKind $nativeChildFailure}
 catch {}
 $line=[int]$originalChildError.InvocationInfo.ScriptLineNumber
 $childCategory=-1;$childCommand=-1
 try {
  if($null -ne $originalChildError.CategoryInfo){
   $childCategory=[int]$originalChildError.CategoryInfo.Category
  }
  if($null -ne $originalChildError.InvocationInfo.MyCommand){
   $childCommand=[int]$originalChildError.InvocationInfo.MyCommand.CommandType
  }
 } catch {}
 $data='{"stage":'+$childStage+',"hresult":'+
  [int]$nativeChildFailure.HResult+',"line":'+$line+
  ',"child_module_readable":'+$childModuleReadable+
  ',"child_module_hresult":'+$childModuleHResult+
  ',"child_error_category":'+$childCategory+
  ',"child_command_type":'+$childCommand+
  ',"child_exception_kind":'+$childExceptionKind+'}'
 $path=[IO.Path]::Combine($childBase,'child-failure.json')
 if(![IO.File]::Exists($path)){[IO.File]::WriteAllText($path,$data)}
} catch {} finally {exit 1}}
"""


def child_script(parent: Path) -> str:
    """Retain uncaught child codes while explicitly preserving exit one."""
    header = (
        f"$childBase={literal(str(parent))};$childStage=1\n"
        + EXCEPTION_KIND
        + _TRAP
        + _PROBE
    )
    body = (
        CHILD.replace(
            "$base=Split-Path -Parent $MyInvocation.MyCommand.Path",
            "Microsoft.PowerShell.Core\\Import-Module -Name $manifest "
            "-ErrorAction Stop\n"
            "$base=Microsoft.PowerShell.Management\\Split-Path "
            "-Parent $MyInvocation.MyCommand.Path",
        )
        .replace(
            "$name=Split-Path -Leaf $base",
            "$childStage=2;$name=Split-Path -Leaf $base",
        )
        .replace(
            " [IO.File]::WriteAllText((Join-Path $base 'child-result.json'),",
            " $childStage=3\n"
            " [IO.File]::WriteAllText((Join-Path $base 'child-result.json'),",
        )
    )
    return OWNED + header + body
