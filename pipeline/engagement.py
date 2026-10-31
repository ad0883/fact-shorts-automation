"""Local experiment records and manual YouTube Studio metric snapshots.

No analytics access is implied by upload authorization. Empty metrics remain empty.
"""

import argparse
from datetime import datetime, timezone
import math
import re
from pathlib import Path
from statistics import median

from pipeline.run_store import read_json, write_json, run_lock, file_hash


METRICS = ("stayed_to_watch", "average_percentage_viewed", "average_view_duration", "engaged_views")


def prepare_experiment(directory, fact, duration, options, layout):
    path = Path(directory) / "experiment.json"
    previous = read_json(path) if path.exists() else {}
    video = Path(directory) / "video.mp4"
    digest = file_hash(video) if video.exists() else None
    record = {**previous, "experiment": options.get("experiment", previous.get("experiment", "unassigned")),
              "variant": options.get("variant", previous.get("variant", "story-v1" if layout == "story" else layout)),
              "title": fact["title"], "category": fact.get("category", ""),
              "duration_seconds": round(duration, 3), "word_count": len(fact["fact_text"].split()),
              "layout": layout, "opening": re.split(r"(?<=[.!?])\s+", fact["fact_text"], maxsplit=1)[0],
              "video_sha256": digest,
              "hypothesis": previous.get("hypothesis", ""),
              "changed_variable": previous.get("changed_variable", ""),
              "metrics_file": "engagement.json"}
    write_json(path, record)
    metrics = Path(directory) / "engagement.json"
    if metrics.exists():
        prior_metrics = read_json(metrics)
        old_digest = prior_metrics.get("video_sha256")
        if old_digest and old_digest != digest:
            # A re-render is a different artifact. Keep the old observations, but
            # never attribute an uploaded version's audience response to the edit.
            write_json(Path(directory) / f"engagement-{old_digest[:16]}.json", prior_metrics)
            write_json(metrics, {"source": "manual YouTube Studio entry", "video_sha256": digest, "snapshots": []})
    else:
        write_json(metrics, {"source": "manual YouTube Studio entry", "video_sha256": digest, "snapshots": []})
    return record


def record_metrics(directory, *, window_hours, stayed_to_watch, average_percentage_viewed,
                   average_view_duration, engaged_views, retention_drops=None):
    directory = Path(directory).resolve()
    if not (directory / "run.json").is_file():
        raise ValueError("Metrics need a run directory containing run.json")
    values = dict(window_hours=window_hours, stayed_to_watch=stayed_to_watch,
                  average_percentage_viewed=average_percentage_viewed,
                  average_view_duration=average_view_duration, engaged_views=engaged_views)
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0 for v in values.values()):
        raise ValueError("Metrics must be finite, nonnegative numbers")
    if window_hours <= 0 or stayed_to_watch > 100 or int(engaged_views) != engaged_views:
        raise ValueError("Use a positive observation window, stayed-to-watch in 0-100, and whole engaged views")
    duration = read_json(directory / "run.json").get("duration_seconds")
    drops = retention_drops or []
    if any(not math.isfinite(x) or x < 0 or (duration and x > duration) for x in drops):
        raise ValueError("Retention drop timestamps must fall inside the video")
    snapshot = {**values, "retention_drop_seconds": drops, "recorded_at": datetime.now(timezone.utc).isoformat()}
    with run_lock(directory):
        path = directory / "engagement.json"
        data = read_json(path) if path.exists() else {"source": "manual YouTube Studio entry", "snapshots": []}
        digest = file_hash(directory / "video.mp4")
        if data.get("video_sha256") and data["video_sha256"] != digest:
            raise ValueError("Video changed since these metrics were recorded. Label the new version before recording metrics.")
        data["video_sha256"] = digest
        # A corrected entry for the same observation window replaces that window only.
        data["snapshots"] = [s for s in data["snapshots"] if s["window_hours"] != window_hours] + [snapshot]
        write_json(path, data)
    return path


def compare_experiments(root, experiment, baseline="baseline"):
    groups = {}
    for path in sorted(Path(root).rglob("experiment.json")):
        record = read_json(path)
        if record.get("experiment") != experiment or not (path.parent / "engagement.json").exists():
            continue
        data = read_json(path.parent / "engagement.json")
        if data.get("video_sha256") != record.get("video_sha256"):
            continue
        for snapshot in data.get("snapshots", []):
            key = (record["variant"], snapshot["window_hours"])
            groups.setdefault(key, []).append((record, snapshot))
    rows = []
    for (variant, window), entries in sorted(groups.items()):
        # Do not treat zero-engaged-view percentages as audience evidence.
        observed = [s for _, s in entries if s["engaged_views"] > 0]
        row = {"variant": variant, "window_hours": window, "videos": len(entries),
               "videos_with_engaged_views": len(observed),
               "median_duration_seconds": median(r["duration_seconds"] for r, _ in entries),
               "median_engaged_views": median(s["engaged_views"] for _, s in entries)}
        for metric in METRICS[:3]:
            row["median_"+metric] = median(s[metric] for s in observed) if observed else None
        rows.append(row)
    for row in rows:
        control = next((r for r in rows if r["variant"] == baseline and r["window_hours"] == row["window_hours"]), None)
        row["delta_vs_baseline"] = {metric: round(row["median_"+metric]-control["median_"+metric], 2)
            for metric in METRICS[:3] if control and control["median_"+metric] is not None and row["median_"+metric] is not None}
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record Studio metrics and compare local experiment variants.")
    commands = parser.add_subparsers(dest="command", required=True)
    label = commands.add_parser("label", help="Tag an existing video without re-rendering it")
    label.add_argument("run", type=Path)
    label.add_argument("--experiment", required=True)
    label.add_argument("--variant", required=True)
    label.add_argument("--hypothesis", default="")
    label.add_argument("--changed-variable", default="")
    record = commands.add_parser("record")
    record.add_argument("run", type=Path)
    record.add_argument("--window-hours", type=float, required=True, help="First N hours after publication, using the same window across variants")
    for metric in METRICS:
        record.add_argument("--"+metric.replace("_", "-"), type=int if metric == "engaged_views" else float, required=True)
    record.add_argument("--retention-drop", type=float, action="append", default=[], help="A timestamp in seconds where viewers leave; repeat as needed")
    compare = commands.add_parser("compare")
    compare.add_argument("--output-dir", type=Path, default=Path("output"))
    compare.add_argument("--experiment", required=True)
    compare.add_argument("--baseline", default="baseline")
    args = parser.parse_args(argv)
    try:
        if args.command == "label":
            if any(not v.strip() or len(v) > 100 for v in (args.experiment, args.variant)):
                raise ValueError("Experiment and variant need 1-100 characters")
            with run_lock(args.run):
                state = read_json(args.run / "run.json")
                if not state.get("duration_seconds") or not (args.run / "video.mp4").is_file():
                    raise ValueError("Label a completed local video")
                old = read_json(args.run / "experiment.json") if (args.run / "experiment.json").exists() else {}
                result = prepare_experiment(args.run, read_json(args.run / "fact.json"), state["duration_seconds"],
                    {"experiment": args.experiment, "variant": args.variant}, old.get("layout", "unrecorded"))
                result.update(hypothesis=args.hypothesis, changed_variable=args.changed_variable)
                write_json(args.run / "experiment.json", result)
                state.setdefault("options", {}).update(experiment=args.experiment, variant=args.variant)
                write_json(args.run / "run.json", state)
            print(f"Labeled {args.run}")
        elif args.command == "record":
            values = {key: getattr(args, key) for key in METRICS}
            path = record_metrics(args.run, window_hours=args.window_hours, retention_drops=args.retention_drop, **values)
            print(f"Saved {path}")
            # Refresh the review page without rendering or contacting YouTube.
            if (args.run / "fact.json").exists() and (args.run / "image_segments.json").exists():
                from pipeline.review import write_review
                duration = read_json(args.run / "run.json").get("duration_seconds")
                if duration:
                    write_review(args.run, read_json(args.run / "fact.json"), duration=duration,
                                 image_segments=read_json(args.run / "image_segments.json"))
        else:
            rows = compare_experiments(args.output_dir, args.experiment, args.baseline)
            if not rows:
                print("No recorded metrics for this experiment yet.")
            else:
                import json
                print(json.dumps(rows, indent=2))
                print("Compare similar topics and equal observation windows. These are descriptive medians, not a randomized A/B test.")
        return 0
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
