from typing import Optional
import json, re, os, subprocess, xml.etree.ElementTree as ET
import shlex
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# QoE weights
W1, W2, W3, W4 = 0.8469, -28.7959, 0.2979, -1.0610

RE_ADD   = re.compile(r"Adding segment with index (\d+)")
RE_URL   = re.compile(r'/avc/(\d+)/segment_(\d+)\.m4s')
RE_STALL = re.compile(r"Stall duration:\s*\"?\s*([0-9.]+)")
RE_SSIM_ALL = re.compile(r"All:([0-9]*\.?[0-9]+)")

def parse_mpd(mpd_path: str):
    ns = {"d": "urn:mpeg:dash:schema:mpd:2011"}
    root = ET.parse(mpd_path).getroot()
    st = root.find(".//d:SegmentTemplate", ns)
    timescale = int(st.attrib["timescale"])
    duration = int(st.attrib["duration"])
    seg_dur = duration / timescale  # seconds

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

    return {k:v for k,v in seg.items() if v["repId"] and v["mediaSegNo"] is not None}

def make_seg_mp4(base_dir: Path, rep_id: str, seg_no: int, out_path: Path, init_cache: dict) -> Path:
    if out_path.exists():
        return out_path

    initp = base_dir / "avc" / rep_id / "segment_.mp4"
    segp  = base_dir / "avc" / rep_id / f"segment_{seg_no:03d}.m4s"
    if not initp.exists(): raise FileNotFoundError(initp)
    if not segp.exists():  raise FileNotFoundError(segp)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if rep_id not in init_cache:
        init_cache[rep_id] = initp.read_bytes()

    with out_path.open("wb") as f:
        f.write(init_cache[rep_id])
        with segp.open("rb") as sf:
            f.write(sf.read())

    return out_path

def _parse_ssim_all(stats_path: Path) -> float:
    txt = stats_path.read_text(errors="ignore")
    for line in reversed([ln.strip() for ln in txt.splitlines() if ln.strip()]):
        m = RE_SSIM_ALL.search(line)
        if m:
            return float(m.group(1))
    raise RuntimeError(f"Could not parse SSIM from stats file: {stats_path}")


def run_ffmpeg_metrics(
    dist_mp4: Path,
    ref_mp4: Path,
    fps: int,
    out_vmaf_json,
    out_ssim_txt,
    calc_vmaf: bool = True,
    calc_ssim: bool = True,
):
    """
    Returns (vmaf_mean | None, ssim_all | None).
    Ensures filtergraph has at least one mapped output (required by ffmpeg).
    """
    if not calc_vmaf and not calc_ssim:
        return None, None

    if calc_vmaf and out_vmaf_json is not None:
        out_vmaf_json.parent.mkdir(parents=True, exist_ok=True)
    if calc_ssim and out_ssim_txt is not None:
        out_ssim_txt.parent.mkdir(parents=True, exist_ok=True)

    parts = [
        f"[0:v]setpts=PTS-STARTPTS,fps={fps}[d]",
        f"[1:v]setpts=PTS-STARTPTS,fps={fps}[r]",
        f"[d][r]scale2ref[d2][r2]",
    ]

    # We will expose exactly one output label to map.
    # Any additional branch gets nullsink so ffmpeg doesn't complain.
    if calc_vmaf and calc_ssim:
        # Two metric branches. Keep VMAF branch as the mapped output.
        parts += [
            "[d2]split=2[dv1][dv2]",
            "[r2]split=2[rv1][rv2]",
            f"[dv1][rv1]libvmaf=log_fmt=json:log_path={out_vmaf_json}[vout]",
            f"[dv2][rv2]ssim=stats_file={out_ssim_txt}[sout]",
            "[sout]nullsink",
        ]
        map_label = "[vout]"

    elif calc_vmaf:
        parts += [
            f"[d2][r2]libvmaf=log_fmt=json:log_path={out_vmaf_json}[vout]",
        ]
        map_label = "[vout]"

    else:  # calc_ssim only
        parts += [
            f"[d2][r2]ssim=stats_file={out_ssim_txt}[sout]",
        ]
        map_label = "[sout]"

    filt = ";".join(parts)

    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-loglevel", "error",
        "-nostdin",
        "-threads", "1",
        "-i", str(dist_mp4),
        "-i", str(ref_mp4),
        "-filter_complex", filt,
        "-map", map_label,
        "-an", "-sn", "-dn",
        "-f", "null", "-"
    ]

    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    if p.returncode != 0:
        cmd_str = " ".join(shlex.quote(x) for x in cmd)
        raise RuntimeError(
            "ffmpeg failed\n"
            f"CMD:\n{cmd_str}\n\n"
            f"FILTER_COMPLEX:\n{filt}\n\n"
            f"FFMPEG_OUTPUT:\n{p.stdout}"
        )

    vmaf_mean = None
    ssim_all = None

    if calc_vmaf:
        data = json.loads(out_vmaf_json.read_text())
        vmaf_mean = float(data["pooled_metrics"]["vmaf"]["mean"])

    if calc_ssim:
        ssim_all = _parse_ssim_all(out_ssim_txt)

    return vmaf_mean, ssim_all

def vmaf_job(base: Path, reps: dict, ref_rep_id: str, playback_idx: int, rep_id: str, media_no: int,
             stall: float, work: Path, init_cache: dict, calc_vmaf: bool, calc_ssim: bool):
    fps = reps.get(rep_id, reps[ref_rep_id])["fps"]

    dist_mp4 = make_seg_mp4(base, rep_id, media_no, work / "segs" / f"rep{rep_id}_seg{media_no:03d}.mp4", init_cache)
    ref_mp4  = make_seg_mp4(base, ref_rep_id, media_no, work / "segs" / f"rep{ref_rep_id}_seg{media_no:03d}.mp4", init_cache)

    out_vmaf_json = work / "vmaf" / f"vmaf_play{playback_idx:03d}_media{media_no:03d}_rep{rep_id}.json" if calc_vmaf else None
    out_ssim_txt  = work / "ssim" / f"ssim_play{playback_idx:03d}_media{media_no:03d}_rep{rep_id}.txt"  if calc_ssim else None

    v, s = run_ffmpeg_metrics(
        dist_mp4, ref_mp4, fps=fps,
        out_vmaf_json=out_vmaf_json,
        out_ssim_txt=out_ssim_txt,
        calc_vmaf=calc_vmaf,
        calc_ssim=calc_ssim,
    )

    bw = reps[rep_id]["bandwidth"] if rep_id in reps else None
    return {
        "playback_idx": playback_idx,
        "media_no": media_no,
        "rep_id": rep_id,
        "bandwidth": bw,
        "stall": stall,
        "vmaf": v,
        "ssim": s,
    }

def main(mpd_path, dash_root_dir, chrome_logs_txt,
         ref_rep_id="9",
         max_media_no=60,
         max_workers=None,
         work_dir="qoe_work",
         calc_vmaf=True,
         calc_ssim=True):

    seg_dur, reps = parse_mpd(mpd_path)
    base = Path(dash_root_dir)
    # Place qoe_work in host-applications directory (next to logs)
    work = Path(chrome_logs_txt).parent / work_dir
    work.mkdir(parents=True, exist_ok=True)

    log_lines = Path(chrome_logs_txt).read_text().splitlines()
    segmap = parse_console_logs(log_lines)

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

    if max_workers is None:
        cpu = os.cpu_count() or 4
        max_workers = max(2, min(8, cpu // 2))

    print(f"Segments to score: {len(tasks)} (cutoff media_no<= {max_media_no}), workers={max_workers}")
    print(f"Segment duration (from MPD): {seg_dur}s")
    print(f"Metrics: VMAF={'on' if calc_vmaf else 'off'}  SSIM={'on' if calc_ssim else 'off'}")

    results = {}
    failures = []
    init_cache = {}

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = []
        for playback_idx, rep_id, media_no, stall in tasks:
            futs.append(ex.submit(
                vmaf_job, base, reps, ref_rep_id,
                playback_idx, rep_id, media_no, stall, work, init_cache,
                calc_vmaf, calc_ssim
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

    ordered = [results[k] for k in sorted(results.keys())]

    # Only build q from VMAF if enabled; QoE formula currently assumes q_list is VMAF-like.
    if calc_vmaf:
        q = [x["vmaf"] for x in ordered]
    else:
        q = []

    t = [x["stall"] for x in ordered]

    max_shown = 60
    for x in ordered[:max_shown]:
        msg = (
            f"play={x['playback_idx']:03d} media={x['media_no']:03d} rep={x['rep_id']} "
            f"bw={x['bandwidth']} stall={x['stall']:.3f}s"
        )
        if calc_vmaf:
            msg += f" vmafToTop={x['vmaf']:.2f}"
        if calc_ssim:
            msg += f" ssimToTop={x['ssim']:.5f}"
        print(msg)

    if len(ordered) > max_shown:
        print(f"... ({len(ordered)-max_shown} more)")

    # Prepare metrics summary
    sum_vmaf = sum(q) if calc_vmaf else None
    sum_stall = sum(t)
    avg_vmaf = (sum_vmaf / len(q)) if (calc_vmaf and q) else None
    avg_ssim = (sum(x["ssim"] for x in ordered) / len(ordered)) if (calc_ssim and ordered) else None
    qoe = compute_qoe(q, t) if calc_vmaf else None

    # Save metrics to JSON file in logs directory
    logs_dir = Path(chrome_logs_txt).parent
    
    # Create output filename based on input log file name
    log_basename = Path(chrome_logs_txt).stem  # e.g., "player_log" from "player_log.txt"
    output_json = logs_dir / "qoe_metrics.json"
    
    metrics_output = {
        "summary": {
            "scored_segments": len(ordered),
            "segment_duration_seconds": seg_dur,
            "total_stall_seconds": sum_stall,
            "qoe_score": qoe,
            "metrics_calculated": {
                "vmaf": calc_vmaf,
                "ssim": calc_ssim
            }
        },
        "per_segment": ordered
    }
    
    if calc_vmaf:
        metrics_output["summary"]["total_vmaf"] = sum_vmaf
        metrics_output["summary"]["average_vmaf"] = avg_vmaf
    
    if calc_ssim:
        metrics_output["summary"]["average_ssim"] = avg_ssim
    
    with output_json.open("w") as f:
        json.dump(metrics_output, f, indent=2)
    
    print(f"\n=== Metrics saved to: {output_json} ===")

    print("\n=== RESULT ===")
    if calc_vmaf:
        print(f"scored_segments={len(q)}  sumVMAF={sum_vmaf:.2f}  sumStall={sum_stall:.3f}s  QoE={qoe:.3f}")
    else:
        print(f"scored_segments={len(ordered)}  sumStall={sum_stall:.3f}s  avgSSIM={avg_ssim if avg_ssim is not None else 'n/a'}")
        print("Note: QoE not computed because calc_vmaf=False and QoE formula currently uses VMAF as q_list.")

if __name__ == "__main__":
    import sys

    # Usage:
    #   python3 qoe.py mpd.mpd dash_root logs.txt
    # Optional env toggles:
    #   CALC_VMAF=0 CALC_SSIM=1 python3 qoe.py ...
    calc_vmaf = os.environ.get("CALC_VMAF", "0") not in ("0", "false", "False", "no", "NO")
    calc_ssim = os.environ.get("CALC_SSIM", "1") not in ("0", "false", "False", "no", "NO")
    os.environ["LD_LIBRARY_PATH"] = os.environ.get("LD_LIBRARY_PATH", "") + ":/usr/local/lib/x86_64-linux-gnu"

    if len(sys.argv) != 4:
        print("Usage: python3 qoe.py <mpd.mpd> <dash_root_dir> <chrome_console_logs.txt>")
        print("Env toggles: CALC_VMAF=0/1  CALC_SSIM=0/1")
        sys.exit(2)

    main(sys.argv[1], sys.argv[2], sys.argv[3], calc_vmaf=calc_vmaf, calc_ssim=calc_ssim)
