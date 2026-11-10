"""sshleaf: a personal Overleaf-like LaTeX editor.

Projects live on this machine ("local") or on any host in ~/.ssh/config.
Files are read and written in place (over SSH for remote hosts). To compile,
the project is rsync'ed into a local mirror and built with tectonic here.
"""

import argparse
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .synctex import SyncTex

HOME = Path.home()
CACHE = HOME / ".cache" / "sshleaf"
STATE_FILE = HOME / ".config" / "sshleaf" / "state.json"
STATIC = Path(__file__).parent / "static"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
# SSH control sockets: unix socket paths are limited to ~104 bytes (%C expands to 40 chars)
CONTROL_DIR = CACHE / "cm"
if len(str(CONTROL_DIR)) > 60:
    CONTROL_DIR = Path(f"/tmp/sshleaf-{os.getuid()}")
CONTROL_DIR.mkdir(parents=True, exist_ok=True)
CONTROL_DIR.chmod(0o700)

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={CONTROL_DIR}/%C",
    "-o", "ControlPersist=600",
]
EXCLUDE = [".git", ".sshleaf-trash", "__pycache__", "node_modules", ".DS_Store", ".venv"]
TRASH = ".sshleaf-trash"

app = FastAPI()
state_lock = threading.Lock()
compile_locks: dict[str, threading.Lock] = {}
PORT = 8765
SSH_CONFIG = HOME / ".ssh" / "config"


# ---------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"projects": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def project_id(host, root):
    return hashlib.sha1(f"{host}:{root}".encode()).hexdigest()[:12]


def get_project(pid):
    with state_lock:
        project = load_state()["projects"].get(pid)
    if project is None:
        raise HTTPException(404, f"unknown project {pid}")
    return project


def update_project(pid, **fields):
    with state_lock:
        state = load_state()
        state["projects"][pid].update(fields)
        save_state(state)
        return state["projects"][pid]


# ---------------------------------------------------------------- shell

def run(host, cmd, data=None, timeout=60):
    """Run a POSIX shell command locally or on an SSH host; return stdout bytes."""
    args = ["bash", "-c", cmd] if host == "local" else ["ssh", *SSH_OPTS, host, cmd]
    proc = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise HTTPException(500, proc.stderr.decode(errors="replace").strip() or f"exit {proc.returncode}")
    return proc.stdout


def shell_path(path):
    """Quote a path for the shell, keeping a leading ~ expandable."""
    if path in ("", "~"):
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def safe_rel(rel):
    rel = posixpath.normpath(rel.strip().lstrip("/"))
    if rel in (".", "") or rel == ".." or rel.startswith("../"):
        raise HTTPException(400, f"bad path {rel!r}")
    return rel


def full_path(project, rel):
    return posixpath.join(project["root"], safe_rel(rel))


def ssh_hosts():
    config = SSH_CONFIG
    hosts = []
    if config.exists():
        for line in config.read_text().splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0].lower() == "host":
                for name in parts[1:]:
                    if not any(ch in name for ch in "*?!") and name not in hosts:
                        hosts.append(name)
    return hosts


# ---------------------------------------------------------------- security

@app.middleware("http")
async def guard(request: Request, call_next):
    # Block DNS rebinding and cross-site requests: only our own origin may call the API.
    host = request.headers.get("host", "")
    if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        return JSONResponse({"detail": "bad host"}, status_code=403)
    if request.url.path.startswith("/api/") and request.headers.get("x-sshleaf") != "1":
        return JSONResponse({"detail": "missing header"}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------- browsing

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/hosts")
def hosts():
    return ["local", *ssh_hosts()]


@app.get("/api/ls")
def list_dir(host: str, path: str = "~"):
    out = run(host, f"cd {shell_path(path)} && pwd && ls -1Ap").decode(errors="replace").splitlines()
    entries = [{"name": name.rstrip("/"), "dir": name.endswith("/")} for name in out[1:] if name]
    entries.sort(key=lambda entry: (not entry["dir"], entry["name"].lower()))
    return {"path": out[0], "entries": entries}


@app.get("/api/projects")
def projects():
    with state_lock:
        items = list(load_state()["projects"].values())
    return sorted(items, key=lambda project: -project.get("opened", 0))


@app.post("/api/projects/forget")
def forget(pid: str = Body(..., embed=True)):
    with state_lock:
        state = load_state()
        state["projects"].pop(pid, None)
        save_state(state)
    return {"ok": True}


@app.post("/api/open")
def open_project(host: str = Body(...), root: str = Body(...)):
    root = run(host, f"cd {shell_path(root)} && pwd").decode().strip()
    pid = project_id(host, root)
    with state_lock:
        state = load_state()
        project = state["projects"].get(pid) or {"id": pid, "host": host, "root": root, "main": None}
        project["opened"] = time.time()
        state["projects"][pid] = project
        save_state(state)
    listing = tree(pid)
    if not project["main"] or project["main"] not in listing["files"]:
        project = update_project(pid, main=guess_main(project, listing["files"]))
    return {**project, **listing}


def guess_main(project, files):
    tex = [name for name in files if name.endswith(".tex")]
    if not tex:
        return None
    quoted = " ".join(shlex.quote(name) for name in tex[:200])
    out = run(project["host"], f"cd {shlex.quote(project['root'])} && grep -l '^[^%]*\\\\documentclass' -- {quoted} || true")
    candidates = out.decode().split() or tex
    for preferred in ("main.tex", "paper.tex"):
        if preferred in candidates:
            return preferred
    return sorted(candidates, key=lambda name: (name.count("/"), name))[0]


@app.get("/api/tree")
def tree(pid: str):
    project = get_project(pid)
    prune = " -o ".join(f"-name {shlex.quote(name)}" for name in EXCLUDE)
    find = f"find . \\( {prune} \\) -prune -o"
    cmd = (f"cd {shlex.quote(project['root'])} && {find} -type d -print | head -n 5000"
           f" && echo '::' && {find} -type f -print | head -n 20000")
    out = run(project["host"], cmd).decode(errors="replace")
    dirs_part, _, files_part = out.partition("::\n")
    strip = lambda text: sorted(line[2:] for line in text.splitlines() if line.startswith("./"))
    return {"dirs": strip(dirs_part), "files": strip(files_part)}


# ---------------------------------------------------------------- files

def digest(data: bytes):
    return hashlib.sha1(data).hexdigest()


@app.get("/api/file")
def read_file(pid: str, path: str):
    project = get_project(pid)
    data = run(project["host"], f"cat -- {shlex.quote(full_path(project, path))}")
    return Response(data, media_type="application/octet-stream", headers={"X-Hash": digest(data)})


@app.get("/api/hash")
def file_hash(pid: str, path: str):
    project = get_project(pid)
    data = run(project["host"], f"cat -- {shlex.quote(full_path(project, path))} 2>/dev/null || true")
    return {"hash": digest(data)}


@app.put("/api/file")
async def write_file(request: Request, pid: str, path: str, base: str = "", force: bool = False):
    project = get_project(pid)
    data = await request.body()
    target = full_path(project, path)
    if base and not force:
        current = file_hash(pid, path)["hash"]
        if current != base:
            raise HTTPException(409, "file changed on disk")
    temp = f"{target}.sshleaf-tmp"
    run(project["host"],
        f"mkdir -p -- {shlex.quote(posixpath.dirname(target))} && cat > {shlex.quote(temp)}"
        f" && mv -f -- {shlex.quote(temp)} {shlex.quote(target)}", data=data)
    return {"hash": digest(data)}


@app.post("/api/mkdir")
def make_dir(pid: str = Body(...), path: str = Body(...)):
    project = get_project(pid)
    run(project["host"], f"mkdir -p -- {shlex.quote(full_path(project, path))}")
    return {"ok": True}


@app.post("/api/move")
def move(pid: str = Body(...), src: str = Body(...), dst: str = Body(...)):
    project = get_project(pid)
    source, target = shlex.quote(full_path(project, src)), full_path(project, dst)
    run(project["host"],
        f"test ! -e {shlex.quote(target)} && mkdir -p -- {shlex.quote(posixpath.dirname(target))}"
        f" && mv -- {source} {shlex.quote(target)}")
    if project.get("main") == safe_rel(src):
        update_project(pid, main=safe_rel(dst))
    return {"ok": True}


@app.post("/api/trash")
def trash(pid: str = Body(...), path: str = Body(...)):
    """Deleting moves the item into <project>/.sshleaf-trash/ (nothing is erased)."""
    project = get_project(pid)
    rel = safe_rel(path)
    trash_dir = posixpath.join(project["root"], TRASH)
    target = posixpath.join(trash_dir, time.strftime("%Y%m%d-%H%M%S-") + rel.replace("/", "__"))
    run(project["host"],
        f"mkdir -p -- {shlex.quote(trash_dir)} && mv -- {shlex.quote(full_path(project, rel))} {shlex.quote(target)}")
    return {"ok": True}


@app.post("/api/main")
def set_main(pid: str = Body(...), main: str = Body(...)):
    return update_project(pid, main=safe_rel(main))


# ---------------------------------------------------------------- compile

DIAG = re.compile(r"^(error|warning): (?:(.+?):(\d+): )?(.*)$")


def mirror_dir(pid):
    return CACHE / "mirror" / pid


def build_dir(pid):
    return CACHE / "build" / pid


def sync_mirror(project):
    mirror = mirror_dir(project["id"])
    mirror.mkdir(parents=True, exist_ok=True)
    excludes = [arg for name in EXCLUDE for arg in ("--exclude", name)]
    excludes += ["--exclude", "*.sshleaf-tmp"]
    if project["host"] == "local":
        source, extra = project["root"].rstrip("/") + "/", []
    else:
        source = f"{project['host']}:{project['root'].rstrip('/')}/"
        extra = ["-e", "ssh " + " ".join(shlex.quote(opt) for opt in SSH_OPTS)]
    proc = subprocess.run(["rsync", "-rlt", "--delete", *extra, *excludes, source, f"{mirror}/"],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise HTTPException(500, "rsync failed: " + proc.stderr)
    return mirror


@app.post("/api/compile")
def compile_project(pid: str = Body(..., embed=True)):
    project = get_project(pid)
    if not project.get("main"):
        raise HTTPException(400, "no main .tex file set")
    lock = compile_locks.setdefault(pid, threading.Lock())
    with lock:
        started = time.time()
        mirror = sync_mirror(project)
        build = build_dir(pid)
        build.mkdir(parents=True, exist_ok=True)
        main = project["main"]
        main_dir = posixpath.dirname(main)
        proc = subprocess.run(
            ["tectonic", "-X", "compile", posixpath.basename(main), "--outdir", str(build),
             "--synctex", "--keep-logs", "--keep-intermediates"],
            cwd=mirror / main_dir, capture_output=True, text=True, timeout=600,
        )
        output = proc.stdout + proc.stderr
        diagnostics, seen = [], set()
        for line in output.splitlines():
            match = DIAG.match(line)
            if not match:
                continue
            level, file, lineno, message = match.groups()
            if file:
                file = posixpath.normpath(posixpath.join(main_dir, file))
            key = (level, file, lineno, message)
            if key in seen:
                continue
            seen.add(key)
            kind = "badbox" if re.match(r"(Under|Over)full", message) else level
            diagnostics.append({"level": kind, "file": file, "line": int(lineno) if lineno else None,
                                "message": message})
        # tectonic adds generic "XeTeX had an error" lines; drop them when a located error exists
        if any(d["level"] == "error" and d["file"] for d in diagnostics):
            diagnostics = [d for d in diagnostics if d["level"] != "error" or d["file"]]
        stem = posixpath.splitext(posixpath.basename(main))[0]
        log_file = build / f"{stem}.log"
        pdf = build / f"{stem}.pdf"
        return {
            "ok": proc.returncode == 0,
            "seconds": round(time.time() - started, 2),
            "diagnostics": diagnostics,
            "output": output[-20000:],
            "log": log_file.read_text(errors="replace")[-200000:] if log_file.exists() else "",
            "has_pdf": pdf.exists(),
        }


def built_file(project, ext):
    stem = posixpath.splitext(posixpath.basename(project["main"] or "main"))[0]
    return build_dir(project["id"]) / f"{stem}{ext}"


@app.get("/api/pdf")
def get_pdf(pid: str):
    pdf = built_file(get_project(pid), ".pdf")
    if not pdf.exists():
        raise HTTPException(404, "not compiled yet")
    return FileResponse(pdf, media_type="application/pdf", headers={"Cache-Control": "no-store"})


@app.post("/api/push-pdf")
def push_pdf(pid: str = Body(..., embed=True)):
    """Copy the compiled PDF next to the main file in the project."""
    project = get_project(pid)
    pdf = built_file(project, ".pdf")
    target = posixpath.join(project["root"], posixpath.dirname(project["main"]), pdf.name)
    run(project["host"], f"cat > {shlex.quote(target)}", data=pdf.read_bytes())
    return {"path": posixpath.relpath(target, project["root"])}


def synctex_for(project):
    path = built_file(project, ".synctex.gz")
    if not path.exists():
        raise HTTPException(404, "no synctex data; compile first")
    base = mirror_dir(project["id"]) / posixpath.dirname(project["main"])
    return SyncTex(path, str(base))


@app.get("/api/synctex/forward")
def synctex_forward(pid: str, path: str, line: int):
    project = get_project(pid)
    return synctex_for(project).forward(str(mirror_dir(pid) / safe_rel(path)), line)


@app.get("/api/synctex/inverse")
def synctex_inverse(pid: str, page: int, x: float, y: float):
    project = get_project(pid)
    hit = synctex_for(project).inverse(page, x, y)
    mirror = os.path.realpath(mirror_dir(pid))
    if hit is None or not hit["file"].startswith(mirror + "/"):
        return None
    return {"file": hit["file"][len(mirror) + 1:], "line": hit["line"]}


# ---------------------------------------------------------------- main

def main():
    global PORT, SSH_CONFIG
    parser = argparse.ArgumentParser(prog="sshleaf")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--ssh-config", type=Path, help="SSH config file to use instead of ~/.ssh/config")
    args = parser.parse_args()
    PORT = args.port
    if args.ssh_config:
        SSH_CONFIG = args.ssh_config.expanduser().resolve()
        SSH_OPTS[:0] = ["-F", str(SSH_CONFIG)]
    assert shutil.which("tectonic"), "tectonic not found (brew install tectonic)"
    url = f"http://127.0.0.1:{PORT}/"
    print(f"sshleaf on {url}")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
