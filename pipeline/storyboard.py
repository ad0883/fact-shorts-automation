"""Validated story beats, word-aligned timing, and visual coverage review.

Diagrams use only supplied labels. Neither alignment nor rendering verifies claims.
"""

import math
import re


ANIMATION_VIEWS = {
    "golf_airflow": {"question", "overview", "smooth", "drag", "dimples", "attached", "wake", "comparison"},
    "memory_list": {"question", "overview", "primacy", "recency", "middle", "comparison"},
}


VISUAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["photo", "question", "comparison", "sequence", "animation"]},
        "headline": {"type": "string"},
        "query": {"type": "string"},
        "labels": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "template": {"type": "string", "enum": list(ANIMATION_VIEWS)},
        "view": {"type": "string", "enum": sorted(set.union(*ANIMATION_VIEWS.values()))},
    }, "required": ["kind", "headline", "query", "labels"],
}
STORYBOARD_SCHEMA = {
    "type": "array", "minItems": 4, "maxItems": 14,
    "items": {"type": "object", "additionalProperties": False,
        "properties": {
            "role": {"type": "string", "enum": ["hook", "setup", "explain", "reveal"]},
            "narration": {"type": "string"}, "visual": VISUAL_SCHEMA,
        }, "required": ["role", "narration", "visual"]},
}


def validate_storyboard(value, narration):
    if not isinstance(value, list) or not 4 <= len(value) <= 14:
        raise ValueError("storyboard must contain 4-14 beats")
    result = []
    for beat in value:
        if not isinstance(beat, dict) or beat.get("role") not in {"hook", "setup", "explain", "reveal"}:
            raise ValueError("Each storyboard beat needs a valid role")
        spoken = beat.get("narration")
        visual = beat.get("visual")
        if not isinstance(spoken, str) or not spoken.strip() or not isinstance(visual, dict):
            raise ValueError("Each storyboard beat needs narration and a visual")
        kind = visual.get("kind")
        headline, query, labels = visual.get("headline", ""), visual.get("query", ""), visual.get("labels", [])
        if kind not in {"photo", "question", "comparison", "sequence", "animation"}:
            raise ValueError("Unknown storyboard visual kind")
        if (not isinstance(headline, str) or not headline.strip() or len(headline) > 80
                or not isinstance(query, str) or len(query) > 100
                or not isinstance(labels, list) or len(labels) > 3
                or any(not isinstance(label, str) or not label.strip() or len(label) > 64 for label in labels)):
            raise ValueError("Visuals need a short headline, query and at most three short labels")
        if kind == "photo" and not query.strip():
            raise ValueError("A photo beat needs a concrete subject query")
        if kind == "comparison" and len(labels) != 2:
            raise ValueError("A comparison needs exactly two labels")
        if kind == "sequence" and not 2 <= len(labels) <= 3:
            raise ValueError("A sequence needs two or three ordered labels")
        motion = {}
        if kind == "animation":
            template = visual.get("template")
            if visual.get("view") not in ANIMATION_VIEWS.get(template, set()):
                raise ValueError("Animation needs a supported template and view")
            if template == "golf_airflow" and ("golf" not in narration.casefold() or "dimple" not in narration.casefold()):
                raise ValueError("The golf_airflow template requires narration about golf-ball dimples")
            if template == "memory_list" and not is_memory_list(narration):
                raise ValueError("The memory_list template requires narration about list recall and serial position")
            motion = {"template": visual["template"], "view": visual["view"]}
        result.append({"role": beat["role"], "narration": " ".join(spoken.split()),
                       "visual": {"kind": kind, "headline": " ".join(headline.split()),
                                  "query": " ".join(query.split()), "labels": [" ".join(x.split()) for x in labels], **motion}})
    if result[0]["role"] != "hook" or result[-1]["role"] != "reveal":
        raise ValueError("Storyboard must open with a hook and finish with a reveal")
    if " ".join(b["narration"] for b in result) != " ".join(narration.split()):
        raise ValueError("Storyboard narration must reproduce fact_text exactly, in order, without omissions")
    return result


def is_memory_list(text):
    text = text.casefold()
    return ("list" in text and ("recall" in text or "remember" in text or "memory" in text)
            and ("serial position" in text or "serial-position" in text or "primacy" in text and "recency" in text))


def subject_storyboard(beats, narration):
    """Use a concrete demonstration for recognized topics, including old scripts.

    This preserves every narrated word. It is not a claim generator, and explicit
    animation instructions are preserved. Other topics keep their planned visuals.
    """
    if not is_memory_list(narration):
        return beats
    result = []
    for beat in beats:
        if beat["visual"]["kind"] == "animation":
            result.append(beat)
            continue
        clauses = re.split(r"(?<=[.!?,])\s+", beat["narration"])
        for clause in clauses:
            tokens = clause.split()
            count = max(1, math.ceil(len(tokens)/12))
            for part in range(count):
                spoken = " ".join(tokens[part*len(tokens)//count:(part+1)*len(tokens)//count])
                # Determine the concept from the complete clause, not a truncated
                # phrase that might omit 'first', 'last', or 'middle'.
                lower = clause.casefold()
                view = "question" if beat["role"] == "hook" else (
                    "overview" if ("first" in lower and "last" in lower or "primacy" in lower and "recency" in lower) else
                    "primacy" if "primacy" in lower or "first" in lower or "early" in lower or "earlier" in lower else
                    "recency" if "recency" in lower or "last" in lower else
                    "middle" if "middle" in lower or "fading" in lower else "overview")
                headline = {"question":"Which items stick?", "overview":"Watch the list's edges",
                            "primacy":"The first items", "recency":"The last items",
                            "middle":"The middle fades"}[view]
                result.append({**beat, "narration": spoken, "visual_basis": "subject_demonstration",
                               "visual": {"kind":"animation", "template":"memory_list", "view":view,
                                          "headline":headline, "query":"", "labels":[]}})
    return result


def time_storyboard(fact, words, duration):
    from pipeline.fetch_images import split_into_sentences, map_sentences_to_timestamps
    beats = fact.get("storyboard")
    if not beats:
        sentences = split_into_sentences(fact["fact_text"])
        beats = [{"role": "hook" if i == 0 else "reveal" if i == len(sentences)-1 else "explain",
                  "narration": sentence,
                  "visual": {"kind": "photo", "headline": "", "query": "", "labels": []}}
                 for i, sentence in enumerate(sentences)]
    beats = subject_storyboard(beats, fact["fact_text"])
    aligned = map_sentences_to_timestamps([b["narration"] for b in beats], words)
    if len(aligned) != len(beats):
        # Severe ASR omissions can collapse boundaries. Keep every beat, flag the
        # estimate, and interpolate by spoken word count instead of dropping text.
        total = sum(len(b["narration"].split()) for b in beats)
        offset, aligned = 0, []
        for beat in beats:
            size = len(beat["narration"].split())
            aligned.append({"start": duration*offset/total, "end": duration*(offset+size)/total,
                            "alignment": "estimated"})
            offset += size
    result = []
    for i, (beat, timing) in enumerate(zip(beats, aligned)):
        start = 0.0 if i == 0 else min(float(timing["start"]), duration)
        end = min(aligned[i+1]["start"], duration) if i+1 < len(beats) else duration
        if end <= start:
            continue
        result.append({**beat, "start": start, "end": end, "beat_index": i,
                       "alignment": timing.get("alignment", "estimated")})
    return result


def coverage_report(segments, duration, *, long_scene_seconds=4):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Visual review needs a positive finite duration")
    cursor, gaps, long_scenes = 0.0, [], []
    for scene in sorted(segments, key=lambda s: s["start"]):
        start, end = max(0., float(scene["start"])), min(duration, float(scene["end"]))
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError("Invalid scene timing in visual review")
        if start > cursor + .05:
            gaps.append({"start": round(cursor, 3), "end": round(start, 3)})
        cursor = max(cursor, end)
        if end-start > long_scene_seconds:
            long_scenes.append({"start": round(start, 3), "end": round(end, 3)})
    if duration > cursor + .05:
        gaps.append({"start": round(cursor, 3), "end": round(duration, 3)})
    missing = sum(g["end"]-g["start"] for g in gaps)
    return {"coverage_percent": round(100*(1-missing/duration), 1), "coverage_basis": "planned_timeline", "gaps": gaps,
            "long_scenes": long_scenes, "long_scene_threshold_seconds": long_scene_seconds}


def compose_visuals(beats, photos, images_dir, duration):
    """Use diagrams where planned; leave photo gaps on the gameplay background."""
    from pipeline.story_graphics import render_graphic
    scenes, fallbacks = [], []
    before = coverage_report(photos, duration)
    for beat in beats:
        visual, start, end = beat["visual"], beat["start"], beat["end"]
        if visual["kind"] != "photo":
            scenes.extend(render_graphic(beat, images_dir))
            continue
        cursor = start
        for photo in sorted(photos, key=lambda p: p["start"]):
            lo, hi = max(cursor, photo["start"]), min(end, photo["end"])
            if hi <= lo:
                continue
            if lo > cursor:
                scenes.extend(_fallback(beat, cursor, lo, images_dir, fallbacks))
            scenes.append({**photo, "start": lo, "end": hi, "role": beat["role"],
                           "beat_index": beat["beat_index"],
                           "layout": photo.get("layout", "full")})
            cursor = hi
        if cursor < end:
            scenes.extend(_fallback(beat, cursor, end, images_dir, fallbacks))
    scenes = pace_shots(scenes)
    report = coverage_report(scenes, duration)
    report.update(moving_media_percent=round(100*sum(s["end"]-s["start"] for s in scenes if s.get("media_type")=="video")/duration,1),
                  text_only_percent=round(100*sum(s["end"]-s["start"] for s in scenes if s.get("text_only"))/duration,1),
                  fallback_percent=round(100*sum(s["end"]-s["start"] for s in scenes if s.get("fallback"))/duration,1))
    report["gameplay_fallback_percent"] = round(100*sum(g["end"]-g["start"] for g in fallbacks)/duration, 1)
    report.update(photo_coverage_before_fallback=before, fallback_scenes=fallbacks,
                  estimated_beats=[b["beat_index"] for b in beats if b["alignment"] == "estimated"])
    warnings = []
    if fallbacks:
        warnings.append(f"{len(fallbacks)} missing-image passage(s) use gameplay with captions ({report['gameplay_fallback_percent']}% of the video). Replace with a subject visual where possible.")
    if report["text_only_percent"] > 35:
        warnings.append(f"{report['text_only_percent']}% of the video is text-led. Motion alone does not replace subject imagery.")
    if report["long_scenes"]:
        warnings.append(f"{len(report['long_scenes'])} scene(s) exceed 4 seconds. Check pacing and whether a demonstration would help.")
    if report["estimated_beats"]:
        warnings.append("Some storyboard timings are estimated; check that reveals match the spoken words.")
    report["warnings"] = warnings
    return scenes, report


def pace_shots(scenes, maximum=3.5):
    """Reframe still photos and advance footage rather than repeating a long hold.

    Subject animations already contain their own visual progression. Preserve
    diagrams in contain mode so labels cannot be cropped away.
    """
    result=[]
    for scene in scenes:
        if scene.get("kind") in {"animation", "question", "comparison", "sequence"}:
            result.append(scene)
            continue
        start,end=scene["start"],scene["end"]
        count=max(1,math.ceil((end-start)/maximum))
        for part in range(count):
            lo=start+(end-start)*part/count
            hi=start+(end-start)*(part+1)/count
            offset = scene.get("source_offset",0)+lo-start
            clip_duration = scene.get("provenance", {}).get("duration_seconds", 0)
            if scene.get("media_type") == "video" and clip_duration:
                offset %= clip_duration
            result.append({**scene,"start":lo,"end":hi,
                           "source_offset":offset,
                           "camera":("push","pull","pan_left","pan_right")[(len(result))%4]})
    return result


def _fallback(beat, start, end, directory, fallbacks):
    # Leave the gameplay background visible; captions already carry narration.
    fallbacks.append({"start": start, "end": end, "beat_index": beat["beat_index"]})
    return []
