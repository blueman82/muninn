"""Native declarations for a CI-only private noninteractive desktop."""

from __future__ import annotations

PROCESS_SOURCE = r"""
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
 public static extern bool CreateProcessWithTokenW(IntPtr token,int logonFlags,
  string executable,StringBuilder command,int flags,IntPtr env,string cwd,
  ref Startup startup,out Process process);
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern int WaitForSingleObject(IntPtr handle,int milliseconds);
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern bool GetExitCodeProcess(IntPtr process,out int code);
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern bool CloseHandle(IntPtr handle);
}
"""

SOURCE = r"""
public static class MuninnCiDesktop {
 [StructLayout(LayoutKind.Sequential)]
 public struct SecurityAttributes {
  public int length;public IntPtr descriptor;
  [MarshalAs(UnmanagedType.Bool)] public bool inherit;
 }
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 public static extern bool
 ConvertStringSecurityDescriptorToSecurityDescriptorW(
  string descriptor,int revision,out IntPtr result,out int size);
 [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 public static extern IntPtr CreateWindowStationW(string name,int flags,
  int access,ref SecurityAttributes attributes);
 [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 public static extern IntPtr CreateDesktopW(string name,string device,
  IntPtr mode,int flags,int access,ref SecurityAttributes attributes);
 [DllImport("user32.dll",SetLastError=true)]
 public static extern IntPtr GetProcessWindowStation();
 [DllImport("user32.dll",SetLastError=true)]
 public static extern IntPtr GetThreadDesktop(int thread);
 [DllImport("user32.dll",SetLastError=true)]
 public static extern bool SetProcessWindowStation(IntPtr station);
 [DllImport("user32.dll",SetLastError=true)]
 public static extern bool SetThreadDesktop(IntPtr desktop);
 [DllImport("user32.dll",SetLastError=true)]
 public static extern bool CloseWindowStation(IntPtr station);
 [DllImport("user32.dll",SetLastError=true)]
 public static extern bool CloseDesktop(IntPtr desktop);
 [DllImport("kernel32.dll")] public static extern int GetCurrentThreadId();
 [DllImport("kernel32.dll")]
 public static extern IntPtr LocalFree(IntPtr data);
 public static IntPtr Station=IntPtr.Zero,Desktop=IntPtr.Zero;
 public static bool Restored=true;
 public static bool StationRestoreOk,DesktopRestoreOk;
 public static bool StationIdentityOk,DesktopIdentityOk;
 public static bool DesktopIdentityBefore,DesktopRestoreCalled;
 public static int StationRestoreError,DesktopRestoreError;
 static void Required(bool valid) {
  if(!valid) throw new System.ComponentModel.Win32Exception(
   Marshal.GetLastWin32Error());
 }
 public static string Prepare(string account,string caller) {
  const int CWF_CREATE_ONLY=1;
  IntPtr originalStation=GetProcessWindowStation();
  int originalThread=GetCurrentThreadId();
  IntPtr originalDesktop=GetThreadDesktop(originalThread);
  Required(originalStation!=IntPtr.Zero && originalDesktop!=IntPtr.Zero);
  string stationName="mn-"+Guid.NewGuid().ToString("N");
  string desktopName="mn-"+Guid.NewGuid().ToString("N");
  string sddl="O:"+caller+"D:P(A;;GA;;;"+account+")"+
   "(A;;GA;;;"+caller+")(A;;GA;;;SY)(A;;GA;;;BA)";
  IntPtr security;int size;
  Required(ConvertStringSecurityDescriptorToSecurityDescriptorW(
   sddl,1,out security,out size));
  try {
   var attributes=new SecurityAttributes();
   attributes.length=Marshal.SizeOf(typeof(SecurityAttributes));
   attributes.descriptor=security;attributes.inherit=false;
   Station=CreateWindowStationW(stationName,CWF_CREATE_ONLY,
    0x000F037F,ref attributes);
   Required(Station!=IntPtr.Zero);
   Restored=false;
   try {
    Required(SetProcessWindowStation(Station));
    Desktop=CreateDesktopW(desktopName,null,IntPtr.Zero,0,
     0x000F01FF,ref attributes);
    Required(Desktop!=IntPtr.Zero);
   } finally {
    StationRestoreOk=SetProcessWindowStation(originalStation);
    StationRestoreError=StationRestoreOk ? 0 : Marshal.GetLastWin32Error();
    DesktopIdentityBefore=StationRestoreOk &&
     GetProcessWindowStation()==originalStation &&
     originalDesktop!=IntPtr.Zero &&
     GetThreadDesktop(originalThread)==originalDesktop;
    DesktopRestoreCalled=!DesktopIdentityBefore;
    DesktopRestoreOk=DesktopIdentityBefore;DesktopRestoreError=0;
    if(DesktopRestoreCalled) {
     DesktopRestoreOk=SetThreadDesktop(originalDesktop);
     DesktopRestoreError=DesktopRestoreOk ? 0 : Marshal.GetLastWin32Error();
    }
    StationIdentityOk=GetProcessWindowStation()==originalStation;
    DesktopIdentityOk=GetThreadDesktop(originalThread)==originalDesktop;
    Restored=StationRestoreOk && DesktopRestoreOk &&
     StationIdentityOk && DesktopIdentityOk;
   }
   if(!Restored)
    throw new InvalidOperationException("desktop_restore_unproven");
   return stationName+"\\"+desktopName;
  } finally {LocalFree(security);}
 }
 public static void CloseOwned() {
  if(!Restored)
    throw new InvalidOperationException("desktop_restore_unproven");
  if(Desktop!=IntPtr.Zero) {
   Required(CloseDesktop(Desktop));Desktop=IntPtr.Zero;
  }
  if(Station!=IntPtr.Zero) {
   Required(CloseWindowStation(Station));Station=IntPtr.Zero;
  }
 }
 public static int Session(IntPtr token) {
  IntPtr data=Marshal.AllocHGlobal(4);
  try {
   int size;
   Required(MuninnCiPrivileges.GetTokenInformation(token,12,data,4,out size));
   if(size!=4) throw new ArgumentException("token_session_invalid");
   return Marshal.ReadInt32(data);
  } finally {Marshal.FreeHGlobal(data);}
 }
}
"""
