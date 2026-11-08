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
from .scenes import SPEECH_RANK, SYNC_ROLES, natural_key, recorded_at, recorded_window, speech_sources
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
    """Analyse every recording, group them by moment (sync), then transcribe the speech.

    ``transcribe_only_primary`` transcribes one speech recording per scene (mic >
    camera > screen); set it to False to transcribe every recording with speech.
    """
    for aid in project.assets:
        analyze_asset(project, aid, do_transcribe=False, force=force, log=log)
    if any(a.role in SYNC_ROLES and a.sync is None for a in project.assets.values()):
        sync_project(project, log=log)
    if not do_transcribe:
        return
    if transcribe_only_primary:
        targets = speech_sources(project)
    else:
        targets = [a.id for a in project.assets.values() if a.has_audio and a.role in SPEECH_ROLES]
    for aid in targets:
        analyze_asset(project, aid, do_transcribe=True, whisper_model=whisper_model, language=language,
                      force=force, log=log)


def sync_project(project: Project, *, reference: str | None = None, max_offset: float | None = None,
                 log: Log = print) -> dict[str, SyncInfo]:
    """Group recordings of the same moment and align them; separate scenes/takes get their own group.

    Every group has a reference (its best audio: mic > camera > screen). A recording with audio joins the
    group whose reference it reliably aligns with, otherwise it starts a new group (a separate scene or
    take). Recordings without audio join the group they were recorded with (by recording time), or the
    only group; otherwise they are a scene of their own.
    """
    cands = [a for a in project.assets.values() if a.role in SYNC_ROLES and (a.has_video or a.has_audio)]
    manual = [a for a in cands if a.sync is not None and a.sync.method == "manual"]
    auto = [a for a in cands if a not in manual]
    audio = sorted((a for a in auto if a.has_audio),
                   key=lambda a: (a.id != reference, SPEECH_RANK.get(a.role, 9), recorded_at(a), natural_key(a.path)))
    silent = sorted((a for a in auto if not a.has_audio), key=lambda a: (recorded_at(a), natural_key(a.path)))

    groups: list[dict] = []          # {"ref": Asset, "audio": samples | None, "members": [Asset]}
    results: dict[str, SyncInfo] = {}

    def new_group(a, samples, method: str) -> None:
        a.sync = SyncInfo(reference=a.id, offset=0.0, confidence=100.0 if method == "reference" else 0.0,
                          method=method, group=len(groups))
        groups.append({"ref": a, "audio": samples, "members": [a]})
        results[a.id] = a.sync

    for a in audio:
        samples = decode_audio(a.path, SYNC_SR, stream=a.audio_stream)
        best = None
        for gi, g in enumerate(groups):
            if g["audio"] is None:
                continue
            res = find_offset_arrays(g["audio"], samples, SYNC_SR, max_offset=max_offset)
            if res.reliable and (best is None or res.confidence > best[1].confidence):
                best = (gi, res)
        if best is None:
            first = not groups
            new_group(a, samples, "reference")
            log(f"sync reference: {a.id}" if first else
                f"[{a.id}] no audio in common with earlier recordings: separate scene/take")
            continue
        gi, res = best
        a.sync = SyncInfo(reference=groups[gi]["ref"].id, offset=res.offset, drift_ppm=res.drift_ppm,
                          confidence=res.confidence, method="audio", group=gi)
        groups[gi]["members"].append(a)
        results[a.id] = a.sync
        drift = f", drift {res.drift_ppm:+.0f} ppm" if res.drift_ppm else ""
        log(f"[{a.id}] offset {res.offset:+.4f}s against {groups[gi]['ref'].id} "
            f"(confidence {res.confidence:.1f}{drift})")

    for a in silent:
        gi, offset = _match_by_time(a, groups)
        if gi is None and len(groups) == 1 and not _clearly_apart(a, groups[0]["members"]):
            gi, offset = 0, 0.0
        if gi is None:
            new_group(a, None, "assumed")
            log(f"[{a.id}] has no audio" + (": separate scene" if len(groups) > 1 else
                                            "; nothing to sync it with"))
            continue
        a.sync = SyncInfo(reference=groups[gi]["ref"].id, offset=offset, confidence=0.0,
                          method="time" if offset else "assumed", group=gi)
        groups[gi]["members"].append(a)
        results[a.id] = a.sync
        log(f"[{a.id}] has no audio; placed with {groups[gi]['ref'].id} at {offset:+.1f}s "
            f"({'by recording time' if offset else 'assumed to start together'}; adjust with `ave sync --manual`)")

    for a in manual:
        ref_group = next((i for i, g in enumerate(groups) if any(m.id == a.sync.reference for m in g["members"])),
                         None)
        if ref_group is None:
            ref_group = len(groups)
            groups.append({"ref": a, "audio": None, "members": [a]})
        a.sync.group = ref_group
        results[a.id] = a.sync
        log(f"[{a.id}] keeping manual offset {a.sync.offset:+.3f}s")

    if groups:
        project.data.sync_reference = groups[0]["ref"].id
    if len(groups) > 1:
        log(f"{len(groups)} separate scenes/takes: they are laid out one after another in recording order")
    project.save()
    return results


def _match_by_time(asset, groups: list[dict]) -> tuple[int | None, float]:
    """Group whose recording time overlaps this asset's (only when both carry recording-time tags)."""
    a0, a1, src = recorded_window(asset)
    if src != "tag":
        return None, 0.0
    best, best_overlap = None, 0.0
    for gi, g in enumerate(groups):
        r0, r1, rsrc = recorded_window(g["ref"])
        if rsrc != "tag":
            continue
        overlap = min(a1, r1) - max(a0, r0)
        if overlap > 0.5 * min(a1 - a0, r1 - r0) and overlap > best_overlap:
            best, best_overlap = gi, overlap
    if best is None:
        return None, 0.0
    return best, round(a0 - recorded_window(groups[best]["ref"])[0], 1)


def _clearly_apart(asset, members) -> bool:
    """True when recording-time tags show the asset was recorded at a different time."""
    a0, a1, src = recorded_window(asset)
    if src != "tag":
        return False
    for m in members:
        m0, m1, msrc = recorded_window(m)
        if msrc == "tag" and min(a1, m1) - max(a0, m0) > -60:
            return False
    return any(recorded_window(m)[2] == "tag" for m in members)


def set_manual_offset(project: Project, asset_id: str, offset: float) -> None:
    ref = project.data.sync_reference or primary_audio_asset(project) or asset_id
    project.asset(asset_id).sync = SyncInfo(reference=ref, offset=offset, confidence=0.0, method="manual")
    project.save()
