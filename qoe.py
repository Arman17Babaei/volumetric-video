import json, re, os, subprocess, xml.etree.ElementTree as ET
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# QoE weights
W1, W2, W3, W4 = 0.8469, -28.7959, 0.2979, -1.0610

RE_ADD   = re.compile(r"Adding segment with index (\d+)")
RE_URL   = re.compile(r'/avc/(\d+)/segment_(\d+)\.m4s')
RE_STALL = re.compile(r"Stall duration:\s*\"?\s*([0-9.]+)")

def parse_mpd(mpd_path: str):
    ns = {"d": "urn:mpeg:dash:schema:mpd:2011"}
    root = ET.parse(mpd_path).getroot()
    st = root.find(".//d:SegmentTemplate", ns)
    timescale = int(st.attrib["timescale"])
    duration = int(st.attrib["duration"])
    seg_dur = duration / timescale  # seconds (4.0 in your MPD)

    reps = {}
    for rep in root.findall(".//d:Representation", ns):
        reps[rep.attrib["id"]] = {
            "bandwidth": int(rep.attrib["bandwidth"]),
            "fps": int(rep.attrib.get("frameRate", "30")),
        }
    return seg_dur, reps

def compute_qoe(q_list, t_list):
    N = len(q_list)
    inc = sum(max(q_list[i+1]-q_list[i], 0.0) for i in range(N-1))
    dec = sum(max(q_list[i]-q_list[i+1], 0.0) for i in range(N-1))
    return W1*sum(q_list) + W2*sum(t_list) + W3*inc + W4*dec

def parse_console_logs(log_lines):
    """
    Returns dict: playbackSegIndex -> {repId, stallSec, mediaSegNo}
    playbackSegIndex is what you print as "Adding segment with index X"
    mediaSegNo is extracted from URL "segment_###.m4s"
    """
    seg = {}
    current_seg = None

    for line in log_lines:
        m = RE_ADD.search(line)
        if m:
            current_seg = int(m.group(1))
            seg.setdefault(current_seg, {"repId": None, "stallSec": 0.0, "mediaSegNo": None})
            continue

        m = RE_URL.search(line)
        if m and current_seg is not None:
            rep_id = m.group(1)
            media_seg_no = int(m.group(2))
            seg[current_seg]["repId"] = rep_id
            seg[current_seg]["mediaSegNo"] = media_seg_no
            continue

        m = RE_STALL.search(line)
        if m and current_seg is not None:
            seg[current_seg]["stallSec"] += float(m.group(1))

    # keep only fully identified segments
    seg = {k:v for k,v in seg.items() if v["repId"] and v["mediaSegNo"] is not None}
    return seg

def make_seg_mp4(base_dir: Path, rep_id: str, seg_no: int, out_path: Path) -> Path:
    """
    Build a standalone mp4 for a single segment using init + one m4s fragment.
    Writes to out_path if it doesn't exist.
    """
    if out_path.exists():
        return out_path

    initp = base_dir / "avc" / rep_id / "segment_.mp4"
    segp  = base_dir / "avc" / rep_id / f"segment_{seg_no:03d}.m4s"
    if not initp.exists(): raise FileNotFoundError(initp)
    if not segp.exists():  raise FileNotFoundError(segp)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(initp.read_bytes() + segp.read_bytes())
    return out_path

def run_ffmpeg_vmaf(dist_mp4: Path, ref_mp4: Path, fps: int, out_json: Path) -> float:
    """
    VMAF(dist, ref) pooled mean. Uses scale2ref to match resolutions.
    Sets -threads 1 to avoid oversubscription when running many ffmpegs in parallel.
    """
    out_json.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-threads", "1",
        "-i", str(dist_mp4),
        "-i", str(ref_mp4),
        "-filter_complex",
        (
            f"[0:v]setpts=PTS-STARTPTS,fps={fps}[d];"
            f"[1:v]setpts=PTS-STARTPTS,fps={fps}[r];"
            f"[d][r]scale2ref[d2][r2];"
            f"[d2][r2]libvmaf=log_fmt=json:log_path={out_json}"
        ),
        "-f", "null", "-"
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{p.stdout}")

    data = json.loads(out_json.read_text())
    return float(data["pooled_metrics"]["vmaf"]["mean"])

def vmaf_job(base: Path, reps: dict, ref_rep_id: str, playback_idx: int, rep_id: str, media_no: int, stall: float, work: Path):
    """
    One parallel task: build dist/ref segment MP4s, run vmaf, return results.
    """
    # pick fps (use current rep's fps; fallback to ref rep fps)
    fps = reps.get(rep_id, reps[ref_rep_id])["fps"]

    dist_mp4 = make_seg_mp4(base, rep_id, media_no, work / "segs" / f"rep{rep_id}_seg{media_no:03d}.mp4")
    ref_mp4  = make_seg_mp4(base, ref_rep_id, media_no, work / "segs" / f"rep{ref_rep_id}_seg{media_no:03d}.mp4")

    out_json = work / "vmaf" / f"vmaf_play{playback_idx:03d}_media{media_no:03d}_rep{rep_id}.json"
    v = run_ffmpeg_vmaf(dist_mp4, ref_mp4, fps=fps, out_json=out_json)

    bw = reps[rep_id]["bandwidth"] if rep_id in reps else None
    return {
        "playback_idx": playback_idx,
        "media_no": media_no,
        "rep_id": rep_id,
        "bandwidth": bw,
        "stall": stall,
        "vmaf": v
    }

def main(mpd_path, dash_root_dir, chrome_logs_txt,
         ref_rep_id="9",
         max_media_no=60,
         max_workers=None,
         work_dir="qoe_work"):

    seg_dur, reps = parse_mpd(mpd_path)
    base = Path(dash_root_dir)
    work = Path(work_dir); work.mkdir(parents=True, exist_ok=True)

    log_lines = Path(chrome_logs_txt).read_text().splitlines()
    segmap = parse_console_logs(log_lines)

    # Build tasks list
    tasks = []
    for playback_idx in sorted(segmap.keys()):
        rep_id = segmap[playback_idx]["repId"]
        media_no = segmap[playback_idx]["mediaSegNo"]
        stall = float(segmap[playback_idx]["stallSec"])
        if media_no > max_media_no:
            continue
        tasks.append((playback_idx, rep_id, media_no, stall))

    if not tasks:
        raise RuntimeError("No usable segments found in logs after applying cutoff.")

    # sensible default workers
    if max_workers is None:
        cpu = os.cpu_count() or 4
        max_workers = max(2, min(8, cpu // 2))

    print(f"Segments to score: {len(tasks)} (cutoff media_no<= {max_media_no}), workers={max_workers}")
    print(f"Segment duration (from MPD): {seg_dur}s")

    results = {}
    failures = []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = []
        for playback_idx, rep_id, media_no, stall in tasks:
            futs.append(ex.submit(
                vmaf_job, base, reps, ref_rep_id,
                playback_idx, rep_id, media_no, stall, work
            ))

        for fut in as_completed(futs):
            try:
                r = fut.result()
                results[r["playback_idx"]] = r
            except Exception as e:
                failures.append(str(e))

    if failures:
        print("\nSome segments failed to score (showing first 3):")
        for f in failures[:3]:
            print("----")
            print(f)

    # Build ordered q/t lists
    ordered = [results[k] for k in sorted(results.keys())]
    q = [x["vmaf"] for x in ordered]
    t = [x["stall"] for x in ordered]

    for x in ordered[:10]:
        print(f"play={x['playback_idx']:03d} media={x['media_no']:03d} rep={x['rep_id']} "
              f"bw={x['bandwidth']} stall={x['stall']:.3f}s vmafToTop={x['vmaf']:.2f}")
    if len(ordered) > 10:
        print(f"... ({len(ordered)-10} more)")

    qoe = compute_qoe(q, t)
    print("\n=== RESULT ===")
    print(f"scored_segments={len(q)}  sumVMAF={sum(q):.2f}  sumStall={sum(t):.3f}s  QoE={qoe:.3f}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 4:
        print("Usage: python3 qoe.py <mpd.mpd> <dash_root_dir> <chrome_console_logs.txt>")
        sys.exit(2)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
