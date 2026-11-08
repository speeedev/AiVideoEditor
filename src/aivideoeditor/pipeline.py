"""Analysis and sync steps that prepare a project for editing."""

from __future__ import annotations

from collections.abc import Callable

from .analysis.audio import decode_audio
from .analysis.speech import analyze_speech
from .analysis.sync import SYNC_SR, find_offset_arrays
from .analysis.transcribe import clean_transcript, transcribe
from .analysis.visual import analyze_visual
from .media.proxy import ensure_proxy
from .project import Project, SyncInfo
from .timeline.assemble import primary_audio_asset

Log = Callable[[str], None]
SPEECH_ROLES = ("mic", "camera", "screen")


def analyze_asset(
    project: Project,
    asset_id: str,
    *,
    do_transcribe: bool = True,
    whisper_model: str = "small",
    language: str | None = None,
    force: bool = False,
    log: Log = print,
) -> None:
    asset = project.asset(asset_id)
    if asset.has_audio and asset.role in SPEECH_ROLES and (force or project.speech(asset_id) is None):
        log(f"[{asset_id}] detecting speech and pauses…")
        samples = decode_audio(asset.path, 16000, stream=asset.audio_stream)
        project.save_speech(asset_id, analyze_speech(samples, 16000))
    if (asset.has_video and not asset.is_image and asset.role in ("screen", "camera", "broll")
            and (force or project.visual(asset_id) is None)):
        log(f"[{asset_id}] analysing visual activity and scene cuts…")
        project.save_visual(asset_id, analyze_visual(asset.path))
    if asset.has_video and asset.role in ("screen", "camera", "broll", "other"):
        ensure_proxy(project, asset, log=log)
    if (do_transcribe and asset.has_audio and asset.role in SPEECH_ROLES
            and (force or project.transcript(asset_id) is None)):
        log(f"[{asset_id}] transcribing with faster-whisper ({whisper_model})…")
        audio = decode_audio(asset.path, 16000, stream=asset.audio_stream)
        last = [0.0]

        def progress(done: float, total: float) -> None:
            if done - last[0] >= 30 or (total and done >= total - 1):
                last[0] = done
                log(f"[{asset_id}] transcribed {done:.0f}/{total:.0f}s")
        raw = transcribe(audio, model=whisper_model, language=language, progress=progress)
        speech = project.speech(asset_id)
        clean, dropped = clean_transcript(raw, speech.silences if speech else None)
        if dropped:
            log(f"[{asset_id}] removed {dropped} likely hallucinated word(s) from the transcript")
        project.save_transcript(asset_id, clean)


def analyze_project(project: Project, *, do_transcribe: bool = True, whisper_model: str = "small",
                    language: str | None = None, force: bool = False, log: Log = print,
                    transcribe_only_primary: bool = True) -> None:
    primary = primary_audio_asset(project)
    for aid, asset in project.assets.items():
        want = do_transcribe and (not transcribe_only_primary or aid == primary)
        analyze_asset(project, aid, do_transcribe=want, whisper_model=whisper_model,
                      language=language, force=force, log=log)


def sync_project(project: Project, *, reference: str | None = None, max_offset: float | None = None,
                 log: Log = print) -> dict[str, SyncInfo]:
    """Find every recording's position on the reference recording's clock."""
    ref_id = reference or primary_audio_asset(project)
    if ref_id is None:
        raise ValueError("no asset with audio to use as sync reference")
    ref = project.asset(ref_id)
    if not ref.has_audio:
        raise ValueError(f"reference '{ref_id}' has no audio")
    project.data.sync_reference = ref_id
    ref.sync = SyncInfo(reference=ref_id, offset=0.0, confidence=100.0, method="reference")
    log(f"sync reference: {ref_id}")
    ref_audio = decode_audio(ref.path, SYNC_SR, stream=ref.audio_stream)
    results: dict[str, SyncInfo] = {ref_id: ref.sync}
    for aid, asset in project.assets.items():
        if aid == ref_id or asset.role not in ("screen", "camera", "mic"):
            continue
        if asset.sync is not None and asset.sync.method == "manual":
            log(f"[{aid}] keeping manual offset {asset.sync.offset:+.3f}s")
            results[aid] = asset.sync
            continue
        if not asset.has_audio:
            log(f"[{aid}] has no audio; assuming it started with the reference (set it with `ave sync --manual`)")
            asset.sync = SyncInfo(reference=ref_id, offset=0.0, confidence=0.0, method="assumed")
            results[aid] = asset.sync
            continue
        other = decode_audio(asset.path, SYNC_SR, stream=asset.audio_stream)
        res = find_offset_arrays(ref_audio, other, SYNC_SR, max_offset=max_offset)
        method = "audio" if res.reliable else "audio-uncertain"
        asset.sync = SyncInfo(reference=ref_id, offset=res.offset, drift_ppm=res.drift_ppm,
                              confidence=res.confidence, method=method)
        results[aid] = asset.sync
        warn = "" if res.reliable else "  ⚠ low confidence, check visually"
        drift = f", drift {res.drift_ppm:+.0f} ppm" if res.drift_ppm else ""
        log(f"[{aid}] offset {res.offset:+.4f}s (confidence {res.confidence:.1f}{drift}){warn}")
    project.save()
    return results


def set_manual_offset(project: Project, asset_id: str, offset: float) -> None:
    ref = project.data.sync_reference or primary_audio_asset(project) or asset_id
    project.asset(asset_id).sync = SyncInfo(reference=ref, offset=offset, confidence=0.0, method="manual")
    project.save()
