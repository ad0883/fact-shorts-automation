"""A portable, local review page beside every rendered short."""

from html import escape
from pathlib import Path
import json
from urllib.parse import urlparse


def write_review(directory: Path, fact: dict, *, duration: float, image_segments: list[dict]) -> Path:
    sources = []
    for source in fact.get("sources", []):
        if not isinstance(source, dict):
            continue
        url = source.get("url", "")
        if urlparse(url).scheme in {"http", "https"}:
            claim = source.get("claim") or "No supporting claim supplied."
            sources.append(f'<li><a href="{escape(url, quote=True)}" rel="noreferrer">{escape(source.get("title") or url)}</a>'
                           f'<br><small>Claim to check: {escape(claim)}</small></li>')
    credits = []
    for segment in image_segments:
        provenance = segment.get("provenance", {})
        name = provenance.get("photographer") or segment.get("photographer") or segment.get("attribution")
        if name:
            url = provenance.get("photo_url", "")
            credit = escape(str(name))
            if urlparse(url).scheme in {"http", "https"}:
                credit = f'<a href="{escape(url, quote=True)}" rel="noreferrer">{credit}</a>'
            provider = provenance.get("provider", "pexels")
            kind = {"artist_concept": "artist concept", "agency_image": "archive image", "archive_image": "archive photo", "reconstruction": "reconstruction/model"}.get(provenance.get("usage"), "illustrative stock photo")
            license_text = escape(provenance.get("license", ""))
            license_url = provenance.get("license_url", "")
            if license_text and urlparse(license_url).scheme in {"http", "https"}:
                license_text = f'<a href="{escape(license_url, quote=True)}" rel="noreferrer">{license_text}</a>'
            details = f" · {license_text}" if license_text else ""
            if provenance.get("modifications"):
                details += f" · {escape(provenance['modifications'])}"
            credits.append(f"<li>{credit} · {escape(provider.upper() if provider == 'nasa' else provider.title())} ({kind}){details}</li>")
    title = escape(fact["title"])
    quality_notes = "".join(f"<li>{escape(note)}</li>" for note in fact.get("quality_warnings", []))
    quality_section = f"<h2>Editorial review notes</h2><ul>{quality_notes}</ul>" if quality_notes else ""
    story_section = ""
    preview_script = ""
    report_path = directory / "image_report.json"
    if report_path.exists():
        quality = json.loads(report_path.read_text(encoding="utf-8")).get("quality", {})
        notes = "".join(f"<li>{escape(note)}</li>" for note in quality.get("warnings", []))
        if quality:
            story_section += (f'<h2>Visual pacing</h2><p>Planned visual coverage: {quality.get("coverage_percent", 0):.1f}% (timeline check)</p>'
                              f'<p>Moving media: {quality.get("moving_media_percent", 0):.1f}% · '
                              f'Text-led scenes: {quality.get("text_only_percent", 0):.1f}% · '
                              f'Missing-visual fallbacks: {quality.get("fallback_percent", 0):.1f}%</p>'
                              f'<ul>{notes}</ul><a href="image_report.json">Visual review details</a>')
    if (directory / "storyboard.json").exists():
        beats = json.loads((directory / "storyboard.json").read_text(encoding="utf-8"))["beats"]
        rows = "".join(f'<tr><td><button type="button" data-time="{b["start"]:.3f}">{b["start"]:.1f}–{b["end"]:.1f}s</button></td>'
                       f'<td>{escape(b["role"])} · {escape(b["visual"]["kind"])}</td><td>{escape(b["narration"])}</td></tr>' for b in beats)
        story_section += f'<h2>Story beats</h2><table><thead><tr><th>Preview</th><th>Purpose</th><th>Narration</th></tr></thead><tbody>{rows}</tbody></table>'
        preview_script = "<script>document.querySelectorAll('button[data-time]').forEach(b=>b.addEventListener('click',()=>{const v=document.querySelector('video');v.currentTime=Number(b.dataset.time);v.play();}));</script>"
    experiment_section = ""
    if (directory / "experiment.json").exists():
        experiment = json.loads((directory / "experiment.json").read_text(encoding="utf-8"))
        experiment_section = (f'<h2>Engagement experiment</h2><p>{escape(experiment["experiment"])} · {escape(experiment["variant"])}</p>'
                              '<div class="links"><a href="experiment.json">Experiment settings</a><a href="engagement.json">Studio metrics</a></div>')
        experiment_section += '<p>Use the same hours after publication for each comparison. Change one variable across similar topics.</p>'
    if (directory / "engagement.json").exists():
        snapshots = json.loads((directory / "engagement.json").read_text(encoding="utf-8")).get("snapshots", [])
        if snapshots:
            for metric in sorted(snapshots, key=lambda s: s["window_hours"]):
                experiment_section += (f'<p>First {metric["window_hours"]:g} hours: {metric["stayed_to_watch"]:g}% stayed to watch · '
                    f'{metric["average_percentage_viewed"]:g}% average viewed · {metric["average_view_duration"]:g}s average duration · '
                    f'{metric["engaged_views"]:g} engaged views</p>')
                if metric.get("retention_drop_seconds"):
                    experiment_section += '<p>Retention drops: ' + ', '.join(f'{x:g}s' for x in metric["retention_drop_seconds"]) + '</p>'
        else:
            experiment_section += '<p>No audience metrics recorded yet.</p>'
    publishing = ""
    if (directory / "youtube_metadata.json").is_file():
        package = json.loads((directory / "youtube_metadata.json").read_text(encoding="utf-8"))
        snippet = package["snippet"]
        publishing = (f'<h2>YouTube upload · {escape(package["status"]["privacyStatus"])}</h2>'
                      f'<p><strong>{escape(snippet["title"])}</strong></p><p>{escape(snippet["description"])}</p>'
                      '<div class="links">'
                      '<a href="youtube_metadata.json">Upload metadata</a></div>')
    html = f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · Review</title>
<style>
:root{{color-scheme:dark;font:16px/1.65 system-ui,sans-serif;background:#10151c;color:#eef1f5}}
body{{max-width:1100px;margin:0 auto;padding:40px 24px}}header{{margin-bottom:28px}}
.eyebrow{{text-transform:uppercase;letter-spacing:.16em;color:#70debd;font-size:.75rem;font-weight:700}}
h1{{font-size:clamp(1.8rem,4vw,3rem);line-height:1.15;max-width:850px;margin:12px 0}}
main{{display:grid;grid-template-columns:minmax(240px,360px) 1fr;gap:40px}}video{{width:100%;border-radius:18px;background:#000;max-height:76vh}}
h2{{font-size:1rem;color:#70debd;margin:28px 0 8px}}p{{white-space:pre-line}}a{{color:#9cbcff}}
.note{{padding:16px 20px;background:#25241b;border-left:3px solid #e2c26a;border-radius:4px}}
.links{{display:flex;gap:20px;flex-wrap:wrap}}small{{color:#a0aebb}}@media(max-width:700px){{main{{grid-template-columns:1fr}}video{{max-height:65vh}}}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}th,td{{text-align:left;padding:9px 6px;border-bottom:1px solid #334150;vertical-align:top}}button{{color:#9cbcff;background:#1b2938;border:0;padding:6px;cursor:pointer}}
</style><header><div class="eyebrow">Fact Shorts / Review</div><h1>{title}</h1>
<small>{escape(fact.get('category', 'Custom script'))} · {duration:.1f} seconds · {len(fact['fact_text'].split())} words</small></header>
<main><section><video controls preload="metadata" src="video.mp4"></video><div class="links"><a href="video.mp4" download>Download video</a><a href="captions.srt" download>Captions</a><a href="fact.json">Script data</a></div></section>
<article><div class="note">Review the narration, captions, image relevance, and factual claims before publishing. Source links are supplied references; they have not been independently verified by this pipeline.</div>
{quality_section}{story_section}{experiment_section}{publishing}<h2>Narration</h2><p>{escape(fact['fact_text'])}</p><h2>Description</h2><p>{escape(fact.get('description',''))}</p>
<h2>Source references</h2><ul>{''.join(sources) or '<li>No references supplied.</li>'}</ul>
<h2>Image credits</h2><ul>{''.join(dict.fromkeys(credits)) or '<li>No stock image credits in this run.</li>'}</ul>
</article></main>{preview_script}</html>'''
    path = directory / "review.html"
    path.write_text(html, encoding="utf-8")
    return path
