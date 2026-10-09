"""Read-only native privilege declarations for the post-cold CI probe."""

from __future__ import annotations

SOURCE = r"""
public static class MuninnCiPrivileges {
 [System.Runtime.InteropServices.StructLayout(
  System.Runtime.InteropServices.LayoutKind.Sequential)]
 public struct Luid {public int low,high;}
 [System.Runtime.InteropServices.DllImport(
  "advapi32.dll",SetLastError=true)]
 public static extern bool GetTokenInformation(IntPtr token,int kind,
  IntPtr data,int length,out int required);
 [System.Runtime.InteropServices.DllImport("advapi32.dll",
  CharSet=System.Runtime.InteropServices.CharSet.Unicode,SetLastError=true)]
 public static extern bool LookupPrivilegeValueW(
  string system,string name,out Luid luid);
 public static System.Collections.Generic.Dictionary<string,int> Read(
  IntPtr token) {
  var result=new System.Collections.Generic.Dictionary<string,int>();
  int size;
  GetTokenInformation(token,3,IntPtr.Zero,0,out size);
  int error=System.Runtime.InteropServices.Marshal.GetLastWin32Error();
  if(error!=122) throw new System.ComponentModel.Win32Exception(error);
  if(size<4 || size>65536) throw new ArgumentException("token_size_invalid");
  IntPtr data=System.Runtime.InteropServices.Marshal.AllocHGlobal(size);
  try {
   if(!GetTokenInformation(token,3,data,size,out size))
    throw new System.ComponentModel.Win32Exception(
     System.Runtime.InteropServices.Marshal.GetLastWin32Error());
   int count=System.Runtime.InteropServices.Marshal.ReadInt32(data);
   if(count<0 || count>512 || 4+count*12>size)
    throw new ArgumentException("token_count_invalid");
   string[] names={"SeIncreaseQuotaPrivilege",
    "SeAssignPrimaryTokenPrivilege","SeImpersonatePrivilege"};
   string[] keys={"quota","assign","impersonate"};
   for(int index=0;index<names.Length;index++) {
    Luid wanted;
    if(!LookupPrivilegeValueW(null,names[index],out wanted))
     throw new System.ComponentModel.Win32Exception(
      System.Runtime.InteropServices.Marshal.GetLastWin32Error());
    int present=0,enabled=0;
    for(int item=0;item<count;item++) {
     int offset=4+12*item;
     int low=System.Runtime.InteropServices.Marshal.ReadInt32(data,offset);
     int high=System.Runtime.InteropServices.Marshal.ReadInt32(data,offset+4);
     if(low!=wanted.low || high!=wanted.high) continue;
     int attributes=System.Runtime.InteropServices.Marshal.ReadInt32(
      data,offset+8);
     present=1;enabled=(attributes&2)!=0?1:0;
    }
    result["caller_"+keys[index]+"_present"]=present;
    result["caller_"+keys[index]+"_enabled"]=enabled;
   }
  } finally {System.Runtime.InteropServices.Marshal.FreeHGlobal(data);}
  return result;
 }
}
"""
