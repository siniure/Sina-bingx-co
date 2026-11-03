#!/usr/bin/env python3
"""Build a real Windows x64 viewer installer with GNU C/binutils on Linux.

No Windows execution or publisher signature is claimed. The installer writes
only a per-user dashboard viewer. It does not install/activate the cloud engine.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import re
import struct
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]
IMPORTS={"KERNEL32.dll":["GetTempPathW","GetTempFileNameW","DeleteFileW","CreateDirectoryW","CreateFileW","WriteFile","CloseHandle","GetSystemDirectoryW","GetCommandLineW","CreateProcessW","WaitForSingleObject","GetExitCodeProcess","RemoveDirectoryW","ExitProcess"],"USER32.dll":["MessageBoxW"]}

def payload_zip():
    buffer=io.BytesIO()
    with ZipFile(buffer,"w",ZIP_DEFLATED) as archive:
        for file in sorted((ROOT/"web").iterdir()):
            if file.is_file(): archive.writestr(file.name,file.read_bytes())
        archive.writestr("Uninstall.ps1",(ROOT/"clients/windows/Uninstall.ps1").read_bytes())
        archive.writestr("Uninstall.cmd",'@echo off\r\nrem RemoteSigned applies to this child process only; organization policy is respected.\r\npowershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0Uninstall.ps1"\r\n')
    return buffer.getvalue()

def symbol_addresses(executable):
    output=subprocess.run(["objdump","-t",str(executable)],check=True,capture_output=True,text=True).stdout
    return {match[2]:int(match[1],16) for line in output.splitlines() if (match:=re.search(r"0x([0-9a-fA-F]+)\s+(\S+)\s*$",line))}

def patch_imports(executable, names):
    data=bytearray(executable.read_bytes())
    pe=struct.unpack_from("<I",data,0x3c)[0]
    optional=pe+24
    assert data[pe:pe+4]==b"PE\0\0" and struct.unpack_from("<H",data,optional)[0]==0x20b
    image_base=struct.unpack_from("<Q",data,optional+24)[0]
    section_count=struct.unpack_from("<H",data,pe+6)[0]
    section_table=optional+struct.unpack_from("<H",data,pe+20)[0]
    sections=[]
    for index in range(section_count):
        offset=section_table+index*40
        virtual_size,rva,raw_size,raw=struct.unpack_from("<IIII",data,offset+8)
        sections.append((rva,max(virtual_size,raw_size),raw))
    def raw_for(rva):
        for start,size,raw in sections:
            if start<=rva<start+size:return raw+rva-start
        raise ValueError("RVA outside sections")
    symbols=symbol_addresses(executable)
    base_rva=symbols['sina_import_start']
    # objdump symbols in a PE image are section-relative values, not absolute VAs.
    # Recover the .idata section base explicitly from its symbol table section number.
    dump=subprocess.run(["objdump","-t",str(executable)],check=True,capture_output=True,text=True).stdout
    for line in dump.splitlines():
        if line.rstrip().endswith('sina_import_start'):
            section_number=int(re.search(r"\(sec\s+(\d+)\)",line)[1])
            base_rva+=sections[section_number-1][0]
            break
    base_raw=raw_for(base_rva)
    cursor=(len(IMPORTS)+1)*20
    descriptor_index=0
    iat_offsets=[]
    for dll,functions in IMPORTS.items():
        ilt=cursor;cursor+=(len(functions)+1)*8
        dll_offset=cursor;encoded=dll.encode()+b'\0';data[base_raw+cursor:base_raw+cursor+len(encoded)]=encoded;cursor+=len(encoded)
        if cursor%2:cursor+=1
        hint_rvas=[]
        for function in functions:
            hint=struct.pack('<H',0)+function.encode()+b'\0'
            hint_rvas.append(base_rva+cursor)
            data[base_raw+cursor:base_raw+cursor+len(hint)]=hint;cursor+=len(hint)
            if cursor%2:cursor+=1
        first_iat=None
        for index,(function,hint_rva) in enumerate(zip(functions,hint_rvas)):
            struct.pack_into('<Q',data,base_raw+ilt+index*8,hint_rva)
            # All IAT pointer symbols use the same .idata contribution.
            function_rva=base_rva+symbols['imp_'+function]-symbols['sina_import_start']
            struct.pack_into('<Q',data,raw_for(function_rva),hint_rva)
            iat_offsets.append(function_rva)
            if first_iat is None:first_iat=function_rva
        struct.pack_into('<IIIII',data,base_raw+descriptor_index*20,base_rva+ilt,0,0,base_rva+dll_offset,first_iat)
        descriptor_index+=1
    struct.pack_into('<II',data,optional+112+8,base_rva,(len(IMPORTS)+1)*20)
    first=min(iat_offsets);last=max(iat_offsets)+16
    struct.pack_into('<II',data,optional+112+12*8,first,last-first)
    struct.pack_into('<I',data,optional+64,0) # Optional image checksum; not a signed publisher hash.
    executable.write_bytes(data)

def build(destination):
    zip_data=payload_zip()
    script=(ROOT/"clients/windows/Install.ps1").read_text().replace('$EXPECTED_SHA256',hashlib.sha256(zip_data).hexdigest()).encode('utf-8-sig')
    with tempfile.TemporaryDirectory(prefix='sina-windows-build-') as temporary:
        directory=Path(temporary)
        for name,blob in [('script_blob',script),('zip_blob',zip_data)]:
            with (directory/'payload.h').open('a') as handle:
                handle.write('static const unsigned char '+name+'[]={' + ','.join(map(str,blob))+'};\n')
        (directory/'bootstrap.c').write_bytes((ROOT/'clients/windows/bootstrap.c').read_bytes())
        assembler=['.section .idata,"aw"','.global sina_import_start','sina_import_start:','.space 2048']
        for dll,functions in IMPORTS.items():
            for name in functions:assembler += ['.global imp_'+name,'imp_'+name+':','.quad 0']
            assembler += ['.quad 0']
        (directory/'imports.s').write_text('\n'.join(assembler)+'\n')
        commands=[
            ['gcc','-c','-Os','-std=c11','-mcmodel=large','-ffreestanding','-fno-builtin','-fno-pic','-fno-pie','-fno-stack-protector','-fno-asynchronous-unwind-tables','-fno-unwind-tables','-mno-red-zone','bootstrap.c','-o','bootstrap.elf.o'],
            ['as','--64','imports.s','-o','imports.elf.o'],
            ['objcopy','-O','pe-x86-64','--remove-section','.note.GNU-stack','bootstrap.elf.o','bootstrap.obj'],
            ['objcopy','-O','pe-x86-64','imports.elf.o','imports.obj'],
            ['ld','-mi386pep','--subsystem','windows','--entry','entry','--image-base','0x140000000','--dynamicbase','--nxcompat','--high-entropy-va','--enable-reloc-section','--major-os-version','6','--major-subsystem-version','6','-o','SinaOCO_Setup_x64.exe','bootstrap.obj','imports.obj']]
        for command in commands:subprocess.run(command,cwd=directory,check=True,capture_output=True)
        executable=directory/'SinaOCO_Setup_x64.exe'
        patch_imports(executable,IMPORTS)
        inspection=subprocess.run(['objdump','-p',str(executable)],check=True,capture_output=True,text=True).stdout
        for dll,functions in IMPORTS.items():
            assert dll in inspection
            for function in functions:assert function in inspection,function
        assert 'PE32+' in inspection and 'DYNAMIC_BASE' in inspection and 'NX_COMPAT' in inspection
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes(executable.read_bytes())
        destination.with_suffix('.inspection.txt').write_text(inspection)
    status={'artifact':str(destination),'format':'PE32+ Windows GUI x64','sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
            'pe_structure_and_imports_verified':True,'windows_execution_tested':False,'publisher_signed':False,
            'powershell_policy':'Child process RemoteSigned after installer confirmation; no permanent or group-policy changes',
            'installs':'Per-user dashboard viewer only; no Python engine, market feed or cloud activation'}
    destination.with_suffix('.build-status.json').write_text(json.dumps(status,indent=2)+'\n')
    return status

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    print(json.dumps(build(parser.parse_args().output),indent=2))
