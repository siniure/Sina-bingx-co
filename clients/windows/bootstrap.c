/* Minimal CRT-free Windows x64 bootstrapper. Source and embedded payload are
 * included for review. RemoteSigned is scoped to its child process only after
 * explicit installer confirmation; user/machine/group policy is not modified.
 * Built with GNU C/binutils, then a standards-based PE import table is filled.
 */
#include <stdint.h>
#define WIN __attribute__((ms_abi))
typedef uint16_t W;
typedef uint32_t D;
typedef void *H;
typedef struct { D cb; W *reserved,*desktop,*title; D x,y,xsize,ysize,xchars,ychars,fill,flags; uint16_t show,reserved_size; void *reserved_bytes; H in,out,err; } SI;
typedef struct { H process,thread; D pid,tid; } PI;
_Static_assert(sizeof(SI)==104,"Windows x64 STARTUPINFO size");
_Static_assert(sizeof(PI)==24,"Windows x64 PROCESS_INFORMATION size");
extern D (WIN *imp_GetTempPathW)(D,W*);
extern D (WIN *imp_GetTempFileNameW)(const W*,const W*,D,W*);
extern int (WIN *imp_DeleteFileW)(const W*);
extern int (WIN *imp_CreateDirectoryW)(const W*,void*);
extern H (WIN *imp_CreateFileW)(const W*,D,D,void*,D,D,H);
extern int (WIN *imp_WriteFile)(H,const void*,D,D*,void*);
extern int (WIN *imp_CloseHandle)(H);
extern D (WIN *imp_GetSystemDirectoryW)(W*,D);
extern W* (WIN *imp_GetCommandLineW)(void);
extern int (WIN *imp_CreateProcessW)(const W*,W*,void*,void*,int,D,void*,const W*,SI*,PI*);
extern D (WIN *imp_WaitForSingleObject)(H,D);
extern int (WIN *imp_GetExitCodeProcess)(H,D*);
extern int (WIN *imp_RemoveDirectoryW)(const W*);
extern void (WIN *imp_ExitProcess)(D);
extern int (WIN *imp_MessageBoxW)(H,const W*,const W*,D);
#include "payload.h"
static W temp[4096],directory[4096],script[4096],payload[4096],test_root[4096],system_path[4096],command[16384];
static int self_test;
static SI startup;
static PI process;
static void copy(W *a,const W *b){while((*a++=*b++)){} }
static void append(W *a,const W *b){while(*a)a++;copy(a,b);}
static int write_blob(const W *path,const unsigned char *bytes,D size){
 H file=imp_CreateFileW(path,0x40000000,0,0,2,0x80,0);
 if(file==(H)(intptr_t)-1)return 0;
 D written=0;int ok=imp_WriteFile(file,bytes,size,&written,0);imp_CloseHandle(file);
 return ok&&written==size;
}
static void fail(void){if(!self_test)imp_MessageBoxW(0,(const W*)u"Sina OCO installer could not complete. No live service was activated. Please use the included installation guide.",(const W*)u"Sina OCO",0x10);imp_ExitProcess(1);}
static int test_mode(void){
 W *p=imp_GetCommandLineW();if(!p)return 0;
 if(*p=='"'){p++;while(*p&&*p!='"')p++;if(*p)p++;}else{while(*p&&*p!=' '&&*p!='\t')p++;}
 while(*p==' '||*p=='\t')p++;
 const W *expected=(const W*)u"--self-test";
 while(*expected&&*p==*expected){p++;expected++;}
 while(*p==' '||*p=='\t')p++;
 return !*expected&&!*p;
}
void WIN entry(void){
 self_test=test_mode();
 if(!self_test&&imp_MessageBoxW(0,(const W*)u"Install Sina OCO research viewer for this user?\n\nThe installer runs its embedded PowerShell script with RemoteSigned for that child process only. It does not change permanent Windows or organizational policy, request administrator access, or activate live OCO. Continue?",(const W*)u"Sina OCO viewer installer",0x24)!=6)imp_ExitProcess(0);
 D temp_length=imp_GetTempPathW(3900,temp);
 if(!temp_length||temp_length>=3900||!imp_GetTempFileNameW(temp,(const W*)u"SOC",0,directory))fail();
 if(!imp_DeleteFileW(directory)||!imp_CreateDirectoryW(directory,0))fail();
 copy(script,directory);append(script,(const W*)u"\\Install.ps1");
 copy(payload,directory);append(payload,(const W*)u"\\client.zip");
 if(!write_blob(script,script_blob,sizeof(script_blob))||!write_blob(payload,zip_blob,sizeof(zip_blob)))fail();
 D system_length=imp_GetSystemDirectoryW(system_path,3800);
 if(!system_length||system_length>=3800)fail();
 append(system_path,(const W*)u"\\WindowsPowerShell\\v1.0\\powershell.exe");
 copy(command,(const W*)u"\"");append(command,system_path);
 append(command,(const W*)u"\" -NoProfile -ExecutionPolicy RemoteSigned -File \"");append(command,script);
 append(command,(const W*)u"\" -PayloadPath \"");append(command,payload);append(command,(const W*)u"\"");
 if(self_test){copy(test_root,directory);append(test_root,(const W*)u"\\test-root");append(command,(const W*)u" -SelfTest -TestRoot \"");append(command,test_root);append(command,(const W*)u"\"");}
 startup.cb=sizeof(startup);
 if(!imp_CreateProcessW(system_path,command,0,0,0,0x08000000,0,directory,&startup,&process))fail();
 imp_WaitForSingleObject(process.process,0xffffffff);
 D result=1;imp_GetExitCodeProcess(process.process,&result);
 imp_CloseHandle(process.thread);imp_CloseHandle(process.process);
 imp_DeleteFileW(script);imp_DeleteFileW(payload);imp_RemoveDirectoryW(directory);
 imp_ExitProcess(result);
}
/* Keep a relocation so ASLR remains enabled even when code layout changes. */
__attribute__((used)) void *sina_relocation_anchor=(void*)&entry;
