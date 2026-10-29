"""Build a self-contained folder and per-user NSIS installer from an allowlist.

Run with an isolated Python containing packaging-requirements.txt; NSIS must be
an explicitly supplied compiler. This script never reads local Agent homes.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
VERSION = '0.6.4'
EXCLUDED = ['pytest', '_pytest', 'httpx', 'IPython', 'tkinter', 'unittest', 'doctest',
            'agenthub_adapters.appserver_stub', 'agenthub_adapters.mock', 'agenthub_rt.dev_auth', 'tests', 'tools']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = output/'build'
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir',
        '--console' if args.console else '--windowed',
        '--noupx','--name','AgentHub','--icon',str(ROOT/'apps/api/agenthub_rt/static/brand/app.ico'),
        '--distpath',str(output/'payload'),'--workpath',str(work),
        '--specpath',str(work), '--add-data',f'{ROOT / "apps/api/agenthub_rt/static"};agenthub_rt/static',
        '--hidden-import','sqlalchemy.dialects.sqlite','--hidden-import','uvicorn.loops.asyncio',
        '--hidden-import','uvicorn.protocols.http.h11_impl','--hidden-import','uvicorn.lifespan.on',
        # P6: the PDF text extractor is imported lazily inside the paper parser.
        '--collect-submodules','pypdf','--hidden-import','agenthub_kernel.papers.db',
        '--copy-metadata','pydantic','--copy-metadata','fastapi', '--copy-metadata','uvicorn']
    for path in ['packages/contracts','packages/adapters','apps/worker','apps/api']:
        command += ['--paths',str(ROOT/path)]
    for module in EXCLUDED:
        command += ['--exclude-module',module]
    command += [str(ROOT/'tools/distribution/entry.py')]
    subprocess.run(command,check=True)
    payload=output/'payload/AgentHub'
    if os.name == 'nt':
        subprocess.run(['icacls',str(payload/'AgentHub.exe'),'/setintegritylevel','M'],check=True,capture_output=True)
    import shutil
    shutil.copyfile(ROOT/'README.md',payload/'README.md')
    # README in the repository and the installed folder both show the same art.
    readme=(payload/'README.md').read_text(encoding='utf-8').replace(
        'apps/api/agenthub_rt/static/brand/', '_internal/agenthub_rt/static/brand/')
    (payload/'README.md').write_text(readme,encoding='utf-8')
    # The release uses local pairing only; remove development defaults and its
    # unused token field placeholder from the actual packaged HTML resources.
    for page in (payload/'_internal/agenthub_rt/static').glob('*.html'):
        source=page.read_text(encoding='utf-8')
        for token in ('dev-user-token','dev-agent-token','dev-worker-token'):
            source=source.replace(token,'')
        page.write_text(source,encoding='utf-8')
    licenses = payload/'THIRD_PARTY_LICENSES'; licenses.mkdir(exist_ok=True)
    for dist in importlib.metadata.distributions():
        if dist.metadata['Name'].lower() in {'pip','setuptools','pyinstaller','pyinstaller-hooks-contrib',
                    'altgraph','pefile','pywin32-ctypes','packaging'}:
            continue
        for item in dist.files or ():
            if item.name.lower().startswith(('license','copying','notice')) and item.suffix.lower() != '.py':
                source=Path(dist.locate_file(item))
                if source.is_file():
                    shutil.copyfile(source,licenses/(dist.metadata['Name']+'-'+item.name))
    shutil.copyfile(ROOT/'apps/api/agenthub_rt/static/vendor/katex/LICENSE',licenses/'KaTeX-LICENSE.txt')
    pylicense=Path(sys.base_prefix)/'LICENSE.txt'
    if pylicense.is_file(): shutil.copyfile(pylicense,licenses/'Python-LICENSE.txt')
    shutil.copyfile(Path(args.nsis).parent/'COPYING',licenses/'NSIS-COPYING.txt')
    audit(payload,work)
    files=sorted(p for p in payload.rglob('*') if p.is_file())
    manifest={'version':VERSION,'platform':'windows-x64','source':subprocess.check_output(
        ['git','-c',f'safe.directory={ROOT.as_posix()}','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'packages':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()},
        'files':[{'path':p.relative_to(payload).as_posix(),'size':p.stat().st_size,'sha256':sha(p)} for p in files]}
    (payload/'MANIFEST.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    installer=output/f'AgentHub-{VERSION}-windows-x64-setup.exe'
    staging_installer=output/f'AgentHub-{VERSION}-candidate-{os.getpid()}.exe'
    script=output/'installer.nsi'
    # Uninstaller deletes only the exact released files, never recursively
    # removes a directory chosen by a user.
    delete='\n'.join('  Delete "$INSTDIR\\'+str(p.relative_to(payload))+'"' for p in payload.rglob('*') if p.is_file())
    dirs=sorted((p for p in payload.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True)
    delete+='\n'+'\n'.join('  RMDir "$INSTDIR\\'+str(p.relative_to(payload))+'"' for p in dirs)
    template=(ROOT/'tools/distribution/installer.nsi.in').read_text(encoding='utf-8')
    for key,value in {'@VERSION@':VERSION,'@PAYLOAD@':str(payload),'@OUTPUT@':str(staging_installer),
                      '@ICON@':str(ROOT/'apps/api/agenthub_rt/static/brand/app.ico'),
                      '@DELETE@':delete}.items():template=template.replace(key,value)
    script.write_text(template,encoding='utf-8-sig')
    compiler_tmp=output/'compiler-tmp'; compiler_tmp.mkdir(exist_ok=True)
    subprocess.run([args.nsis,'/V2',str(script)],check=True,
                   env={**os.environ,'TMP':str(compiler_tmp),'TEMP':str(compiler_tmp)})
    os.replace(staging_installer,installer)
    if os.name == 'nt':
        subprocess.run(['icacls',str(installer),'/setintegritylevel','M'],check=True,capture_output=True)
    archive=output/f'AgentHub-{VERSION}-windows-x64-portable.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in payload.rglob('*'):
            if p.is_file():z.write(p,'AgentHub/'+p.relative_to(payload).as_posix())
    if os.name == 'nt':
        # ZIP is also a user-facing artifact; do not leave an inherited Low
        # label from a sandboxed build folder on the released archive.
        subprocess.run(['icacls',str(archive),'/setintegritylevel','M'],check=True,capture_output=True)
    (output/'SHA256SUMS.txt').write_text('\n'.join(f'{sha(p)}  {p.name}' for p in [installer,archive])+'\n')
    print(json.dumps({'installer':str(installer),'zip':str(archive),'files':len(files)},indent=2))


def audit(payload,work):
    forbidden={'.git','.env','.venv','node_modules','var','evidence','tests','__pycache__','.pytest_cache'}
    for p in payload.rglob('*'):
        if not p.is_file():continue
        parts=set(p.relative_to(payload).parts)
        if parts&forbidden or p.name.lower() in {'auth.json','credentials.json','profile.json'} or p.suffix in {'.db','.log','.sqlite'}:
            raise RuntimeError('Forbidden release file: '+str(p.relative_to(payload)))
    # Inspect the actual embedded Python module table, not only visible files.
    toc=(work/'AgentHub/Analysis-00.toc').read_text(encoding='utf-8')
    for name in ['pytest','_pytest','agenthub_adapters.appserver_stub','agenthub_adapters.mock','agenthub_rt.dev_auth']:
        if "('"+name+"'" in toc:
            raise RuntimeError('Forbidden bundled module: '+name)
    (payload/'PACKAGE_AUDIT.json').write_text(json.dumps({'source':'allowlist + embedded module table',
        'excluded':EXCLUDED,'credentials_included':False,'runtime_data_included':False},indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--nsis',required=True)
    parser.add_argument('--console',action='store_true',help='Diagnostic build with a console')
    build(parser.parse_args())
