# Capability Objects — ffmpeg, OCR, and Other System Tools

A DBBASIC object is plain Python running in a subprocess. There is no
import sandbox and no call whitelist, so an object can do anything
Python on that machine can do — including shell out to system binaries.
That makes real capability work a normal object: OCR a scanned receipt
with `tesseract`, transcode a clip with `ffmpeg`, render a thumbnail
with ImageMagick, extract text from a PDF, call a local model.

The old prototype shipped a pack of these. The runtime carries the same
capability; this page documents how to write them safely.

## The execution model

Each object call runs in a **spawned subprocess** with a wall-clock
timeout (`DBBASIC_OBJECT_TIMEOUT_SECONDS`). Practical consequences:

- The object has the full standard library, the filesystem, and the
  ability to launch its own subprocesses. `subprocess.run([...])` works.
- A runaway object is killed at the timeout, so a hung `ffmpeg` cannot
  wedge the server. Set the timeout to fit the workload.
- It is isolated from the parent's memory, not from the machine. There
  is no CPU or memory cap yet (see [status](status.md)), so capability
  objects are **operator/admin code** — your own tools on your own box,
  the same trust level as a cron job. Writing object source is gated;
  visitors run these objects, they do not author them.

## Prerequisite: install the tool

Capability objects call binaries that must exist on the VM. This is a
normal operator step, explicit rather than hidden:

```bash
sudo apt install tesseract-ocr ffmpeg imagemagick poppler-utils
```

If a binary is missing, the object's `subprocess.run` raises
`FileNotFoundError`, which surfaces as a normal object execution error
in the logs and change feed — no silent failure.

## Worked example: OCR an uploaded image

A browser form posting `multipart/form-data` delivers files under
`request["_files"]` with base64 content (see
[object authoring](object-authoring.md#upload-forms)). The object
writes the bytes to a temp file, runs `tesseract`, and returns the text.

```python
import base64
import subprocess
import tempfile
from pathlib import Path


def POST(request):
    upload = request.get("_files", {}).get("image")
    if upload is None:
        return {"status": "error", "error": "post an image field"}

    content = base64.b64decode(upload["content_base64"])
    with tempfile.TemporaryDirectory() as work:
        source = Path(work) / "in.png"
        source.write_bytes(content)
        result = subprocess.run(
            ["tesseract", str(source), "stdout"],
            capture_output=True, text=True, timeout=30,
        )

    if result.returncode != 0:
        _logger.error("ocr failed", stderr=result.stderr[:500])
        return {"status": "error", "error": "OCR failed"}

    text = result.stdout.strip()
    _logger.info("ocr done", chars=len(text))
    return {"status": "ok", "text": text}
```

Set that object's timeout generously (`DBBASIC_OBJECT_TIMEOUT_SECONDS`)
so a large scan has room, and grant it a public `execute` rule if the
form is public.

## Worked example: transcode with ffmpeg

```python
import base64
import subprocess
import tempfile
from pathlib import Path


def POST(request):
    upload = request.get("_files", {}).get("clip")
    content = base64.b64decode(upload["content_base64"])
    with tempfile.TemporaryDirectory() as work:
        src = Path(work) / "in"
        out = Path(work) / "out.mp3"
        src.write_bytes(content)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vn", "-b:a", "128k", str(out)],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0 or not out.exists():
            return {"status": "error", "error": "transcode failed"}
        audio = out.read_bytes()

    return {
        "content_type": "audio/mpeg",
        "body": base64.b64encode(audio).decode("ascii"),
        "encoding": "base64",
    }
```

Small media (receipts, short clips, single images) fits inside a
request. **Long media does not** — a two-second timeout is fine for a
receipt but a 2 GB video transcode belongs in a background job, not a
web request. Background jobs are the next platform slice
([status](status.md)); until then, keep capability objects to work that
finishes in seconds.

## Composing with files, records, and AI

Capability objects get more useful combined with the rest of the
platform:

- **Files** — instead of accepting an upload each time, an object can
  read a stored file's bytes and OCR or transcode it, then write the
  result back as a new file or a record. (The clean helper for an
  object to read from the `files` collection by id is a small planned
  addition; today an object works from the request payload.)
- **Records** — an OCR object can `create` a record with the extracted
  text so it becomes searchable through [global search](http-api-contract.md#global-search).
- **AI + shell** — because the object is exposed as an
  `execute_object` MCP tool, the shell AI can call it. "OCR my latest
  upload and note the total" becomes: the AI reads the file, calls the
  OCR object, and creates a note — each step permission-checked and
  audited, no glue code.

## Safety checklist for a capability object

- Set a timeout that fits the job; never rely on the default.
- Validate the upload before spending CPU on it (size, content type).
- Write to a `tempfile.TemporaryDirectory()` so nothing leaks between
  calls; never build a shell command by string-concatenating user input
  — pass argument lists to `subprocess.run`, never `shell=True`.
- Log failures with `_logger` so they land in the change feed.
- Treat these as operator-authored: source writes are gated, and a
  per-family "builder role" for scoped, non-admin object creation is a
  deliberate open design item, not a shipped capability.
