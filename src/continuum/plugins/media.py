"""Seeing and hearing for a worker: pictures, PDFs, video and sound, understood by Claude.

DeepSeek reads only text. A worker's `perceive` call gathers what Claude needs to look at: a picture or a PDF as it
is; a video as frames across its length plus what is said in it; a recording as what is said in it. Speech is turned
into text on this host (faster-whisper, run by the host's own Python), and frames are cut with ffmpeg. Claude Code,
signed in with the person's account on this host, then looks at the frames and pictures and answers the worker's
question. Without Claude, what was said still comes back.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import shutil
from pathlib import Path
from typing import Any

from . import cli_agents
from .sandbox import host_env

# Long recordings take the faster model: on two cores, "small" listens at about real time.
SMALL_UP_TO = 600
MAX_FRAMES = 16
TRANSCRIBE = r'''
import json, sys
from faster_whisper import WhisperModel
path, size = sys.argv[1], sys.argv[2]
model = WhisperModel(size, device="cpu", compute_type="int8")
_, info = model.transcribe(path, beam_size=1)
# Chinese comes out in simplified characters and with punctuation when it is told so.
prompt = "以下是普通话的句子。" if info.language == "zh" else None
segments, info = model.transcribe(path, beam_size=1, vad_filter=True, initial_prompt=prompt)
out = [{"start": round(s.start, 1), "text": s.text.strip()} for s in segments]
print(json.dumps({"language": info.language, "segments": out}, ensure_ascii=False))
'''


def kind_of(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime.startswith(("image/", "video/", "audio/")):
        return mime.split("/")[0]
    if mime == "application/pdf":
        return "pdf"
    return "other"


def clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60:02d}:{seconds % 60:02d}"


async def _run(argv: list[str], timeout: float) -> tuple[int | None, str, str]:
    process = await asyncio.create_subprocess_exec(*argv, env=host_env(), stdin=asyncio.subprocess.DEVNULL,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                   start_new_session=True)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None, "", "超时"
    return process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _tool(name: str) -> str | None:
    return shutil.which(name, path=host_env()["PATH"])


async def duration(path: Path) -> float | None:
    probe = _tool("ffprobe")
    if probe is None:
        return None
    code, out, _ = await _run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], 60)
    try:
        return float(out.strip()) if code == 0 else None
    except ValueError:
        return None


async def has_sound(path: Path) -> bool:
    probe = _tool("ffprobe")
    if probe is None:
        return True
    code, out, _ = await _run([probe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of",
                               "csv=p=0", str(path)], 60)
    return code == 0 and bool(out.strip())


async def transcribe(path: Path, seconds: float | None) -> str:
    """What is said in a recording or a video, with times; a sentence saying why when it cannot be had."""
    python = _tool("python3")
    if python is None:
        return "（没能转写：这台机器上没有 python3）"
    size = "small" if (seconds or 0) <= SMALL_UP_TO else "base"
    code, out, err = await _run([python, "-c", TRANSCRIBE, str(path), size], max(300, (seconds or 60) * 3))
    if code != 0:
        why = "超时" if code is None else (err.strip().splitlines() or ["出错了"])[-1][:200]
        if "No module named 'faster_whisper'" in err:
            why = "这台机器上没有装 faster-whisper（pip install --user faster-whisper）"
        return f"（没能转写：{why}）"
    try:
        found = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return "（没能转写：输出看不懂）"
    lines = [f"[{clock(s['start'])}] {s['text']}" for s in found.get("segments") or [] if s.get("text")]
    return "\n".join(lines) or "（没有听到说话）"


async def frames(path: Path, folder: Path, seconds: float | None) -> list[tuple[float, Path]]:
    """Pictures across a video's length: one every ten seconds or so, between 4 and 16 of them."""
    ffmpeg = _tool("ffmpeg")
    if ffmpeg is None:
        return []
    folder.mkdir(parents=True, exist_ok=True)
    length = seconds or 0
    count = max(4, min(MAX_FRAMES, int(length / 10) + 1)) if length > 0 else 1
    times = [length * (i + 0.5) / count for i in range(count)] if length > 0 else [0.0]
    out: list[tuple[float, Path]] = []
    for i, at in enumerate(times, 1):
        target = folder / f"frame-{i:02d}.jpg"
        code, _, _ = await _run([ffmpeg, "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(path), "-frames:v", "1",
                                 "-vf", "scale='min(1024,iw)':-2", "-q:v", "4", str(target)], 60)
        if code == 0 and target.exists():
            out.append((at, target))
    return out


async def gather(paths: list[Path], folder: Path, step: Any = None) -> tuple[list[str], list[Path]]:
    """What there is to look at and listen to: a description of each file for Claude, and the pictures it should
    open (copied into the workspace, where Claude may read them)."""
    notes: list[str] = []
    pictures: list[Path] = []
    folder.mkdir(parents=True, exist_ok=True)
    for n, path in enumerate(paths, 1):
        if not path.is_file():
            notes.append(f"{n}. {path.name}：找不到这个文件（{path}）")
            continue
        kind = kind_of(path)
        if step:
            step(f"处理 {path.name}…")
        if kind in ("image", "pdf"):
            copy = folder / f"{n:02d}-{path.name}"
            shutil.copyfile(path, copy)
            pictures.append(copy)
            notes.append(f"{n}. {'图片' if kind == 'image' else 'PDF'} {path.name}：用 Read 打开 {copy.name}")
        elif kind == "video":
            seconds = await duration(path)
            shots = await frames(path, folder / f"{n:02d}-frames", seconds)
            pictures += [p for _, p in shots]
            shown = "；".join(f"{clock(at)} {p.parent.name}/{p.name}" for at, p in shots) or "（没能截取画面）"
            said = await transcribe(path, seconds) if await has_sound(path) else "（这个视频没有声音）"
            notes.append(f"{n}. 视频 {path.name}（时长 {clock(seconds or 0)}）\n   画面（按时间，用 Read 依次打开）：{shown}"
                         f"\n   语音转写：\n{_indent(said)}")
        elif kind == "audio":
            seconds = await duration(path)
            said = await transcribe(path, seconds)
            notes.append(f"{n}. 音频 {path.name}（时长 {clock(seconds or 0)}）\n   语音转写：\n{_indent(said)}")
        else:
            copy = folder / f"{n:02d}-{path.name}"
            shutil.copyfile(path, copy)
            pictures.append(copy)
            notes.append(f"{n}. 文件 {path.name}：用 Read 打开 {copy.name}")
    return notes, pictures


def _indent(text: str) -> str:
    return "\n".join(f"   {line}" for line in text.splitlines())


LOOK = """请根据下面的材料回答问题。材料都在当前目录下，图片、PDF 和视频画面用 Read 工具逐个打开看（Read 能直接看图）；
语音已经转写成文字附在下面（转写可能有错别字，按上下文理解）。
只回答问题，要具体：说出你看到、听到的内容（数字、文字、人物动作、时间点）。看不清或听不清的直接说，不要编。

问题：{question}

材料：
{notes}"""


async def perceive(job: Any, claude: Any, paths: list[Path], question: str, folder: Path, step: Any = None) -> str:
    """Look at and listen to files, and answer a question about them: Claude when it can, else what was said."""
    notes, pictures = await gather(paths, folder, step)
    if claude is None:
        heard = "\n".join(n for n in notes if "语音转写" in n)
        reason = "Claude Code 没有启用或没有配置"
        return (f"（看不了画面：{reason}。）\n{heard}" if heard else f"错误：看不了：{reason}；"
                "图片、PDF 和视频画面要 Claude 来看")
    if step:
        step("Claude 正在看…")
    task = LOOK.format(question=question or "这些内容讲了什么？", notes="\n".join(notes))
    answer = await cli_agents.delegate("claude_code", claude, _Here(job, folder), task, fresh=True)
    if answer.startswith("错误："):
        heard = "\n".join(n for n in notes if "语音转写" in n)
        return answer + (f"\n已经转写出来的：\n{heard}" if heard else "")
    return answer


class _Here:
    """The job, working in the folder that holds what is to be seen: Claude reads it there."""

    def __init__(self, job: Any, folder: Path):
        self._job = job
        self.workdir = folder

    def __getattr__(self, name: str) -> Any:
        return getattr(self._job, name)

    def state_file(self, suffix: str) -> Path:
        return self.workdir.with_name(self.workdir.name + suffix)
