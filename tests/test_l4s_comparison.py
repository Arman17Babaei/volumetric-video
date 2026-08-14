import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("analysis", ROOT / "analyze_l4s_comparison.py")
analysis = importlib.util.module_from_spec(spec); sys.modules[spec.name] = analysis; spec.loader.exec_module(analysis)


def test_schedule():
    assert analysis.planned_events(10) == [
        (0.0,"start","A"),(10.0,"start","B"),(20.0,"stop","A"),
        (30.0,"start","A"),(40.0,"stop","B"),(50.0,"stop","A")]


def test_ss_parser():
    row=analysis.parse_ss_line("cwnd:12 rtt:1.5/0.2 bytes_acked:1000 bytes_sent:1200 retrans:0/3")
    assert row["cwnd_packets"]==12
    assert row["rtt_ms"]==1.5
    assert row["retrans"]==3


def test_packet_matching_with_retransmission_order():
    P=analysis.Packet
    a1=P(1,"a","b",5201,1,10,20,"PA",100,1)
    a2=P(2,"a","b",5201,1,10,20,"PA",100,1)
    b1=P(1.2,"a","b",5201,1,10,20,"PA",100,3)
    b2=P(2.2,"a","b",5201,1,10,20,"PA",100,1)
    assert analysis.match_packets([a1,a2],[b1,b2])==[(a1,b1),(a2,b2)]


def test_queue_baseline_and_clamp():
    assert analysis.estimate_baseline_us([100,105,110,1000])==100
    assert analysis.corrected_queue_delay_us(95,100)==0
    assert analysis.corrected_queue_delay_us(125,100)==25


def test_time_bins():
    bins=analysis.time_bin([(0.01,1),(0.24,2),(0.26,3)],.25)
    assert bins[0]==[1,2] and bins[1]==[3]


def test_marking_curve():
    rows=[]
    rows += [{"queue_delay_us":100,"ce_marked":0} for _ in range(10)]
    rows += [{"queue_delay_us":1100,"ce_marked":1} for _ in range(10)]
    curve=analysis.marking_curve(rows,bin_ms=.25,min_samples=5)
    assert len(curve)==2
    assert curve[0]["ce_probability"]==0
    assert curve[1]["ce_probability"]==1


def test_already_ce_is_not_new_mark():
    P=analysis.Packet
    inc=P(1,"a","b",5201,1,1,1,"A",60,3)
    out=P(1.1,"a","b",5201,1,1,1,"A",60,3)
    assert not (inc.ecn==1 and out.ecn==3)
