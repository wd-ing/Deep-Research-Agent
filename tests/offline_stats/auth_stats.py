"""Offline authority stats over data/refs snapshots (read-only). Does not touch retrieval.

口径:
- 来源编号 = session 内 source 表 rowid 升序 (nodes._load_numbered_evidence 同款)
- 报告引用 = report.markdown 里 [S<n>·<m>] / [S<n>] 的 S<n> (按来源计, 同一来源多次引用计多次)
- 只统计 report 存在且 session 唯一的库 (v13_gate 有 20 份 report 的汇总库排除)
- 权威性分类 = 域名规则表 (脚本内, 不进检索代码), 与 source_nature 对照
"""
import re, sqlite3, glob, os, sys, json
from collections import Counter, defaultdict

SNAP = "/tmp/auth/snap"
CITE_RE = re.compile(r"\[S(\d+)(?:·\d+)?[^\]]*\]")
CITE_ANY = re.compile(r"S(\d+)(?:·(\d+))?")

LOW = {  # 低权威: 社媒/问答/内容农场/文库转存
    "weibo.com", "xueqiu.com", "zhihu.com", "zhuanlan.zhihu.com", "tieba.baidu.com",
    "douban.com", "bilibili.com", "xiaohongshu.com", "douyin.com", "reddit.com",
    "quora.com", "medium.com", "jianshu.com", "csdn.net", "blog.csdn.net",
    "cnblogs.com", "51cto.com", "juejin.cn", "baijiahao.baidu.com", "sohu.com",
    "163.com", "toutiao.com", "wenku.baidu.com", "docin.com", "doc88.com",
    "book118.com", "renrendoc.com", "max.book118.com", "yunpan.plus",
    "mbd.baidu.com", "zhidao.baidu.com", "ixigua.com",
}
HIGH_SUFFIX = (".gov", ".gov.cn", ".edu", ".edu.cn", ".ac.cn", ".org.cn")
HIGH = {  # 官方/标准/学术/一手: 保守规则, 只放明显的
    "arxiv.org", "nature.com", "science.org", "nih.gov", "who.int", "ieee.org",
    "acm.org", "springer.com", "sciencedirect.com", "pubmed.ncbi.nlm.nih.gov",
    "doi.org", "github.com", "rust-lang.org", "go.dev", "golang.org",
    "doc.rust-lang.org", "kubernetes.io", "python.org", "w3.org", "ietf.org",
    "europa.eu", "fda.gov", "nmpa.gov.cn", "cnipa.gov.cn", "samr.gov.cn",
}


def tier(domain: str) -> str:
    d = (domain or "").lower().removeprefix("www.")
    parts = d.split(".")
    cands = {d} | {".".join(parts[i:]) for i in range(len(parts) - 1)}
    if cands & LOW:
        return "low"
    if cands & HIGH or d.endswith(HIGH_SUFFIX):
        return "high"
    return "mid"


def analyze(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    sess = c.execute("select id from session").fetchall()
    reps = c.execute("select session_id, markdown from report").fetchall()
    if len(reps) != 1 or len(sess) != 1:
        return None
    sid, md = reps[0]["session_id"], reps[0]["markdown"] or ""
    cut = md.rfind("\n---\n\n### 来源\n")
    if cut >= 0:
        md = md[:cut]          # 去掉文末自动生成的来源清单, 否则每个来源都"被引用"
    srcs = [dict(r) for r in c.execute(
        "select id, domain, source_type, source_nature, engine, on_topic, "
        "superseded_by from source where session_id=? order by rowid", (sid,))]
    for i, s in enumerate(srcs, 1):
        s["no"] = i
        s["tier"] = tier(s["domain"])
    by_no = {s["no"]: s for s in srcs}
    cites = Counter()
    for m in re.finditer(r"\[\s*(S[^\]]*)\]", md):
        for n in {int(x.group(1)) for x in CITE_ANY.finditer(m.group(1))}:
            cites[n] += 1
    bad = [n for n in cites if n not in by_no]
    return {"sid": sid, "len": len(md), "srcs": srcs, "cites": cites, "bad_nums": bad,
            "by_no": by_no}


def summarize(name, a):
    srcs, cites = a["srcs"], a["cites"]
    tot_c = sum(n for k, n in cites.items() if k in a["by_no"])
    out = {"db": name, "len": a["len"], "sources": len(srcs),
           "cited_sources": sum(1 for k in cites if k in a["by_no"]),
           "cite_total": tot_c, "bad_cite_nums": len(a["bad_nums"])}
    for t in ("high", "mid", "low"):
        ss = [s for s in srcs if s["tier"] == t]
        out[f"src_{t}"] = len(ss)
        out[f"cite_{t}"] = sum(cites.get(s["no"], 0) for s in ss)
    nat = defaultdict(lambda: [0, 0])
    for s in srcs:
        k = s["source_nature"] or "unjudged"
        nat[k][0] += 1
        nat[k][1] += cites.get(s["no"], 0)
    out["nature"] = {k: tuple(v) for k, v in sorted(nat.items())}
    dom = Counter()
    for s in srcs:
        dom[s["domain"]] += cites.get(s["no"], 0)
    top = dom.most_common(3)
    out["top_domain"] = [(d, n, round(n / tot_c, 3) if tot_c else 0) for d, n in top]
    top_src = max(((cites.get(s["no"], 0), s["domain"]) for s in srcs), default=(0, ""))
    out["top_single_source"] = (top_src[1], top_src[0],
                                round(top_src[0] / tot_c, 3) if tot_c else 0)
    low_srcs = sorted(((cites.get(s["no"], 0), s["domain"]) for s in srcs
                       if s["tier"] == "low" and cites.get(s["no"], 0)), reverse=True)
    out["low_cited"] = low_srcs[:5]
    return out


def agg(rows, label):
    tot=lambda k: sum(r[k] for r in rows)
    c=tot("cite_total") or 1; s=tot("sources") or 1
    print(f"{label:<28} n={len(rows)} sources={tot('sources')} cites={tot('cite_total')} | "
          f"low: src {tot('src_low')/s:.1%} cite {tot('cite_low')/c:.1%} | "
          f"high: src {tot('src_high')/s:.1%} cite {tot('cite_high')/c:.1%} | "
          f"mean top1-source share {sum(r['top_single_source'][2] for r in rows)/len(rows):.1%}")


if __name__ == "__main__":
    rows = []
    for p in sorted(glob.glob(f"{SNAP}/ra_*.db")):
        name = os.path.basename(p)[:-3]
        try:
            a = analyze(p)
        except sqlite3.DatabaseError as e:
            print("skip", name, e, file=sys.stderr)
            continue
        if a is None:
            print("skip(multi-session)", name, file=sys.stderr)
            continue
        rows.append(summarize(name, a))
    GROUPS = {
      "MiniMax标准 v18-v26(rustgo/cbam/glp1)": [r for r in rows if r["db"] in {"ra_v18_baseline","ra_v19_batch1","ra_v20_batch2","ra_v21_batch3","ra_v22_final","ra_v23_final","ra_v24_final","ra_v25_cbam","ra_v25_glp1","ra_v25_rustgo","ra_v26_rustgo"}],
      "Ark标准 v28-v30(有正文)": [r for r in rows if r["db"] in {"ra_v28_B","ra_v28_C","ra_v29_D","ra_v29_E","ra_v30_std"}],
      "快速档(全部)": [r for r in rows if "fast" in r["db"] or r["db"] in {"ra_v29_F1","ra_v29_F2"}],
    }
    json.dump(rows, open("/tmp/auth/stats.json", "w"), ensure_ascii=False, indent=1)
    for k,v in GROUPS.items(): agg(v,k)
    print()
    for r in rows:
        print(f"{r['db']:<20} len={r['len']:>6} src={r['sources']:>3} cited={r['cited_sources']:>3} "
              f"cites={r['cite_total']:>3} bad={r['bad_cite_nums']} | "
              f"high {r['src_high']}/{r['cite_high']}  mid {r['src_mid']}/{r['cite_mid']}  "
              f"low {r['src_low']}/{r['cite_low']} | top1src {r['top_single_source']}")
